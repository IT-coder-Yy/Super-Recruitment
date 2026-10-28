from __future__ import annotations

import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from app.adapters.notifications import send_wecom_notification
from app.config import get_setting
from app.services.auth import current_actor
from app.models import (
    Application,
    ApplicationStatus,
    ExportRun,
    ExportRunStatus,
    FollowUpStatus,
    FollowUpTask,
    Interview,
    InterviewStatus,
    Notification,
    NotificationStatus,
    Offer,
    OfferApproval,
    OfferApprovalStatus,
    OfferStatus,
    OnboardingStatus,
    OnboardingTask,
    utc_now,
)
from app.services.workflow import (
    InvalidStatusTransition,
    lock_record,
    get_or_create_follow_up,
    transition_application,
    write_audit_log,
)


OFFER_TEMPLATE_VERSION = "testin-offer-2026-06"


def submit_interview_feedback(
    session: Session,
    interview_id: int,
    *,
    result: str,
    comment: str,
    next_action: str,
    actor: str = "本地管理员",
) -> dict[str, Any]:
    interview = lock_record(session, Interview, interview_id)
    if interview.feedback:
        if all(interview.feedback.get(k) == v for k, v in {"result": result, "comment": comment, "next_action": next_action}.items()):
            return {"application_id": interview.application_id, "status": interview.application.status.value}
        raise ValueError("本轮已有反馈，不能覆盖旧结论")
    if interview.status != InterviewStatus.CONFIRMED:
        raise ValueError("请先完成本轮面试预约")
    actor = current_actor.get() or actor
    application = interview.application
    interview.feedback = {
        "result": result,
        "comment": comment,
        "next_action": next_action,
        "actor": actor,
        "submitted_at": utc_now().isoformat(),
    }
    interview.status = InterviewStatus.COMPLETED

    _move_to_feedback_pending(session, application, actor=actor)
    if next_action == "next_round":
        transition_application(
            session,
            application.id,
            ApplicationStatus.NEXT_ROUND,
            actor=actor,
            is_human_action=True,
            reason=comment or "面试反馈建议进入下一轮",
        )
    elif next_action == "reject":
        transition_application(
            session,
            application.id,
            ApplicationStatus.REJECTED,
            actor=actor,
            is_human_action=True,
            reason=comment or "面试反馈不通过",
        )
    elif next_action == "offer":
        transition_application(
            session,
            application.id,
            ApplicationStatus.OFFER_PENDING,
            actor=actor,
            is_human_action=True,
            reason=comment or "面试反馈通过，进入 Offer 流程",
        )
        offer = create_offer(session, application.id, actor=actor)
        notify_internal(
            session,
            title="Offer 待审批",
            content=f"{application.candidate.name or '候选人'} 的 {application.job.title} Offer 已创建，等待审批。",
            target_type="offer",
            target_id=offer.id,
        )
    else:
        raise ValueError("next_action 仅支持 next_round、reject 或 offer")

    write_audit_log(
        session,
        actor=actor,
        action="interview.feedback_submitted",
        entity_type="interview",
        entity_id=interview.id,
        after_data=interview.feedback,
    )
    session.flush()
    return {"application_id": application.id, "status": application.status.value}


def create_offer(
    session: Session,
    application_id: int,
    *,
    actor: str = "本地管理员",
) -> Offer:
    application = lock_record(session, Application, application_id)
    if application.status != ApplicationStatus.OFFER_PENDING:
        raise ValueError("只有面试通过并进入 Offer 阶段才能创建录用通知")
    existing = session.scalar(
        select(Offer)
        .where(
            Offer.application_id == application.id,
            ~Offer.status.in_([OfferStatus.DECLINED, OfferStatus.EXPIRED]),
        )
        .order_by(desc(Offer.created_at))
    )
    if existing:
        return existing

    candidate = application.candidate
    content = build_offer_content(
        candidate_name=candidate.name or "同学",
        company_name="Testin",
        job_title=application.job.title,
        report_time="",
        report_location="",
        office_location="",
    )
    offer = Offer(
        application_id=application.id,
        status=OfferStatus.PENDING_APPROVAL,
        candidate_name=candidate.name or "待补充姓名",
        candidate_email=candidate.email,
        company_name="Testin",
        job_title=application.job.title,
        content=content,
        report_time="",
        report_location="",
        office_location="",
        expires_at=utc_now() + timedelta(days=7),
    )
    session.add(offer)
    session.flush()
    approval = OfferApproval(
        offer_id=offer.id,
        approver="管理员",
        status=OfferApprovalStatus.PENDING,
    )
    session.add(approval)
    write_audit_log(
        session,
        actor=actor,
        action="offer.created",
        entity_type="offer",
        entity_id=offer.id,
        after_data={"application_id": application.id, "job_title": offer.job_title},
    )
    session.flush()
    return offer


