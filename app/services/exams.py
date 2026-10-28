"""测评与笔试共享资源、邮件和 HR 结论，不读取或批改试题。"""
from copy import deepcopy
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationStatus, ExamDispatch, ExamResource, Notification, NotificationStatus
from app.services.offers import send_email_notification
from app.services.workflow import lock_record, transition_application, write_audit_log

KINDS = {"assessment": ("测评", ApplicationStatus.ASSESSMENT_PENDING, ApplicationStatus.WRITTEN_PENDING),
         "written": ("笔试", ApplicationStatus.WRITTEN_PENDING, ApplicationStatus.INTERVIEW_PENDING)}
DELIVERED = {NotificationStatus.SENT, NotificationStatus.MOCKED}


def validate_resource(kind: str, title: str, url: str) -> None:
    parsed = urlsplit(url.strip())
    if kind not in KINDS or not title.strip() or len(title) > 200:
        raise ValueError("请选择测评或笔试，并填写标题（最多 200 字）")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in url):
        raise ValueError("请填写有效的 http(s) 外部试卷链接")


def delete_or_disable(session: Session, resource_id: int) -> str:
    resource = lock_record(session, ExamResource, resource_id)
    used = session.scalar(select(ExamDispatch.id).where(ExamDispatch.resource_id == resource.id).limit(1))
    action = "disabled" if used else "deleted"
    write_audit_log(session, actor="HR", action=f"exam_resource.{action}", entity_type="exam_resource", entity_id=resource.id,
                    before_data={"title": resource.title, "url": resource.url, "kind": resource.kind})
    if used:
        resource.active = False
    else:
        # 先删记录后由路由清理该资源的内部文件；失败可重试，不扫其他文件。
        session.delete(resource)
    session.flush()
    return action


def preview_dispatch(application: Application, resource: ExamResource) -> dict:
    if resource.job_id != application.job_id:
        raise ValueError("试卷不适用于本次投递岗位，请选择对应岗位资源")
    if not resource.active:
        raise ValueError("资源已停用，请选择其他资源")
    if application.status != KINDS[resource.kind][1]:
        raise ValueError("投递尚未到达该考试阶段，或阶段已经结束")
    email = application.candidate.email
    if not email or "@" not in email:
        raise ValueError("请先补充有效邮箱")
    label = KINDS[resource.kind][0]
    return {"resource_id": resource.id, "resource_version": 1, "title": resource.title, "url": resource.url,
            "recipient": email, "subject": f"{application.job.title}｜{label}邀请",
            "content": f"{application.candidate.name}，你好：\n请完成 {application.job.title} 的{label}：{resource.title}\n{resource.url}\n如有问题请联系 HR。",
            "candidate_revision": (application.candidate.structured_data or {}).get("revision", 0)}


def send_exam(session: Session, application_id: int, resource_id: int, *, candidate_revision: int) -> Notification:
    application = lock_record(session, Application, application_id)
    resource = lock_record(session, ExamResource, resource_id)
    snapshot = preview_dispatch(application, resource)
    if candidate_revision != snapshot["candidate_revision"]:
        raise ValueError("候选人资料已变更，请重新预览收件人与邮件")
    dispatch = session.scalar(select(ExamDispatch).where(ExamDispatch.application_id == application.id, ExamDispatch.resource_id == resource.id))
    for other in session.scalars(select(ExamDispatch).where(ExamDispatch.application_id == application.id, ExamDispatch.kind == resource.kind)):
        if dispatch and other.id == dispatch.id:
            continue
        latest = dispatch_notification(session, other.id)
        if latest and latest.status != NotificationStatus.FAILED:
            raise ValueError("已有发放记录已发送或待核实，不能重复发放其他试卷")
    if dispatch is None:
        dispatch = ExamDispatch(application_id=application.id, resource_id=resource.id, kind=resource.kind, snapshot=snapshot)
        session.add(dispatch)
        session.flush()
        write_audit_log(session, actor="HR", action="exam.dispatched", entity_type="application", entity_id=application.id,
                        after_data={"dispatch_id": dispatch.id, "snapshot": snapshot})
    elif dispatch.snapshot != snapshot:
        raise ValueError("已保存的发放快照与当前资料不同，不能向旧邮箱重试；请核对后登记新资源并重新预览发放")
    snapshot = dispatch.snapshot
    return send_email_notification(session, recipient=snapshot["recipient"], subject=snapshot["subject"], content=snapshot["content"],
                                   target_type="exam_dispatch", target_id=dispatch.id, template=resource.kind)


def dispatch_notification(session: Session, dispatch_id: int) -> Notification | None:
    return session.scalar(select(Notification).where(Notification.target_type == "exam_dispatch", Notification.target_id == dispatch_id)
                          .order_by(Notification.id.desc()))


def decide_exam(session: Session, application_id: int, *, kind: str, result: str, note: str, version: int) -> Application:
    if kind not in KINDS or result not in {"passed", "failed", "skipped"} or not note.strip():
        raise ValueError("请选择通过、不通过或跳过，并填写核实说明")
    application = lock_record(session, Application, application_id)
    data = deepcopy(application.extra_data or {})
    decisions = data.setdefault("exams", {})
    previous = decisions.get(kind)
    if previous:
        if previous["result"] == result and previous["note"] == note.strip() and previous["version"] == version + 1:
            return application
        raise ValueError("本阶段已确认，旧页面或冲突结论不能覆盖")
    if application.status != KINDS[kind][1] or data.get("workflow_revision", 0) != version:
        raise ValueError("阶段或版本已变化，请刷新")
    if result != "skipped":
        dispatches = session.scalars(select(ExamDispatch).where(ExamDispatch.application_id == application.id, ExamDispatch.kind == kind)).all()
        if not any((n := dispatch_notification(session, d.id)) and n.status in DELIVERED for d in dispatches):
            raise ValueError("请先完成发放，或核实待确认的邮件送达结果")
    decisions[kind] = {"result": result, "note": note.strip(), "version": version + 1}
    data["workflow_revision"] = version + 1
    application.extra_data = data
    transition_application(session, application.id, ApplicationStatus.REJECTED if result == "failed" else KINDS[kind][2],
                           actor="HR", is_human_action=True, reason=note.strip())
    write_audit_log(session, actor="HR", action=f"exam.{kind}_decided", entity_type="application", entity_id=application.id,
                    after_data=decisions[kind])
    return application
