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
CHANGE_PENDING = {"real_updating", "real_update_unknown", "real_cancelling", "real_cancel_unknown"}
PENDING = CREATION_PENDING | CHANGE_PENDING


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
        change = (item.meeting_request or {}).get("change", {})
        if item.meeting_status in CHANGE_PENDING and change.get("action") == "update":
            intervals.append({"start_at": datetime.fromisoformat(change["start_time"]),
                              "end_at": datetime.fromisoformat(change["end_time"])})
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
    if interview.meeting_status in CHANGE_PENDING:
        return reconcile_change(session, interview)
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


def change_booking(session: Session, interview_id: int, *, action: str, revision: str,
                   reason: str, start_at: datetime | None = None, end_at: datetime | None = None):
    """HR显式改期/取消；持久化操作版本，重复表单不能再次发出写请求。"""
    interview = lock_record(session, Interview, interview_id)
    snapshot = dict(interview.meeting_request or {})
    if (interview.status != InterviewStatus.CONFIRMED or interview.feedback
            or interview.application.status != ApplicationStatus.INTERVIEW_CONFIRMED
            or interview.meeting_status != "real_created" or not interview.meeting_id
            or not revision or revision != snapshot.get("revision", snapshot.get("attempt_id"))):
        raise ValueError("会议状态或操作版本已变化，请刷新；结果未知时只能查询核实")
    if action not in {"update", "cancel"} or not reason.strip():
        raise ValueError("请选择改期或取消，并填写原因")
    if appointment_time(interview.confirmed_start_at) <= datetime.now(timezone.utc):
        raise ValueError("已到开始时间的会议不能在此改期或取消，请在腾讯会议核实")
    change = {"id": uuid4().hex, "action": action, "reason": reason.strip()[:300]}
    if action == "update":
        if start_at is None or end_at is None:
            raise ValueError("改期须填写完整起止时间")
        start, end = appointment_time(start_at), appointment_time(end_at)
        if start <= datetime.now(timezone.utc) or end <= start:
            raise ValueError("改期须为未来时段，结束时间须晚于开始时间")
        if start == appointment_time(interview.confirmed_start_at) and end == appointment_time(interview.confirmed_end_at):
            raise ValueError("新时间与原安排相同，无需改期")
        for busy in occupied_intervals(session, exclude_id=interview.id):
            if intervals_conflict(start, end, busy["start_at"], busy["end_at"], buffer_minutes=DEFAULT_BUFFER_MINUTES):
                raise ValueError("新时段已被占用，请重新选择")
        change.update(start_time=start.isoformat(), end_time=end.isoformat())
    adapter = TencentMeetingAdapter()
    if adapter.account_fingerprint != snapshot.get("account_fingerprint"):
        raise ValueError("会议账号配置已变化，请恢复原配置后操作")
    interview.meeting_request = {**snapshot, "change": change}
    interview.meeting_status = "real_updating" if action == "update" else "real_cancelling"
    meeting_id = interview.meeting_id
    _audit(session, interview, f"{action}_requested", change=change, previous_revision=revision)
    session.commit()
    accepted = False
    try:
        if action == "update":
            result = adapter.update_meeting(meeting_id, change)
            accepted = True
            if str(result.get("meeting_id")) != meeting_id:
                raise MeetingError("改期响应不能确认本场会议")
            # 官方修改响应只保证ID，不保证新时间；额外查询一次核对，不重试。
            result = adapter.get_meeting(meeting_id=meeting_id)
            verified_meeting(result, {**snapshot, **change}, meeting_id)
        else:
            result = adapter.cancel_meeting(meeting_id)
            if result.get("cancelled") is not True:
                raise MeetingError("取消结果无法确认")
    except Exception as exc:
        interview = lock_record(session, Interview, interview_id)
        current = dict(interview.meeting_request or {})
        if (current.get("change") or {}).get("id") == change["id"] and interview.meeting_status in CHANGE_PENDING:
            definite = not accepted and isinstance(exc, MeetingError) and not exc.uncertain
            interview.meeting_status = "real_created" if definite else f"real_{action}_unknown"
            if definite:
                current.pop("change")
                current["revision"] = change["id"]
                interview.meeting_request = current
            _audit(session, interview, f"{action}_failed" if definite else f"{action}_unknown", error_type=type(exc).__name__)
            session.commit()
        raise MeetingError("会议变更未完成，请刷新查看；待核实状态不会自动重发") from None
    return _complete_change(session, interview_id, change, result)


def _complete_change(session: Session, interview_id: int, change: dict, result: dict):
    interview = lock_record(session, Interview, interview_id)
    snapshot = dict(interview.meeting_request or {})
    if snapshot.get("revision") == change["id"] and interview.meeting_status not in CHANGE_PENDING:
        return interview
    if (snapshot.get("change") or {}).get("id") != change["id"] or interview.meeting_status not in CHANGE_PENDING:
        raise ValueError("会议变更已被处理，请刷新")
    if change["action"] == "cancel":
        interview.status, interview.meeting_status = InterviewStatus.CANCELLED, "real_cancelled"
        interview.calendar_status, interview.booking_token = "local_cancelled", None
        for slot in interview.slots:
            slot.status = InterviewSlotStatus.RELEASED
        transition_application(session, interview.application_id, ApplicationStatus.INTERVIEW_PENDING,
                               actor="HR", is_human_action=True, reason=change["reason"])
    else:
        _, url = verified_meeting(result, {**snapshot, **change}, interview.meeting_id)
        # SQLite内沿用北京时间钟面，与已有排期和日历一致。
        start = appointment_time(datetime.fromisoformat(change["start_time"])).replace(tzinfo=None)
        end = appointment_time(datetime.fromisoformat(change["end_time"])).replace(tzinfo=None)
        interview.confirmed_start_at, interview.confirmed_end_at = start, end
        for slot in interview.slots:
            if slot.status == InterviewSlotStatus.CONFIRMED:
                slot.start_at, slot.end_at = start, end
        interview.meeting_url, interview.meeting_status = url, "real_created"
        snapshot.update(start_time=change["start_time"], end_time=change["end_time"])
    snapshot.pop("change")
    snapshot["revision"] = change["id"]
    interview.meeting_request = snapshot
    _audit(session, interview, f"{change['action']}_confirmed", change=change)
    session.commit()
    return interview


def reconcile_change(session: Session, interview: Interview):
    snapshot = dict(interview.meeting_request or {})
    change = dict(snapshot.get("change") or {})
    adapter = TencentMeetingAdapter()
    if not change or adapter.account_fingerprint != snapshot.get("account_fingerprint"):
        raise ValueError("会议操作或账号配置已变化，请恢复原配置后核实")
    interview_id, meeting_id = interview.id, interview.meeting_id
    _audit(session, interview, "change_query_requested", change_id=change["id"])
    session.commit()
    try:
        result = adapter.get_meeting(meeting_id=meeting_id)
        if change["action"] == "cancel":
            if str(result.get("meeting_id")) != meeting_id or result.get("status") != "MEETING_STATE_CANCELLED":
                raise MeetingError("尚未确认取消；保留原占用")
        else:
            verified_meeting(result, {**snapshot, **change}, meeting_id)
    except Exception as exc:
        interview = lock_record(session, Interview, interview_id)
        _audit(session, interview, "change_query_failed", error_type=type(exc).__name__)
        session.commit()
        raise MeetingError("查询未确认变更，保留占用；不会重发改期或取消请求") from None
    return _complete_change(session, interview_id, change, result)