def offer_snapshot(offer: Offer) -> dict:
    return {"terms": offer.terms or {}, "job_title": offer.job_title, "company_name": offer.company_name,
            "report_time": offer.report_time, "report_location": offer.report_location, "office_location": offer.office_location,
            "expires_at": _as_utc_aware(offer.expires_at).isoformat() if offer.expires_at else None,
            "candidate_email": offer.candidate_email, "content": offer.content}


def missing_offer_terms(offer: Offer) -> list[str]:
    values = {"薪资": (offer.terms or {}).get("salary"), "补贴（无补贴请填无）": (offer.terms or {}).get("subsidy"),
              "岗位": offer.job_title, "报到日期": offer.report_time, "报到地址": offer.report_location,
              "办公地址": offer.office_location, "有效期": offer.expires_at, "收件邮箱": offer.candidate_email}
    return [name for name, value in values.items() if not value or "待" in str(value) or "确认" in str(value)]


def save_offer_terms(session: Session, offer_id: int, values: dict, *, version: int) -> Offer:
    offer = lock_record(session, Offer, offer_id)
    if offer.status not in {OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL, OfferStatus.APPROVED}:
        raise ValueError("已发放或已结束的 Offer 条款不可修改")
    current = int((offer.terms or {}).get("revision", 0))
    if version != current:
        raise ValueError("Offer 条款已更新，请刷新后再保存")
    delivery = session.scalar(select(Notification).where(Notification.target_type == "offer", Notification.target_id == offer.id,
        Notification.channel == "email", Notification.status.in_([NotificationStatus.SENDING, NotificationStatus.UNKNOWN, NotificationStatus.SENT, NotificationStatus.MOCKED])).limit(1))
    if delivery:
        raise ValueError("邮件已发送或送达待核实，不能改写条款")
    before = offer_snapshot(offer)
    report = datetime.fromisoformat(values["report_time"])
    expiry = _as_utc_aware(datetime.fromisoformat(values["expires_at"]))
    if report.date() < utc_now().date() or expiry <= utc_now():
        raise ValueError("报到日期不能早于今天，Offer 有效期须在未来")
    for key in ("job_title", "company_name", "office_location", "report_location"):
        setattr(offer, key, values[key].strip())
    offer.report_time, offer.expires_at = report.date().isoformat(), expiry
    offer.candidate_email = offer.application.candidate.email
    offer.terms = {"revision": current + 1, "salary": values["salary"].strip(), "subsidy": values["subsidy"].strip()}
    missing = missing_offer_terms(offer)
    if missing:
        raise ValueError("请补齐条款：" + "、".join(missing))
    offer.content = build_offer_content(candidate_name=offer.candidate_name, company_name=offer.company_name,
        job_title=offer.job_title, report_time=offer.report_time, report_location=offer.report_location, office_location=offer.office_location)
    offer.content += f"\n【薪资】{offer.terms['salary']}\n【补贴】{offer.terms['subsidy']}\n【有效期】{expiry.isoformat()}"
    offer.status, offer.candidate_token = OfferStatus.PENDING_APPROVAL, None
    session.add(OfferApproval(offer_id=offer.id, status=OfferApprovalStatus.PENDING, snapshot=offer_snapshot(offer)))
    write_audit_log(session, actor="HR", action="offer.terms_revised", entity_type="offer", entity_id=offer.id,
                    before_data=before, after_data=offer_snapshot(offer))
    session.flush()
    return offer


