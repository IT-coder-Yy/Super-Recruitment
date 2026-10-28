"""单进程 Demo 定时扫描入口，无需打开业务页面。"""
from datetime import timedelta, timezone

from sqlalchemy import select, update

from app.models import Application, ApplicationStatus, EvaluationAttempt, FollowUpStatus, FollowUpTask, Notification, NotificationStatus, Offer, OfferStatus, OnboardingTask, OnboardingStatus, InterviewStatus, utc_now
from app.services.followups import DEFAULT_FOLLOWUP_RULES, FollowUpRule, scan_followups
from app.services.offers import notify_internal
from app.services.workflow import get_or_create_follow_up, write_audit_log

RULES = DEFAULT_FOLLOWUP_RULES + (
    FollowUpRule("exam_pending", ("assessment_pending", "written_pending"), ("status_changed_at",), timedelta(hours=48),
                 "测评或笔试待跟进", "核实发放及 HR 结论", "{candidate_name} 的考试阶段需要 HR 核实"),
    FollowUpRule("offer_approval_pending", ("offer_pending",), ("status_changed_at",), timedelta(hours=24),
                 "Offer 待办理", "完善条款并审批", "{candidate_name} 的 Offer 待办理"),
    FollowUpRule("onboarding_pending", ("onboarding",), ("status_changed_at",), timedelta(days=2),
                 "入职办理待跟进", "核对材料、准备状态与到岗日期", "{candidate_name} 的入职准备需要核实"),
)


def recover_inflight(session):
    for item in session.scalars(select(Notification).where(Notification.status == NotificationStatus.SENDING)):
        item.status, item.error_message = NotificationStatus.UNKNOWN, "上次发送中断，需人工核实送达后才能重试"
        write_audit_log(session, actor="系统恢复", action="notification.interrupted", entity_type="notification", entity_id=item.id)
    session.execute(update(EvaluationAttempt).where(EvaluationAttempt.status == "running").values(status="failed", error="上次评估中断，可重新评估"))


def run_followups(session_factory, *, now=None):
    now = now or utc_now()
    with session_factory() as session:
        rows = []
        for application in session.scalars(select(Application)):
            extra = (application.extra_data or {}).get("onboarding", {})
            latest = max(application.interviews, key=lambda i: i.round) if application.interviews else None
            interview_end = None
            status = application.status.value
            if latest and latest.status == InterviewStatus.CONFIRMED and latest.confirmed_end_at and not latest.feedback:
                from app.workbench import appointment_time
                interview_end = appointment_time(latest.confirmed_end_at)
                if interview_end <= now:
                    status = "feedback_pending"
            rows.append({"id": application.id, "status": status, "candidate_name": application.candidate.name,
                         "owner": application.owner, "created_at": _utc(application.created_at), "applied_at": _utc(application.applied_at),
                         "status_changed_at": _utc(application.status_changed_at), "onboarding_date": extra.get("expected_date"), "interview_end_at": interview_end})
        close_legacy_followups(session, now=now)
        desired = scan_followups(rows, rules=RULES, now=now)
        active = {(d["target_id"], d["rule_code"]) for d in desired}
        codes = {r.code for r in RULES}
        for task in session.scalars(select(FollowUpTask).where(FollowUpTask.target_type == "application", FollowUpTask.status == FollowUpStatus.OPEN)):
            if task.rule_code in codes and (task.target_id, task.rule_code) not in active:
                task.status, task.resolved_at = FollowUpStatus.RESOLVED, now
        new = []
        for entry in desired:
            prefix = f"application:{entry['target_id']}:{entry['rule_code']}:"
            task, created = get_or_create_follow_up(session, {
                "target_type": "application", "target_id": entry["target_id"], "rule_code": entry["rule_code"],
                "window_key": entry["dedupe_key"].removeprefix(prefix), "owner": entry["owner"],
                "due_at": entry["due_at"].astimezone(timezone.utc), "reason": entry["reason"],
                "suggested_action": entry["suggested_action"], "reminder_draft": entry["message_draft"],
                "next_check_at": entry["next_check_at"].astimezone(timezone.utc),
            }, actor="定时跟进程序")
            if created:
                new.append(task)
        session.commit()
        for task in new:
            notify_internal(session, title=task.reason, content=task.reminder_draft, target_type="follow_up", target_id=task.id)
        session.commit()
        return len(new)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def close_legacy_followups(session, *, now=None):
    for task in session.scalars(select(FollowUpTask).where(FollowUpTask.status == FollowUpStatus.OPEN, FollowUpTask.target_type.in_(["offer", "onboarding"]))):
        if task.target_type == "offer":
            offer = session.get(Offer, task.target_id)
            closed = not offer or offer.status not in {OfferStatus.PENDING_APPROVAL, OfferStatus.SENT}
        else:
            preparation = session.get(OnboardingTask, task.target_id)
            closed = not preparation or preparation.status != OnboardingStatus.OPEN or preparation.offer.application.status != ApplicationStatus.ONBOARDING
        if closed:
            task.status, task.resolved_at = FollowUpStatus.RESOLVED, now or utc_now()
