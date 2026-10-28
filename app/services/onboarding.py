"""HR 登记与独立到岗确认，复用投递 JSON 和原有准备任务。"""
from copy import deepcopy
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Application, ApplicationStatus, FollowUpTask, FollowUpStatus, OnboardingStatus, OnboardingTask, utc_now
from app.services.workflow import lock_record, transition_application, write_audit_log
from app.services.scheduling import DEFAULT_TIMEZONE


def update_onboarding(session, application_id: int, *, action: str, version: int, values: dict):
    application = lock_record(session, Application, application_id)
    data = deepcopy(application.extra_data or {})
    before = data.get("onboarding", {})
    note = values.get("note", "").strip()
    if application.status == ApplicationStatus.JOINED and action == "join" and before.get("actual_date") == values.get("actual_date"):
        return application
    if application.status != ApplicationStatus.ONBOARDING or before.get("revision", 0) != version:
        raise ValueError("入职阶段或资料版本已变化，请刷新；已放弃的办理不能恢复")
    if action not in {"update", "needs_supplement", "defer", "withdraw", "join"}:
        raise ValueError("未知入职操作")
    if action != "update" and not note:
        raise ValueError("请填写核实说明")
    now = utc_now()
    registration = {**before, "revision": version + 1, "note": note, "updated_at": now.isoformat()}
    today = now.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).date()
    if action in {"update", "defer"}:
        expected = values.get("expected_date") or before.get("expected_date")
        if not expected or date.fromisoformat(expected) < today:
            raise ValueError("请填写今天或未来的预计到岗日期")
        location = values.get("location") or before.get("location")
        if not location:
            raise ValueError("请填写实际办理的报到地点")
        registration.update(expected_date=expected, location=location, state="deferred" if action == "defer" else "preparing")
    elif action == "needs_supplement":
        registration["state"] = "needs_supplement"
    elif action == "withdraw":
        registration["state"] = "withdrawn"
        for task in session.scalars(select(OnboardingTask).where(OnboardingTask.application_id == application.id, OnboardingTask.status == OnboardingStatus.OPEN)):
            task.status, task.note = OnboardingStatus.CANCELLED, note
        for task in session.scalars(select(FollowUpTask).where(FollowUpTask.target_type == "application", FollowUpTask.target_id == application.id, FollowUpTask.status == FollowUpStatus.OPEN)):
            task.status = FollowUpStatus.CANCELLED
        transition_application(session, application.id, ApplicationStatus.ONBOARDING_WITHDRAWN, actor="HR", is_human_action=True, reason=note)
    else:
        actual = values.get("actual_date")
        if not actual or date.fromisoformat(actual) > today:
            raise ValueError("请核实已实际到岗的日期，不能填写未来日期")
        if not before.get("expected_date") or before.get("state") == "needs_supplement":
            raise ValueError("请先补齐入职登记和待补材料")
        if session.scalar(select(OnboardingTask.id).where(OnboardingTask.application_id == application.id, OnboardingTask.status != OnboardingStatus.DONE).limit(1)):
            raise ValueError("请先完成材料核对、账号和工位等准备事项")
        registration.update(actual_date=actual, state="joined")
        transition_application(session, application.id, ApplicationStatus.JOINED, actor="HR", is_human_action=True, reason=note)
    data["onboarding"] = registration
    application.extra_data = data
    from app.services.periodic import close_legacy_followups
    close_legacy_followups(session)
    write_audit_log(session, actor="HR", action=f"onboarding.{action}", entity_type="application", entity_id=application.id,
                    before_data=before, after_data=registration)
    session.flush()
    return application