def approve_offer(session: Session, offer_id: int, *, approver: str = "管理员", reason: str = "单级审批通过", version: int | None = None) -> Offer:
    offer = lock_record(session, Offer, offer_id)
    snapshot = offer_snapshot(offer)
    if version is not None and version != (offer.terms or {}).get("revision", 0):
        raise ValueError("条款版本已更新，请重新审核")
    if offer.status == OfferStatus.APPROVED:
        return offer
    if offer.status not in {OfferStatus.PENDING_APPROVAL, OfferStatus.DRAFT}:
        raise ValueError("当前 Offer 状态不允许审批")
    missing = missing_offer_terms(offer)
    if missing:
        raise ValueError("审批前请补齐：" + "、".join(missing))
    if _as_utc_aware(offer.expires_at) <= utc_now():
        raise ValueError("Offer 已过期，请更新条款后重新审批")
    approval = OfferApproval(offer_id=offer.id, status=OfferApprovalStatus.APPROVED, approver=current_actor.get() or approver,
                            decision_reason=reason, decided_at=utc_now(), snapshot=snapshot)
    session.add(approval)
    offer.status = OfferStatus.APPROVED
    write_audit_log(session, actor=approver, action="offer.approved", entity_type="offer", entity_id=offer.id, after_data=snapshot)
    session.flush()
    return offer


def return_offer(session: Session, offer_id: int, *, version: int, reason: str) -> Offer:
    offer = lock_record(session, Offer, offer_id)
    if offer.status not in {OfferStatus.PENDING_APPROVAL, OfferStatus.APPROVED} or version != (offer.terms or {}).get("revision", 0) or not reason.strip():
        raise ValueError("请在当前条款版本下填写退回原因")
    if session.scalar(select(Notification.id).where(Notification.target_type == "offer", Notification.target_id == offer.id, Notification.channel == "email", Notification.status != NotificationStatus.FAILED).limit(1)):
        raise ValueError("发送结果待核实或已送达，不能退回")
    offer.status = OfferStatus.DRAFT
    session.add(OfferApproval(offer_id=offer.id, status=OfferApprovalStatus.REJECTED, approver=current_actor.get() or "HR",
                             decision_reason=reason, decided_at=utc_now(), snapshot=offer_snapshot(offer)))
    write_audit_log(session, actor="HR", action="offer.returned", entity_type="offer", entity_id=offer.id, after_data={"reason": reason, "version": version})
    return offer


def send_offer(session: Session, offer_id: int, *, actor: str = "本地管理员") -> Offer:
    offer = lock_record(session, Offer, offer_id)
    if offer.status in {OfferStatus.SENT, OfferStatus.ACCEPTED, OfferStatus.DECLINED}:
        return offer
    if offer.status != OfferStatus.APPROVED:
        raise ValueError("请先审批 Offer，再发放")
    if offer.application.status != ApplicationStatus.OFFER_PENDING:
        raise ValueError("投递已离开 Offer 阶段，禁止发信")
    if offer.candidate_email != offer.application.candidate.email:
        raise ValueError("候选人邮箱已变化，请保存新条款并重新审批收件人")
    approval = session.scalar(select(OfferApproval).where(OfferApproval.offer_id == offer.id, OfferApproval.status == OfferApprovalStatus.APPROVED).order_by(OfferApproval.id.desc()))
    if not approval or approval.snapshot != offer_snapshot(offer):
        raise ValueError("当前条款没有对应审批快照，请重新审批")
    if missing_offer_terms(offer) or _as_utc_aware(offer.expires_at) <= utc_now():
        raise ValueError("Offer 条款缺失或已过期")
    if not offer.candidate_token:
        offer.candidate_token = secrets.token_urlsafe(24)
    from app.config import public_url
    result = send_email_notification(session, recipient=offer.candidate_email,
        subject=f"欢迎加入 {offer.company_name}｜{offer.job_title} 录用通知",
        content=f"{offer.content}\n\n请打开确认链接：{public_url('/offer/' + offer.candidate_token)}",
        target_type="offer", target_id=offer.id, template=f"offer_email_v{(offer.terms or {}).get('revision', 0)}")
    if result.status in {NotificationStatus.SENT, NotificationStatus.MOCKED}:
        offer = record_offer_delivery(session, offer_id, result, actor=actor)
    session.flush()
    return offer


