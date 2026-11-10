"""真实建会占用、单次发送和人工核对；数据库提交与外部请求分开。"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.tencent_meeting import MeetingError, TencentMeetingAdapter, verified_meeting
from app.models import ApplicationStatus, Interview, InterviewSlot, InterviewSlotStatus, InterviewStatus
from app.services.scheduling import DEFAULT_BUFFER_MINUTES, intervals_conflict
from app.services.workflow import lock_record, transition_application, write_audit_log
from app.workbench import appointment_time


CREATION_PENDING = {"real_creating", "real_unknown"}
PENDING = CREATION_PENDING


def occupied_intervals(session: Session, *, exclude_id: int | None = None) -> list[dict]:
    rows = session.scalars(select(Interview).where(
        Interview.id != exclude_id if exclude_id is not None else True,
        Interview.status != InterviewStatus.CANCELLED,
        (Interview.confirmed_start_at.is_not(None)) | Interview.meeting_status.in_(PENDING),
    ))
    intervals = []
    for item in rows:
        if item.confirmed_start_at:
            intervals.append({"start_at": item.confirmed_start_at, "end_at": item.confirmed_end_at})
        elif item.meeting_request:
            intervals.append({"start_at": datetime.fromisoformat(item.meeting_request["start_time"]),
                              "end_at": datetime.fromisoformat(item.meeting_request["end_time"])})
    return intervals


def complete_booking(session: Session, interview: Interview, slot: InterviewSlot, url: str, *, mode: str):
    for item in interview.slots:
        item.status = InterviewSlotStatus.CONFIRMED if item.id == slot.id else InterviewSlotStatus.RELEASED
    interview.confirmed_start_at, interview.confirmed_end_at = slot.start_at, slot.end_at
    interview.status, interview.meeting_url = InterviewStatus.CONFIRMED, url
    interview.meeting_status = "real_created" if mode == "real" else "mock_created"
    interview.calendar_status = "local_created" if mode == "real" else "demo_created"
    interview.booking_token = None
    if interview.application.status == ApplicationStatus.SCHEDULING:
        transition_application(session, interview.application_id, ApplicationStatus.INTERVIEW_CONFIRMED,
                               actor="候选人预约")
    write_audit_log(session, actor="候选人", action="interview.confirmed", entity_type="interview",
                    entity_id=interview.id, after_data={"start_at": slot.start_at.isoformat(), "meeting_mode": mode})


def _audit(session, interview, action, **details):
    write_audit_log(session, actor="会议程序", action=f"interview.meeting_{action}",
                    entity_type="interview", entity_id=interview.id,
                    details={"attempt_id": (interview.meeting_request or {}).get("attempt_id"), **details})


def create_real_booking(session: Session, interview: Interview, slot: InterviewSlot, *, retry: bool = False):
    interview = lock_record(session, Interview, interview.id)
    if interview.status not in {InterviewStatus.DRAFT, InterviewStatus.INVITED} or interview.confirmed_start_at:
        raise ValueError("本轮预约已处理，不能重复建会")
    if interview.meeting_status in PENDING or interview.meeting_id:
        raise ValueError("已有建会请求待核实，请查询已有会议，勿重新创建")
    if retry != (interview.meeting_status == "real_failed"):
        raise ValueError("请由 HR 从失败记录明确重试")
    if slot.interview_id != interview.id or slot.status != InterviewSlotStatus.AVAILABLE:
        raise ValueError("预约时段已不可用")
    if interview.application.status != ApplicationStatus.SCHEDULING:
        raise ValueError("投递已不在预约阶段")
    start, end = appointment_time(slot.start_at), appointment_time(slot.end_at)
    if start <= datetime.now(timezone.utc):
        raise ValueError("预约时段已过期，请重新安排")
    for busy in occupied_intervals(session, exclude_id=interview.id):
        if intervals_conflict(start, end, busy["start_at"], busy["end_at"], buffer_minutes=DEFAULT_BUFFER_MINUTES):
            raise ValueError("该时段已被占用，请重新安排")
    adapter = TencentMeetingAdapter()
    previous = interview.meeting_request or {}
    if retry and previous.get("account_fingerprint") != adapter.account_fingerprint:
        raise ValueError("会议账号配置已变化，请恢复原配置后重试")
    attempt = uuid4().hex
    snapshot = {
        "attempt_id": attempt, "slot_id": slot.id,
        "subject": f"{interview.application.job.title[:60]}面试 · TF-{attempt}",
        "start_time": start.isoformat(), "end_time": end.isoformat(),
        "account_fingerprint": adapter.account_fingerprint,
    }
    interview.meeting_request, interview.meeting_status = snapshot, "real_creating"
    _audit(session, interview, "requested", retry=retry, previous_attempt=previous.get("attempt_id"),
           request={key: snapshot[key] for key in ("slot_id", "subject", "start_time", "end_time")})
    interview_id = interview.id
    # 先持久化占用，再释放 SQLite 写锁；中断后也不能重发。
    session.commit()
    try:
        result = adapter.create_meeting(snapshot)
    except Exception as exc:
        interview = lock_record(session, Interview, interview_id)
        if (interview.meeting_request or {}).get("attempt_id") == attempt and interview.meeting_status in CREATION_PENDING:
            certain_failure = isinstance(exc, MeetingError) and not exc.uncertain
            interview.meeting_status = "real_failed" if certain_failure else "real_unknown"
            _audit(session, interview, "failed" if certain_failure else "unknown", error_type=type(exc).__name__)
            session.commit()
        raise MeetingError("建会未确认，请联系 HR 查看建会记录并核实结果") from None
    return _accept_result(session, interview_id, snapshot, result, remember_id=True)


def _accept_result(session, interview_id, snapshot, result, *, remember_id=False):
    interview = lock_record(session, Interview, interview_id)
    if (interview.meeting_request or {}).get("attempt_id") != snapshot["attempt_id"]:
        raise ValueError("建会记录已变化，请重新核实")
    meeting_id, url = None, None
    try:
        meeting_id, url = verified_meeting(result, snapshot, interview.meeting_id)
        if session.scalar(select(Interview.id).where(Interview.meeting_id == meeting_id, Interview.id != interview_id)):
            raise MeetingError("该会议已关联其他面试，请核实")
    except MeetingError:
        if interview.meeting_status in CREATION_PENDING:
            # 只保留可安全解析且未被其他面试使用的ID，不持久化未校验的链接。
            proposed = str(result.get("meeting_id") or "")
            if (remember_id and not interview.meeting_id and proposed.isascii() and proposed.isdigit() and len(proposed) <= 20
                    and not session.scalar(select(Interview.id).where(Interview.meeting_id == proposed))):
                interview.meeting_id = proposed
            interview.meeting_status = "real_unknown"
            _audit(session, interview, "unknown", error_type="verification_failed")
            session.commit()
        raise
    if interview.meeting_status == "real_created":
        return interview  # 同一次创建与人工查询并发到达，复用已完成记录。
    if interview.meeting_status not in CREATION_PENDING:
        raise ValueError("本次建会状态已变化，不能覆盖")
    slot = session.get(InterviewSlot, snapshot["slot_id"])
    if slot is None or slot.interview_id != interview.id or interview.application.status != ApplicationStatus.SCHEDULING:
        raise ValueError("预约资料已变化，请人工核实")
    interview.meeting_id = meeting_id
    complete_booking(session, interview, slot, url, mode="real")
    _audit(session, interview, "confirmed", meeting_id=meeting_id)
    session.commit()
    return interview


def reconcile_booking(session: Session, interview_id: int, *, meeting_code: str = ""):
    interview = lock_record(session, Interview, interview_id)
    if interview.meeting_status == "real_created":
        return interview
    if interview.meeting_status not in PENDING or not interview.meeting_request:
        raise ValueError("只有待核实的建会记录可以查询恢复")
    snapshot = dict(interview.meeting_request)
    adapter = TencentMeetingAdapter()
    if adapter.account_fingerprint != snapshot["account_fingerprint"]:
        raise ValueError("会议账号配置已变化，请恢复原配置后核实")
    meeting_id = interview.meeting_id
    if not meeting_id and not meeting_code:
        raise ValueError("请在原账号核实本次主题与时间，填写对应会议号")
    _audit(session, interview, "query_requested")
    session.commit()
    try:
        result = adapter.get_meeting(meeting_id=meeting_id, meeting_code=meeting_code)
    except Exception as exc:
        interview = lock_record(session, Interview, interview_id)
        _audit(session, interview, "query_failed", error_type=type(exc).__name__)
        session.commit()
        raise MeetingError("查询未确认已有会议，保留占用；请核实，不会自动重建") from None
    return _accept_result(session, interview_id, snapshot, result)
