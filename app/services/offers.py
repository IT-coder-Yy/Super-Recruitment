from __future__ import annotations

import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.adapters.notifications import send_wecom_notification
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
    interview = session.get(Interview, interview_id)
    if interview is None:
        raise ValueError("面试记录不存在")
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
    application = _get_application(session, application_id)
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
        report_time="Offer 接受后由 HR 确认",
        report_location="Testin 办公区 1 层前台",
        office_location="Testin 项目办公室",
    )
    offer = Offer(
        application_id=application.id,
        status=OfferStatus.PENDING_APPROVAL,
        candidate_name=candidate.name or "待补充姓名",
        candidate_email=candidate.email,
        company_name="Testin",
        job_title=application.job.title,
        content=content,
        report_time="Offer 接受后由 HR 确认",
        report_location="Testin 办公区 1 层前台",
        office_location="Testin 项目办公室",
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


def approve_offer(
    session: Session,
    offer_id: int,
    *,
    approver: str = "管理员",
    reason: str = "单级审批通过",
) -> Offer:
    offer = _get_offer(session, offer_id)
    if offer.status not in {OfferStatus.PENDING_APPROVAL, OfferStatus.DRAFT}:
        raise ValueError("当前 Offer 状态不允许审批")
    approval = offer.approvals[0] if offer.approvals else OfferApproval(offer_id=offer.id)
    approval.status = OfferApprovalStatus.APPROVED
    approval.approver = approver
    approval.decision_reason = reason
    approval.decided_at = utc_now()
    session.add(approval)
    offer.status = OfferStatus.APPROVED
    write_audit_log(
        session,
        actor=approver,
        action="offer.approved",
        entity_type="offer",
        entity_id=offer.id,
        after_data={"reason": reason},
    )
    session.flush()
    return offer


def send_offer(
    session: Session,
    offer_id: int,
    *,
    actor: str = "本地管理员",
) -> Offer:
    offer = _get_offer(session, offer_id)
    if offer.status != OfferStatus.APPROVED:
        raise ValueError("请先审批 Offer，再发放")
    if not offer.candidate_email:
        raise ValueError("候选人邮箱缺失，无法发送 Offer")

    token = secrets.token_urlsafe(24)
    offer.candidate_token = token
    offer.sent_at = utc_now()
    offer.status = OfferStatus.SENT
    subject = f"欢迎你加入 Testin｜{offer.job_title} 录用通知"
    public_url = f"/offer/{token}"
    content = f"{offer.content}\n\n请打开确认链接：{public_url}"
    result = send_email_notification(
        session,
        recipient=offer.candidate_email,
        subject=subject,
        content=content,
        target_type="offer",
        target_id=offer.id,
    )
    transition_application(
        session,
        offer.application_id,
        ApplicationStatus.OFFER_SENT,
        actor=actor,
        is_human_action=True,
        reason="Offer 已发放给候选人邮箱",
    )
    notify_internal(
        session,
        title="Offer 已发放",
        content=f"{offer.candidate_name} 的 Offer 已发送到 {offer.candidate_email}。",
        target_type="offer",
        target_id=offer.id,
    )
    write_audit_log(
        session,
        actor=actor,
        action="offer.sent",
        entity_type="offer",
        entity_id=offer.id,
        after_data={"email_status": result.status.value, "candidate_email": offer.candidate_email},
    )
    session.flush()
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
    if offer.status != OfferStatus.SENT:
        raise ValueError("当前 Offer 状态不允许重复确认")
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
    task.status = OnboardingStatus.DONE
    task.completed_at = utc_now()
    task.note = note
    session.flush()
    remaining = session.scalar(
        select(OnboardingTask)
        .where(
            OnboardingTask.offer_id == task.offer_id,
            OnboardingTask.status == OnboardingStatus.OPEN,
        )
        .limit(1)
    )
    if remaining is None:
        application = task.offer.application
        if application.status == ApplicationStatus.ONBOARDING:
            transition_application(
                session,
                application.id,
                ApplicationStatus.JOINED,
                actor=actor,
                is_human_action=True,
                reason="入职任务完成，HR 确认到岗",
            )
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
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    status = NotificationStatus.MOCKED
    response: dict[str, Any] = {"mode": "mock", "reason": "SMTP 未配置"}
    error = None
    if smtp_host:
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME") or "no-reply@testin.local"
            message["To"] = recipient
            message.set_content(content)
            port = int(os.getenv("SMTP_PORT", "587"))
            username = os.getenv("SMTP_USERNAME", "")
            password = os.getenv("SMTP_PASSWORD", "")
            with smtplib.SMTP(smtp_host, port, timeout=10) as smtp:
                smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
            status = NotificationStatus.SENT
            response = {"mode": "smtp", "host": smtp_host}
        except Exception as exc:  # noqa: BLE001 - 邮件失败必须可审计但不破坏数据库
            status = NotificationStatus.FAILED
            error = str(exc)
            response = {"mode": "smtp", "host": smtp_host}

    notification = Notification(
        channel="email",
        recipient=recipient,
        target_type=target_type,
        target_id=target_id,
        template=template,
        status=status,
        subject=subject,
        content=content,
        provider_response=response,
        error_message=error,
    )
    session.add(notification)
    session.flush()
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
        "准备账号、工位和入职材料",
        "HR 人工确认候选人到岗",
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