def record_offer_delivery(session: Session, offer_id: int, notification: Notification, *, actor: str = "HR") -> Offer:
    """记录已核实的历史发送事实；即使核实时过期，也不再发信或复活已终止投递。"""
    offer = lock_record(session, Offer, offer_id)
    if offer.status == OfferStatus.APPROVED:
        offer.status, offer.sent_at = OfferStatus.SENT, notification.created_at
        if offer.application.status == ApplicationStatus.OFFER_PENDING:
            transition_application(session, offer.application_id, ApplicationStatus.OFFER_SENT, actor=actor,
                                   is_human_action=True, reason="Offer 送达已确认")
        if offer.expires_at and _as_utc_aware(offer.expires_at) <= utc_now():
            offer.status = OfferStatus.EXPIRED
        write_audit_log(session, actor=actor, action="offer.sent", entity_type="offer", entity_id=offer.id,
                        after_data={"notification_id": notification.id, "email_status": notification.status.value, "offer_status":offer.status.value})
    return offer


def respond_offer(
    session: Session,
    token: str,
    *,
    accepted: bool,
    note: str = "",
) -> Offer:
    offer = session.scalar(select(Offer).where(Offer.candidate_token == token))
    if offer is None:
        raise ValueError("Offer 链接无效")
    offer = lock_record(session, Offer, offer.id)
    if offer.status == (OfferStatus.ACCEPTED if accepted else OfferStatus.DECLINED):
        return offer
    if offer.status != OfferStatus.SENT:
        raise ValueError("当前 Offer 状态不允许该确认")
    if offer.expires_at and _as_utc_aware(offer.expires_at) < utc_now():
        offer.status = OfferStatus.EXPIRED
        write_audit_log(
            session,
            actor="系统",
            action="offer.expired",
            entity_type="offer",
            entity_id=offer.id,
            after_data={"expires_at": offer.expires_at.isoformat()},
        )
        session.flush()
        return offer

    offer.responded_at = utc_now()
    offer.response_note = note
    if accepted:
        offer.status = OfferStatus.ACCEPTED
        transition_application(
            session,
            offer.application_id,
            ApplicationStatus.OFFER_ACCEPTED,
            actor="候选人",
            is_human_action=True,
            reason="候选人接受 Offer",
        )
        _ensure_onboarding_tasks(session, offer)
        transition_application(
            session,
            offer.application_id,
            ApplicationStatus.ONBOARDING,
            actor="系统",
            is_human_action=True,
            reason="Offer 接受后进入入职准备",
        )
        notify_internal(
            session,
            title="候选人已接受 Offer",
            content=f"{offer.candidate_name} 已接受 {offer.job_title} Offer，请准备入职事项。",
            target_type="offer",
            target_id=offer.id,
        )
    else:
        offer.status = OfferStatus.DECLINED
        transition_application(
            session,
            offer.application_id,
            ApplicationStatus.OFFER_DECLINED,
            actor="候选人",
            is_human_action=True,
            reason=note or "候选人拒绝 Offer",
        )
        notify_internal(
            session,
            title="候选人拒绝 Offer",
            content=f"{offer.candidate_name} 已拒绝 {offer.job_title} Offer。",
            target_type="offer",
            target_id=offer.id,
        )
    session.flush()
    return offer


def confirm_onboarding_task(
    session: Session,
    task_id: int,
    *,
    actor: str = "本地管理员",
    note: str = "HR 确认完成",
) -> OnboardingTask:
    task = session.get(OnboardingTask, task_id)
    if task is None:
        raise ValueError("入职任务不存在")
    application = lock_record(session, Application, task.application_id)
    if application.status != ApplicationStatus.ONBOARDING:
        raise ValueError("该投递已结束入职办理，不能修改遗留任务")
    if task.status == OnboardingStatus.DONE:
        return task
    task.status = OnboardingStatus.DONE
    task.completed_at = utc_now()
    task.note = note
    from app.services.periodic import close_legacy_followups
    close_legacy_followups(session)
    write_audit_log(
        session,
        actor=actor,
        action="onboarding_task.completed",
        entity_type="onboarding_task",
        entity_id=task.id,
        after_data={"note": note},
    )
    session.flush()
    return task


def build_offer_content(
    *,
    candidate_name: str,
    company_name: str,
    job_title: str,
    report_time: str,
    report_location: str,
    office_location: str,
) -> str:
    return (
        f"欢迎你加入 {company_name}！\n\n"
        f"{candidate_name}，你好，恭喜你顺利通过了 {job_title} 的面试。"
        f"现向你发出正式录用通知，真诚邀请你加入 {company_name}。\n\n"
        "请在 Offer 有效期内打开确认链接，查看录用信息，并确认是否接受录用。\n\n"
        f"【报到时间】{report_time}\n"
        f"【报到地址】{report_location}\n"
        f"【办公地址】{office_location}\n\n"
        "点击“接受”后即视为已签署链接中的录用通知函；逾期未确认，Offer 将自动失效。"
    )


def send_email_notification(
    session: Session,
    *,
    recipient: str,
    subject: str,
    content: str,
    target_type: str | None = None,
    target_id: int | None = None,
    template: str = "offer_email",
) -> Notification:
    # 先持久化发送声明，崩溃后按待核实处理，避免刷新或重试重复发信。
    session.flush()
    session.execute(update(Notification).where(Notification.id == -1).values(id=Notification.id))
    previous = session.scalar(select(Notification).where(
        Notification.channel == "email", Notification.target_type == target_type,
        Notification.target_id == target_id, Notification.template == template,
    ).order_by(Notification.id.desc())) if target_id is not None else None
    if previous and previous.status != NotificationStatus.FAILED:
        return previous
    if previous:
        recipient, subject, content = previous.recipient, previous.subject, previous.content
    notification = Notification(channel="email", recipient=recipient, subject=subject, content=content,
        target_type=target_type, target_id=target_id, template=template, status=NotificationStatus.SENDING)
    session.add(notification)
    session.commit()
    smtp_host = get_setting("SMTP_HOST").strip()
    notification.status = NotificationStatus.MOCKED
    notification.provider_response = {"mode": "mock", "reason": "SMTP 未配置"}
    if smtp_host:
        sending_data, accepted = False, False
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = get_setting("SMTP_FROM") or get_setting("SMTP_USERNAME")
            message["To"] = recipient
            message.set_content(content)
            use_ssl = (get_setting("SMTP_SSL") or get_setting("SMTP_USE_SSL", "false")).lower() in {"true", "1", "yes"}
            port = int(get_setting("SMTP_PORT") or ("465" if use_ssl else "587"))
            factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
            with factory(smtp_host, port, timeout=10) as smtp:
                if not use_ssl:
                    smtp.starttls()
                if get_setting("SMTP_USERNAME"):
                    smtp.login(get_setting("SMTP_USERNAME"), get_setting("SMTP_PASSWORD"))
                sending_data = True
                refused = smtp.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                accepted = True
            notification.status = NotificationStatus.SENT
        except Exception as exc:
            definitive = isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError, smtplib.SMTPSenderRefused))
            notification.status = NotificationStatus.SENT if accepted else (
                NotificationStatus.UNKNOWN if sending_data and not definitive else NotificationStatus.FAILED)
            notification.error_message = f"SMTP {type(exc).__name__}；" + ("送达结果待 HR 核实" if notification.status == NotificationStatus.UNKNOWN else "请检查设置后重试")
        notification.provider_response = {"mode": "smtp", "transport": "ssl" if (get_setting("SMTP_SSL") or get_setting("SMTP_USE_SSL", "false")).lower() in {"true", "1", "yes"} else "starttls"}
    write_audit_log(session, actor="通知程序", action="notification.attempted", entity_type="notification", entity_id=notification.id,
                    after_data={"status": notification.status.value, "target_type": target_type, "target_id": target_id})
    session.commit()
    return notification


def notify_internal(
    session: Session,
    *,
    title: str,
    content: str,
    target_type: str | None = None,
    target_id: int | None = None,
) -> Notification:
    result = send_wecom_notification(f"{title}\n{content}")
    status = (
        NotificationStatus.MOCKED
        if result.get("is_mock")
        else NotificationStatus.SENT
        if result.get("ok")
        else NotificationStatus.FAILED
    )
    notification = Notification(
        channel="wecom",
        recipient="内部招聘群",
        target_type=target_type,
        target_id=target_id,
        template="internal_status",
        status=status,
        subject=title,
        content=content,
        provider_response=result,
        error_message=(result.get("error") or {}).get("message") if isinstance(result.get("error"), dict) else None,
    )
    session.add(notification)
    session.flush()
    return notification


def record_export_run(
    session: Session,
    *,
    export_type: str,
    status: ExportRunStatus,
    file_name: str | None = None,
    file_path: str | None = None,
    row_count: int = 0,
    sync_target: str | None = None,
    message: str | None = None,
) -> ExportRun:
    run = ExportRun(
        export_type=export_type,
        status=status,
        file_name=file_name,
        file_path=file_path,
        row_count=row_count,
        sync_target=sync_target,
        message=message,
    )
    session.add(run)
    session.flush()
    return run


def scan_offer_onboarding_reminders(session: Session) -> None:
    now = utc_now()
    pending_offers = session.scalars(
        select(Offer).where(Offer.status.in_([OfferStatus.PENDING_APPROVAL, OfferStatus.SENT]))
    ).all()
    for offer in pending_offers:
        code = "offer_approval_pending" if offer.status == OfferStatus.PENDING_APPROVAL else "offer_confirmation_pending"
        get_or_create_follow_up(
            session,
            {
                "target_type": "offer",
                "target_id": offer.id,
                "rule_code": code,
                "window_key": f"{code}:{offer.id}",
                "owner": offer.application.owner or "HR",
                "due_at": now,
                "reason": "Offer 等待处理",
                "suggested_action": "检查审批或候选人确认状态",
                "reminder_draft": f"{offer.candidate_name} 的 {offer.job_title} Offer 需要跟进。",
            },
            actor="follow_up_agent",
        )
    onboarding = session.scalars(
        select(OnboardingTask).where(OnboardingTask.status == OnboardingStatus.OPEN)
    ).all()
    for task in onboarding:
        get_or_create_follow_up(
            session,
            {
                "target_type": "onboarding",
                "target_id": task.id,
                "rule_code": "onboarding_pending",
                "window_key": f"onboarding_pending:{task.id}",
                "owner": task.owner or "HR",
                "due_at": task.due_at,
                "reason": "入职事项待完成",
                "suggested_action": "确认候选人到岗和入职材料",
                "reminder_draft": task.title,
            },
            actor="follow_up_agent",
        )


def list_offers(session: Session, *, limit: int = 200) -> list[Offer]:
    return (
        session.scalars(select(Offer).order_by(desc(Offer.updated_at)).limit(limit))
        .unique()
        .all()
    )


def list_onboarding_tasks(session: Session, *, limit: int = 200) -> list[OnboardingTask]:
    return session.scalars(
        select(OnboardingTask)
        .order_by(OnboardingTask.status, OnboardingTask.due_at)
        .limit(limit)
    ).unique().all()


def _move_to_feedback_pending(
    session: Session,
    application: Application,
    *,
    actor: str,
) -> None:
    for target in (
        ApplicationStatus.INTERVIEWED,
        ApplicationStatus.FEEDBACK_PENDING,
    ):
        if application.status == target:
            continue
        try:
            transition_application(
                session,
                application.id,
                target,
                actor=actor,
                is_human_action=True,
                reason="面试反馈提交",
            )
        except InvalidStatusTransition:
            continue


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ensure_onboarding_tasks(session: Session, offer: Offer) -> None:
    existing = session.scalar(select(OnboardingTask).where(OnboardingTask.offer_id == offer.id).limit(1))
    if existing:
        return
    due = utc_now() + timedelta(days=3)
    tasks = [
        "确认入职时间与报到地址",
        "核对入职材料",
        "准备账号",
        "准备工位",
    ]
    for title in tasks:
        session.add(
            OnboardingTask(
                offer_id=offer.id,
                application_id=offer.application_id,
                title=title,
                owner=offer.application.owner or "HR",
                due_at=due,
            )
        )
    session.flush()


def _get_application(session: Session, application_id: int) -> Application:
    application = session.get(Application, application_id)
    if application is None:
        raise ValueError("投递记录不存在")
    return application


def _get_offer(session: Session, offer_id: int) -> Offer:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise ValueError("Offer 不存在")
    return offer
