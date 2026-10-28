from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    ROOT_DIR,
    clear_llm_api_key,
    ensure_runtime_dirs,
    get_setting,
    public_integration_settings,
    public_llm_settings,
    reload_env,
    save_app_setting,
    save_llm_settings,
    public_url,
)

ensure_runtime_dirs()
reload_env(override=False)

from app.adapters.meeting import create_mock_tencent_meeting
from app.ai.service import AIService
from app.db import SessionLocal, get_db, init_db, session_scope
from app.models import (
    Application,
    ApplicationStatus,
    AuditLog,
    Candidate,
    Evaluation,
    ExportRun,
    ExportRunStatus,
    FollowUpStatus,
    FollowUpTask,
    Interview,
    InterviewSlot,
    InterviewSlotStatus,
    InterviewStatus,
    Job,
    Notification,
    NotificationStatus,
    Offer,
    OfferStatus,
    OnboardingStatus,
    OnboardingTask,
    User,
)
from app.schemas import EvaluationCreate, EvaluationDimension, InterviewCreate, InterviewSlotCreate
from app.seed import seed_database
from app.workbench import appointment_time, build_workbench_context
from app.services.auth import current_actor, hash_password, normalize_username, verify_password
from app.services.candidates import normalize_name, normalize_phone, revise_candidate
from app.services.exporter import export_recruitment_csv, export_recruitment_xlsx
from app.services.imports import (
    confirm_import_task,
    create_file_import_task,
    import_task_rows,
    parse_import_task,
    reject_import_task,
    serialize_import_task,
    store_upload_bytes,
    supplement_import_task,
    build_rule_parse,
)
from app.services.offers import (
    approve_offer,
    save_offer_terms,
    return_offer,
    missing_offer_terms,
    create_offer,
    list_offers,
    list_onboarding_tasks,
    notify_internal,
    record_export_run,
    respond_offer,
    record_offer_delivery,
    send_offer,
    send_email_notification,
    submit_interview_feedback,
    confirm_onboarding_task,
)
from app.services.scheduling import (
    BookingTokenError,
    DEFAULT_BUFFER_MINUTES,
    create_booking_token,
    generate_candidate_slots,
    intervals_conflict,
    verify_booking_token,
)
from app.services.workflow import (
    HumanActionRequired,
    InvalidStatusTransition,
    add_interview_slot,
    create_evaluation,
    create_interview,
    resolve_follow_up,
    transition_application,
    write_audit_log,
    lock_record,
)


from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from app.services.periodic import recover_inflight, run_followups


@asynccontextmanager
async def lifespan(application):
    startup()
    scheduler = None
    if get_setting("FOLLOWUP_ENABLED", "true").lower() not in {"false", "0", "no"}:
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(run_followups, "interval", seconds=max(1, int(get_setting("FOLLOWUP_INTERVAL_SECONDS", "900"))),
                          args=[SessionLocal], max_instances=1, coalesce=True, next_run_time=datetime.now(timezone.utc))
        scheduler.start()
    application.state.followup_scheduler = scheduler
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=True)


app = FastAPI(
    lifespan=lifespan,
    title="AI 招聘自动化 Demo",
    description="低成本、本地运行、人工审批优先的 AI 招聘工作流。",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=ROOT_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT_DIR / "app" / "templates")
templates.env.filters["appointment_time"] = appointment_time

PUBLIC_EXACT_PATHS = {"/health", "/login", "/register", "/favicon.ico"}
PUBLIC_PATH_PREFIXES = ("/static/", "/schedule/", "/offer/")
CSRF_EXEMPT_PREFIXES = ("/schedule/", "/offer/")
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 5

STATUS_LABELS = {
    "assessment_pending": "待测评", "written_pending": "待笔试", "onboarding_withdrawn": "放弃入职",
    "captured": "已采集",
    "parsing": "解析中",
    "needs_review": "待人工确认",
    "parsed": "已解析",
    "deduplicated": "已查重",
    "evaluating": "AI 评估中",
    "evaluation_review": "待评估审核",
    "interview_pending": "待安排面试",
    "on_hold": "暂缓",
    "rejected": "已淘汰",
    "scheduling": "排期中",
    "interview_confirmed": "面试已确认",
    "interviewed": "已面试",
    "feedback_pending": "待反馈",
    "next_round": "下一轮",
    "offer_pending": "待 Offer",
    "offer_sent": "Offer 已发",
    "offer_accepted": "Offer 已接受",
    "offer_declined": "Offer 已拒绝",
    "onboarding": "待入职",
    "joined": "已入职",
}


def startup() -> None:
    init_db()
    with session_scope() as session:
        recover_inflight(session)
        if (session.scalar(select(func.count(Candidate.id))) or 0) == 0:
            seed_database(session)


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    path = request.url.path
    if (
        request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith("/api/")
        and not path.startswith(CSRF_EXEMPT_PREFIXES)
    ):
        if not await _csrf_request_valid(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403)
            return Response("CSRF 校验失败，请刷新页面后重试", status_code=403)

    if path in PUBLIC_EXACT_PATHS or path.startswith(PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    user_id = request.session.get("user_id")
    user = None
    if isinstance(user_id, int):
        with SessionLocal() as session:
            user = session.get(User, user_id)

    if user is None:
        if path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    request.state.current_user = {
        "id": user.id,
        "username": user.username,
    }
    actor_token = current_actor.set(f"{user.id}:{user.username}")
    try:
        return await call_next(request)
    finally:
        current_actor.reset(actor_token)


app.add_middleware(
    SessionMiddleware,
    secret_key=get_setting("APP_SECRET_KEY", "change-this-local-secret"),
    session_cookie="talentflow_session",
    same_site="lax",
    https_only=False,
    path="/",
    max_age=14 * 24 * 3600,
)


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 24:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def _csrf_request_valid(request: Request) -> bool:
    expected = request.session.get("csrf_token")
    provided = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
    if not isinstance(expected, str) or not expected:
        return False
    return bool(provided) and secrets.compare_digest(expected, provided)


def _csrf_form_valid(request: Request, csrf_token: str) -> bool:
    expected = request.session.get("csrf_token")
    if not isinstance(expected, str) or not expected:
        return False
    return bool(csrf_token) and secrets.compare_digest(expected, csrf_token)


def _ensure_csrf_form(request: Request, csrf_token: str) -> None:
    if not _csrf_form_valid(request, csrf_token):
        raise HTTPException(403, "CSRF 校验失败，请刷新页面后重试")


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _latest(items: list[Any]) -> Any | None:
    return max(items, key=lambda item: item.created_at) if items else None


def _application_row(application: Application) -> dict[str, Any]:
    evaluation = _latest(application.evaluations)
    attempt = max(application.evaluation_attempts, key=lambda a: a.id) if application.evaluation_attempts else None
    interview = max(application.interviews, key=lambda item: item.round) if application.interviews else None
    return {
        "id": application.id,
        "candidate_id": application.candidate_id,
        "candidate_name": application.candidate.name or "待补充",
        "phone": application.candidate.phone or "未提供",
        "email": application.candidate.email or "未提供",
        "job_title": application.job.title,
        "status": application.status.value,
        "status_label": STATUS_LABELS.get(application.status.value, application.status.value),
        "owner": application.owner or "未分配",
        "channel": application.channel or "未知",
        "score": evaluation.total_score if evaluation else None,
        "evaluation_id": evaluation.id if evaluation else None,
        "evaluation_stale": evaluation.is_stale if evaluation else False,
        "evaluation_attempt": {"id": attempt.id, "status": attempt.status, "error": attempt.error} if attempt else None,
        "workflow_revision": (application.extra_data or {}).get("workflow_revision", 0),
        "exam_decisions": (application.extra_data or {}).get("exams", {}),
        "evaluation_reason": evaluation.reason if evaluation else None,
        "dimensions": evaluation.scores if evaluation else [],
        "missing_information": evaluation.missing_information if evaluation else [],
        "interview_id": interview.id if interview else None,
        "interview_time": interview.confirmed_start_at if interview else None,
        "meeting_url": interview.meeting_url if interview else None,
        "updated_at": application.updated_at,
        "created_at": application.created_at,
    }


def _application_rows(session: Session) -> list[dict[str, Any]]:
    applications = session.scalars(
        select(Application).order_by(desc(Application.updated_at))
    ).unique().all()
    return [_application_row(item) for item in applications]


def _candidate_profile(candidate: Candidate) -> dict[str, Any]:
    structured = candidate.structured_data or {}
    human_summary = structured.get("human_summary") if isinstance(structured, dict) else None
    nested_candidate = structured.get("candidate") if isinstance(structured, dict) else None
    data = nested_candidate if isinstance(nested_candidate, dict) else structured
    if not isinstance(data, dict):
        data = {}

    raw_chat_lines: list[str] = []
    for source in candidate.sources:
        for message in source.raw_chat or []:
            content = message.get("content") if isinstance(message, dict) else None
            if content:
                raw_chat_lines.append(str(content))

    if isinstance(human_summary, dict):
        headline = human_summary.get("headline")
        skills = human_summary.get("skills") or []
        education = human_summary.get("education") or []
        projects = human_summary.get("projects") or []
        internships = human_summary.get("internships") or []
        work_experience = human_summary.get("work_experience") or []
        chat_summary = human_summary.get("chat_summary")
        availability = human_summary.get("availability")
        missing_fields = human_summary.get("missing_fields") or []
        warnings = human_summary.get("warnings") or []
        note = human_summary.get("note")
    else:
        headline = data.get("summary")
        skills = data.get("skills") or []
        education = _profile_list(data.get("education"))
        projects = _profile_experiences(data.get("project_experience")) or _profile_list(data.get("projects"))
        internships = _profile_experiences(data.get("internship_experience"))
        work_experience = _profile_experiences(data.get("work_experience"))
        chat = structured.get("chat") if isinstance(structured, dict) else {}
        chat_summary = chat.get("summary") if isinstance(chat, dict) else None
        availability = chat.get("availability") if isinstance(chat, dict) else None
        quality = structured.get("quality") if isinstance(structured, dict) else {}
        missing_fields = quality.get("missing_fields") if isinstance(quality, dict) else []
        warnings = quality.get("warnings") if isinstance(quality, dict) else []
        note = data.get("note")

    if isinstance(skills, str):
        skills = [skills]
    if not headline:
        latest_resume = next((source.raw_resume for source in candidate.sources if source.raw_resume), "")
        headline = latest_resume[:240] if latest_resume else "暂无简历摘要"
    if not chat_summary:
        chat_summary = "；".join(raw_chat_lines) if raw_chat_lines else "暂无聊天记录，本地上传任务可后续补充。"

    return {
        "headline": headline,
        "skills": skills,
        "education": education,
        "projects": projects,
        "internships": internships,
        "work_experience": work_experience,
        "chat_summary": chat_summary,
        "availability": availability or "待人工确认",
        "missing_fields": missing_fields or [],
        "warnings": warnings or [],
        "note": note or "",
    }


def _profile_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                result.append(
                    " / ".join(
                        str(part)
                        for part in (
                            item.get("school"),
                            item.get("degree"),
                            item.get("major"),
                            item.get("evidence"),
                        )
                        if part
                    )
                )
        return [item for item in result if item]
    return []


def _profile_experiences(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = " / ".join(
            str(part)
            for part in (
                item.get("name"),
                item.get("company"),
                item.get("role"),
            )
            if part
        ) or "未命名经历"
        result.append(
            {
                "title": title,
                "description": item.get("description") or item.get("evidence") or "待补充描述",
                "evidence": item.get("evidence") or item.get("description") or "",
            }
        )
    return result


def _dashboard_data(session: Session) -> dict[str, Any]:
    rows = _application_rows(session)
    statuses = Counter(row["status"] for row in rows)
    channels = Counter(row["channel"] for row in rows)
    jobs = Counter(row["job_title"] for row in rows)
    total_candidates = session.scalar(select(func.count(Candidate.id))) or 0
    reached = {row["id"]: {row["status"]} for row in rows}
    for log in session.scalars(select(AuditLog).where(AuditLog.entity_type == "application", AuditLog.action == "application.status_changed")):
        if log.entity_id in reached:
            for snapshot in (log.before_data, log.after_data):
                if snapshot and snapshot.get("status"):
                    reached[log.entity_id].add(snapshot["status"])
    overdue = session.scalar(
        select(func.count(FollowUpTask.id)).where(
            FollowUpTask.status == FollowUpStatus.OPEN,
            FollowUpTask.due_at <= datetime.now(timezone.utc)
        )
    ) or 0
    total_offers = session.scalar(select(func.count(Offer.id))) or 0
    accepted_offers = session.scalar(
        select(func.count(Offer.id)).where(Offer.status == OfferStatus.ACCEPTED)
    ) or 0
    sent_offers = session.scalar(
        select(func.count(Offer.id)).where(
            Offer.status.in_([OfferStatus.SENT, OfferStatus.ACCEPTED, OfferStatus.DECLINED])
        )
    ) or 0
    onboarding_open = session.scalar(
        select(func.count(OnboardingTask.id)).where(
            OnboardingTask.status == OnboardingStatus.OPEN
        )
    ) or 0
    notification_failed = session.scalar(
        select(func.count(Notification.id)).where(Notification.status == NotificationStatus.FAILED)
    ) or 0
    funnel = {
        "applications": len(rows),
        "screened": sum(bool(states & {"deduplicated", "evaluating", "evaluation_review"}) for states in reached.values()),
        "assessments": sum("assessment_pending" in states for states in reached.values()),
        "written": sum("written_pending" in states for states in reached.values()),
        "interviews": sum(bool(states & {"interview_pending", "scheduling", "interview_confirmed", "interviewed", "feedback_pending", "next_round"}) for states in reached.values()),
        "offers": sum(bool(states & {"offer_pending", "offer_sent", "offer_accepted", "offer_declined"}) for states in reached.values()),
        "onboarded": statuses.get("joined", 0),
    }
    exam_counts = {}
    later_stages = {"interview_pending", "scheduling", "interview_confirmed", "interviewed", "feedback_pending", "next_round", "offer_pending", "offer_sent", "offer_accepted", "offer_declined", "onboarding", "joined", "onboarding_withdrawn"}
    for kind in ("assessment", "written"):
        sent = {d.application_id for d in session.scalars(select(ExamDispatch).where(ExamDispatch.kind == kind))
                if (n := exams.dispatch_notification(session, d.id)) and n.status in exams.DELIVERED}
        exam_counts[kind] = {
            "sent": len(sent),
            **{result: sum(row["exam_decisions"].get(kind, {}).get("result") == result for row in rows) for result in ("passed", "failed", "skipped")},
            "historical_unrecorded": sum(not row["exam_decisions"].get(kind) and row["status"] in later_stages for row in rows),
        }
    return {
        "exam_counts": exam_counts,
        "total_candidates": total_candidates,
        "new_today": sum(
            bool(row["created_at"] and row["created_at"].replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date() == datetime.now(ZoneInfo("Asia/Shanghai")).date())
            for row in rows
        ),
        "pending_review": statuses.get("evaluation_review", 0) + statuses.get("needs_review", 0),
        "pending_schedule": statuses.get("interview_pending", 0) + statuses.get("scheduling", 0),
        "pending_feedback": statuses.get("feedback_pending", 0),
        "overdue": overdue,
        "total_offers": total_offers,
        "accepted_offers": accepted_offers,
        "offer_acceptance_rate": round(accepted_offers * 100 / sent_offers, 1) if sent_offers else 0,
        "offer_conversion_rate": round(total_offers * 100 / len(rows), 1) if rows else 0,
        "onboarding_open": onboarding_open,
        "joined_rate": round(statuses.get("joined", 0) * 100 / accepted_offers, 1) if accepted_offers else 0,
        "notification_failed": notification_failed,
        "funnel": funnel,
        "status_counts": dict(statuses),
        "channels": [{"name": name, "count": count} for name, count in channels.most_common()],
        "jobs": [{"name": name, "count": count} for name, count in jobs.most_common()],
        **funnel,
    }


def _offer_detail(session: Session, offer: Offer | None) -> dict[str, Any] | None:
    if offer is None:
        return None
    notifications = session.scalars(
        select(Notification)
        .where(
            Notification.target_type == "offer",
            Notification.target_id == offer.id,
        )
        .order_by(desc(Notification.created_at))
        .limit(20)
    ).all()
    audits = session.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "offer", AuditLog.entity_id == offer.id)
        .order_by(desc(AuditLog.created_at))
        .limit(20)
    ).all()
    pending_terms = missing_offer_terms(offer)
    approval_summary = (
        f"{offer.candidate_name} 已完成面试反馈流程，拟录用岗位为 {offer.job_title}。"
        f"当前条款版本：{(offer.terms or {}).get('revision', 0)}，修改后需重新审批。"
    )
    return {
        "pending_terms": pending_terms,
        "approval_summary": approval_summary,
        "email_draft": offer.content,
        "notifications": notifications,
        "audits": audits,
    }


def _split_lines(value: str) -> list[str]:
    return [item.strip() for item in value.replace("；", "\n").splitlines() if item.strip()]


def _mask_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "****"


def _mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return ""
    name, domain = value.split("@", 1)
    return f"{name[:2]}***@{domain}"


def _candidate_manual_snapshot(data: dict[str, Any] | None) -> dict[str, Any]:
    source = data or {}
    human_summary = source.get("human_summary") if isinstance(source, dict) else {}
    if not isinstance(human_summary, dict):
        human_summary = {}
    return {
        "skills_count": len(human_summary.get("skills") or []),
        "projects_count": len(human_summary.get("projects") or []),
        "chat_summary_configured": bool(human_summary.get("chat_summary")),
        "note_configured": bool(human_summary.get("note")),
        "manual_supplement": source.get("manual_supplement") if isinstance(source, dict) else None,
    }


def _base_context(
    request: Request, session: Session, active_page: str
) -> dict[str, Any]:
    summary = _dashboard_data(session)
    return {
        "active_page": active_page,
        "pending_evaluations": summary["pending_review"],
        "overdue_count": summary["overdue"],
        "notification_count": summary["overdue"] + summary["notification_failed"],
        "current_user": getattr(
            request.state,
            "current_user",
            {"username": "管理员"},
        ),
        "csrf_token": _csrf_token(request),
        "status_labels": STATUS_LABELS,
    }


def _render(
    request: Request,
    session: Session,
    template: str,
    active_page: str,
    **context: Any,
):
    merged = _base_context(request, session, active_page)
    merged.update(context)
    return templates.TemplateResponse(request=request, name=template, context=merged)


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "registered": request.query_params.get("registered") == "1",
            "csrf_token": _csrf_token(request),
        },
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    session: Session = Depends(get_db),
):
    _ensure_csrf_form(request, csrf_token)
    if _login_rate_limited(request, username):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "登录尝试过多，请稍后再试",
                "username": username.strip(),
                "registered": False,
                "csrf_token": _csrf_token(request),
            },
            status_code=429,
        )
    user = session.scalar(
        select(User).where(
            User.normalized_username == normalize_username(username)
        )
    )
    if user is None or not verify_password(password, user.password_hash):
        _record_login_failure(request, username)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "用户名或密码错误",
                "username": username.strip(),
                "registered": False,
                "csrf_token": _csrf_token(request),
            },
            status_code=401,
        )
    _clear_login_failures(request, username)
    request.session.clear()
    request.session["user_id"] = user.id
    _csrf_token(request)
    return RedirectResponse("/", status_code=303)


@app.get("/register")
def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"csrf_token": _csrf_token(request)},
    )


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    session: Session = Depends(get_db),
):
    _ensure_csrf_form(request, csrf_token)
    display_username = username.strip()
    normalized_username = normalize_username(username)
    error = None
    if len(display_username) < 3:
        error = "用户名至少需要 3 个字符"
    elif len(display_username) > 100:
        error = "用户名不能超过 100 个字符"
    elif len(password) < 6:
        error = "密码至少需要 6 个字符"

    if error:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": error,
                "username": display_username,
                "csrf_token": _csrf_token(request),
            },
            status_code=422,
        )

    existing_user = session.scalar(
        select(User).where(User.normalized_username == normalized_username)
    )
    if existing_user is not None:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "该用户名已被注册",
                "username": display_username,
                "csrf_token": _csrf_token(request),
            },
            status_code=409,
        )

    session.add(
        User(
            username=display_username,
            normalized_username=normalized_username,
            password_hash=hash_password(password),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "该用户名已被注册",
                "username": display_username,
                "csrf_token": _csrf_token(request),
            },
            status_code=409,
        )
    return RedirectResponse("/login?registered=1", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    _ensure_csrf_form(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _login_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "local"
    return f"{host}:{normalize_username(username)}"


def _recent_login_failures(key: str) -> list[float]:
    now = time.time()
    attempts = [
        timestamp
        for timestamp in LOGIN_ATTEMPTS.get(key, [])
        if now - timestamp <= LOGIN_WINDOW_SECONDS
    ]
    LOGIN_ATTEMPTS[key] = attempts
    return attempts


def _login_rate_limited(request: Request, username: str) -> bool:
    return len(_recent_login_failures(_login_key(request, username))) >= LOGIN_MAX_FAILURES


def _record_login_failure(request: Request, username: str) -> None:
    key = _login_key(request, username)
    attempts = _recent_login_failures(key)
    attempts.append(time.time())
    LOGIN_ATTEMPTS[key] = attempts


def _clear_login_failures(request: Request, username: str) -> None:
    LOGIN_ATTEMPTS.pop(_login_key(request, username), None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": get_setting("LLM_PROVIDER", "mock")}


@app.get("/")
def workbench(request: Request, session: Session = Depends(get_db)):
    summary = _dashboard_data(session)
    rows = _application_rows(session)
    tasks = session.scalars(
        select(FollowUpTask)
        .where(FollowUpTask.status == FollowUpStatus.OPEN)
        .order_by(FollowUpTask.due_at)
        .limit(6)
    ).all()
    audits = session.scalars(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(8)
    ).all()
    return _render(
        request,
        session,
        "workbench.html",
        "workbench",
        summary=summary,
        rows=rows[:8],
        tasks=tasks,
        audits=audits,
        **build_workbench_context(session, rows, summary),
    )


@app.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        session,
        "dashboard.html",
        "dashboard",
        summary=_dashboard_data(session),
    )


@app.get("/candidates")
def candidates_page(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        session,
        "candidates.html",
        "candidates",
        rows=_application_rows(session),
    )


@app.get("/candidates/{candidate_id}")
def candidate_detail(
    candidate_id: int, request: Request, session: Session = Depends(get_db)
):
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选人不存在")
    rows = [_application_row(item) for item in candidate.applications]
    profile = _candidate_profile(candidate)
    selected = next((a for a in candidate.applications if str(a.id) == request.query_params.get("application_id")), None) or next(iter(candidate.applications), None)
    if selected:
        profile["chat_summary"] = (selected.extra_data or {}).get("manual_chat", "\n".join(str(m.get("content", "")) for link in selected.source_links for m in (link.source.raw_chat or [])))
    audits = session.scalars(
        select(AuditLog)
        .where(
            (AuditLog.entity_type == "candidate") & (AuditLog.entity_id == candidate_id)
            | ((AuditLog.entity_type == "application") & AuditLog.entity_id.in_([a.id for a in candidate.applications]))
        )
        .order_by(desc(AuditLog.created_at))
        .limit(20)
    ).all()
    return _render(
        request,
        session,
        "candidate_detail.html",
        "candidate_detail",
        candidate=candidate,
        profile=profile,
        rows=rows,
        audits=audits,
        evaluation_history=sorted([e for a in candidate.applications for e in a.evaluations], key=lambda e: e.id, reverse=True),
    )


@app.post("/api/candidates/{candidate_id}/supplement")
def supplement_candidate_api(
    candidate_id: int,
    name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    skills: str | None = Form(None),
    experience: str | None = Form(None),
    chat_summary: str | None = Form(None),
    note: str | None = Form(None),
    version: int | None = Form(None),
    application_id: int | None = Form(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选人不存在")
    changes = {key: value for key, value in {
        "name": name, "phone": phone, "email": email, "skills": skills,
        "experience": experience, "chat_summary": chat_summary, "note": note,
    }.items() if value is not None}
    try:
        revise_candidate(session, candidate, changes, version=version, application_id=application_id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "message": "资料已保存；受影响的旧评估已标记过期", "revision": candidate.structured_data["revision"]}


@app.post("/api/candidates/{candidate_id}/profile")
def update_candidate_profile_api(
    candidate_id: int,
    name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    skills: str | None = Form(None),
    experience: str | None = Form(None),
    chat_summary: str | None = Form(None),
    note: str | None = Form(None),
    version: int | None = Form(None),
    application_id: int | None = Form(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return supplement_candidate_api(
        candidate_id=candidate_id,
        name=name,
        phone=phone,
        email=email,
        skills=skills,
        experience=experience,
        chat_summary=chat_summary,
        note=note,
        version=version,
        application_id=application_id,
        session=session,
    )


@app.post("/api/candidates/{candidate_id}/delete")
def delete_candidate_api(
    candidate_id: int,
    confirm: str = Form(""),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _delete_candidate(candidate_id, confirm, session)


@app.delete("/api/candidates/{candidate_id}")
def delete_candidate_rest_api(
    candidate_id: int,
    confirm: str = "DELETE",
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _delete_candidate(candidate_id, confirm, session)


def _delete_candidate(
    candidate_id: int,
    confirm: str,
    session: Session,
) -> dict[str, Any]:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选人不存在")
    if confirm.strip() != "DELETE":
        raise HTTPException(422, "请输入 DELETE 确认删除")
    write_audit_log(
        session,
        actor="本地管理员",
        action="candidate.deleted",
        entity_type="candidate",
        entity_id=candidate.id,
        before_data={
            "name": candidate.name,
            "phone": _mask_phone(candidate.phone),
            "email": _mask_email(candidate.email),
            "application_count": len(candidate.applications),
        },
    )
    session.delete(candidate)
    session.commit()
    return {"ok": True, "message": "候选人数据已删除", "redirect": "/candidates"}


@app.get("/import")
def import_page(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        session,
        "import.html",
        "import",
        import_tasks=import_task_rows(session),
        jobs=session.scalars(select(Job).order_by(Job.id)).all(),
    )


@app.get("/mock-platform")
def mock_platform_page(request: Request, session: Session = Depends(get_db)):
    return _render(request, session, "mock_platform.html", "mock_platform")


@app.get("/evaluations")
def evaluations_page(request: Request, session: Session = Depends(get_db)):
    rows = [
        row
        for row in _application_rows(session)
        if row["evaluation_id"] or row["status"] in {"captured", "parsed", "deduplicated", "evaluation_review"}
    ]
    return _render(request, session, "evaluations.html", "evaluations", rows=rows)


@app.get("/interviews")
def interviews_page(request: Request, session: Session = Depends(get_db)):
    interviews = session.scalars(
        select(Interview).order_by(desc(Interview.created_at))
    ).unique().all()
    base_url = str(request.base_url).rstrip("/")
    booking_urls = {
        item.id: f"{base_url}/schedule/{item.booking_token}"
        for item in interviews
        if item.booking_token
    }
    return _render(
        request,
        session,
        "interviews.html",
        "interviews",
        interviews=interviews,
        booking_urls=booking_urls,
    )


@app.get("/offers")
def offers_page(
    request: Request,
    offer_id: int | None = None,
    session: Session = Depends(get_db),
):
    offers = list_offers(session)
    selected_offer = (
        next((item for item in offers if item.id == offer_id), None)
        if offer_id
        else (offers[0] if offers else None)
    )
    return _render(
        request,
        session,
        "offers.html",
        "offers",
        offers=offers,
        selected_offer=selected_offer,
        offer_detail=_offer_detail(session, selected_offer) if selected_offer else None,
    )


@app.get("/onboarding")
def onboarding_page(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        session,
        "onboarding.html",
        "onboarding",
        tasks=list_onboarding_tasks(session),
        applications=session.scalars(select(Application).where(Application.status.in_([ApplicationStatus.ONBOARDING, ApplicationStatus.JOINED, ApplicationStatus.ONBOARDING_WITHDRAWN]))).all(),
    )


@app.get("/notifications")
def notifications_page(request: Request, session: Session = Depends(get_db)):
    notifications = session.scalars(
        select(Notification).order_by(desc(Notification.created_at)).limit(200)
    ).all()
    exports = session.scalars(
        select(ExportRun).order_by(desc(ExportRun.created_at)).limit(20)
    ).all()
    return _render(
        request,
        session,
        "notifications.html",
        "notifications",
        notifications=notifications,
        exports=exports,
    )


@app.get("/follow-ups")
def followups_page(request: Request, session: Session = Depends(get_db)):
    tasks = session.scalars(
        select(FollowUpTask).order_by(FollowUpTask.status, FollowUpTask.due_at)
    ).all()
    return _render(
        request, session, "follow_ups.html", "follow_ups", tasks=tasks
    )


@app.get("/audit")
def audit_page(request: Request, session: Session = Depends(get_db)):
    logs = session.scalars(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(200)
    ).all()
    return _render(request, session, "audit.html", "audit", logs=logs)


@app.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        session,
        "settings.html",
        "settings",
        settings=public_llm_settings(),
        integrations=public_integration_settings(),
    )


@app.post("/settings")
def save_settings_page(
    request: Request,
    provider: str = Form(...),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_db),
):
    _ensure_csrf_form(request, csrf_token)
    _save_settings(provider, base_url, model, api_key, session)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/llm/clear")
def clear_llm_settings_page(
    request: Request,
    csrf_token: str = Form(""),
    session: Session = Depends(get_db),
):
    _ensure_csrf_form(request, csrf_token)
    _clear_llm_settings(session)
    return RedirectResponse("/settings?api_key_cleared=1#model", status_code=303)


@app.post("/settings/wecom")
def save_wecom_settings_page(
    request: Request,
    webhook_url: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_db),
):
    _ensure_csrf_form(request, csrf_token)
    save_app_setting("WECOM_WEBHOOK_URL", webhook_url.strip())
    write_audit_log(
        session,
        actor="本地管理员",
        action="settings.wecom_updated",
        entity_type="settings",
        after_data={"wecom_webhook_configured": bool(webhook_url.strip())},
    )
    session.commit()
    return RedirectResponse("/settings?wecom_saved=1#wecom", status_code=303)


@app.get("/api/settings/llm")
def get_llm_settings_api() -> dict[str, Any]:
    return public_llm_settings()


@app.post("/api/settings/llm")
def save_llm_settings_api(
    provider: str = Form(...),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _save_settings(provider, base_url, model, api_key, session)


@app.post("/api/settings/llm/clear")
def clear_llm_settings_api(session: Session = Depends(get_db)) -> dict[str, Any]:
    settings = _clear_llm_settings(session)
    return {"ok": True, "message": "API Key 已清空，当前回退 Mock 模式", **settings}


@app.post("/api/settings/llm/clear-key")
def clear_llm_settings_key_api(session: Session = Depends(get_db)) -> dict[str, Any]:
    return clear_llm_settings_api(session)


def _clear_llm_settings(session: Session) -> dict[str, Any]:
    clear_llm_api_key()
    write_audit_log(
        session,
        actor="本地管理员",
        action="settings.llm_api_key_cleared",
        entity_type="settings",
        after_data={"provider": "mock", "api_key_configured": False},
    )
    session.commit()
    return public_llm_settings()


def _save_settings(
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    session: Session,
) -> dict[str, Any]:
    normalized_provider = provider.strip().lower()
    if normalized_provider not in {"mock", "openai_compatible", "openai-compatible", "openai"}:
        raise HTTPException(422, "不支持的模型提供方")
    save_llm_settings(
        provider=normalized_provider,
        base_url=base_url.strip(),
        model=model.strip(),
        api_key=api_key.strip() or None,
    )
    write_audit_log(
        session,
        actor="本地管理员",
        action="settings.llm_updated",
        entity_type="settings",
        after_data={
            "provider": normalized_provider,
            "base_url": base_url.strip(),
            "model": model.strip(),
            "api_key_configured": public_llm_settings()["api_key_configured"],
        },
    )
    session.commit()
    return public_llm_settings()


def _capture_mock_recruitment_data(session: Session) -> dict[str, Any]:
    summary = seed_database(session)
    session.commit()
    return {
        "ok": True,
        "message": "虚构候选人简历、初步聊天和岗位 JD 已采集",
        "candidate_count": session.scalar(select(func.count(Candidate.id))) or 0,
        "application_count": session.scalar(select(func.count(Application.id))) or 0,
        **summary,
    }


@app.post("/api/import/mock")
def import_mock(session: Session = Depends(get_db)) -> dict[str, Any]:
    return _capture_mock_recruitment_data(session)


@app.post("/api/import/files")
async def upload_import_files(
    request: Request,
    files: list[UploadFile] = File(...),
    job_id: int | None = Form(None),
    job_title: str = Form(""),
    jd_text: str = Form(""),
    channel: str = Form("本地上传"),
    owner: str = Form("HR"),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    tasks, errors = [], []
    for file in files:
        stored = None
        try:
            stored = store_upload_bytes(
                original_filename=file.filename or "resume.txt",
                content=await file.read(int(get_setting("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))) + 1),
                content_type=file.content_type,
            )
            task = create_file_import_task(
                session, uploads=[stored], operator_id=request.session.get("user_id"),
                job_title=job_title, jd_text=jd_text, channel=channel, owner=owner, job_id=job_id,
            )
            session.commit()
            tasks.append(serialize_import_task(task))
        except (ValueError, OSError) as exc:
            session.rollback()
            if stored:
                stored.file_path.unlink(missing_ok=True)
            errors.append({"filename": file.filename, "error": str(exc)})
        finally:
            await file.close()
    if not tasks:
        return JSONResponse({"ok": False, "tasks": [], "errors": errors, "detail": "没有文件成功上传"}, status_code=422)
    return {"ok": not errors, "message": f"已创建 {len(tasks)} 个独立任务，失败 {len(errors)} 个", "tasks": tasks, "errors": errors, "task": tasks[0]}


@app.get("/api/import/tasks")
def list_import_tasks(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return import_task_rows(session)


@app.get("/api/import/tasks/{task_id}")
def get_import_task(task_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
    from app.models import ImportTask

    task = session.get(ImportTask, task_id)
    if task is None:
        raise HTTPException(404, "导入任务不存在")
    return serialize_import_task(task)


@app.post("/api/import/tasks/{task_id}/parse")
def parse_import_task_api(
    task_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        task = parse_import_task(session, task_id)
        session.commit()
        return {
            "ok": task.error_message is None,
            "message": task.error_message or "解析完成，请核对预览后确认入库。",
            "task": serialize_import_task(task),
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/import/tasks/{task_id}/supplement")
async def supplement_import_api(task_id: int, request: Request, session: Session = Depends(get_db)):
    form = dict(await request.form())
    try:
        version = int(form.pop("version"))
        task = supplement_import_task(session, task_id, form, version=version)
        session.commit()
        return {"ok": True, "task": serialize_import_task(task), "message": "预览已保存，请核对并确认入库"}
    except (KeyError, ValueError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/import/tasks/{task_id}/confirm")
def confirm_import_task_api(
    task_id: int,
    start_evaluation: bool = Form(False),
    version: int | None = Form(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        application = confirm_import_task(
            session,
            task_id,
            start_evaluation=start_evaluation,
            version=version,
        )
        session.commit()
        evaluation_result, evaluation_error = None, None
        if start_evaluation:
            try:
                evaluation_result = _evaluate_application_record(session, application)
            except ValueError as exc:
                evaluation_error = str(exc)
        return {
            "ok": True,
            "message": "已确认入库，候选人和投递记录已创建。"
            + ("AI 评估已完成，等待人工审核。" if evaluation_result else evaluation_error or ""),
            "candidate_id": application.candidate_id,
            "application_id": application.id,
            "evaluation": evaluation_result,
            "evaluation_error": evaluation_error,
            "candidate_url": f"/candidates/{application.candidate_id}",
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/import/tasks/{task_id}/reject")
def reject_import_task_api(
    task_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        task = reject_import_task(session, task_id)
        session.commit()
        return {
            "ok": True,
            "message": "导入任务已废弃，不会写入候选人库。",
            "task": serialize_import_task(task),
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/connectors/mock/capture")
def capture_mock_connector(session: Session = Depends(get_db)) -> dict[str, Any]:
    return _capture_mock_recruitment_data(session)


@app.get("/api/candidates")
def candidates_api(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _application_rows(session)


@app.get("/api/candidates/{candidate_id}")
def candidate_api(candidate_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选人不存在")
    return {
        "id": candidate.id,
        "name": candidate.name,
        "phone": candidate.phone,
        "email": candidate.email,
        "dedup_status": candidate.dedup_status.value,
        "structured_data": candidate.structured_data,
        "applications": [_application_row(item) for item in candidate.applications],
    }


from app.services.evaluations import evaluate_application_record as _evaluate_application_record


@app.post("/api/applications/{application_id}/parse")
def parse_application(
    application_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, "投递记录不存在")
    source = application.source
    raw_chat = "\n".join(
        str(item.get("content", "")) for item in (source.raw_chat or [])
    ) if source else ""
    # 已确认资料仅重新展示；解析不能覆盖人工事实或倒退招聘阶段。
    structured = application.candidate.structured_data
    if not structured:
        structured = build_rule_parse(source.raw_resume if source else "", job_title=application.job.title, jd_text=application.job_version.jd_raw)
        application.candidate.structured_data = structured
    write_audit_log(
        session,
        actor="解析Agent",
        action="application.parsed",
        entity_type="application",
        entity_id=application.id,
        details={"provider": structured.get("ai_meta", {}).get("provider")},
    )
    session.commit()
    return {"ok": True, "application_id": application.id, "structured": structured}


@app.post("/api/applications/{application_id}/evaluate")
def evaluate_application(
    application_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, "投递记录不存在")
    try:
        evaluation = _evaluate_application_record(session, application)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    session.commit()
    return {
        "ok": True,
        "application_id": application.id,
        **evaluation,
    }


@app.post("/api/applications/{application_id}/decision")
def decide_application(
    application_id: int,
    status: str = Form(...),
    reason: str = Form(""),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        if status not in {"assessment_pending", "on_hold", "rejected"}:
            raise ValueError("请通过当前阶段的专用操作推进，不能跳过测评、笔试或入职确认")
        current = lock_record(session, Application, application_id)
        if current.status not in {ApplicationStatus.EVALUATION_REVIEW, ApplicationStatus.ON_HOLD}:
            raise ValueError("通用初筛决定仅用于评估审核或暂缓阶段，请使用当前阶段的专用操作")
        application = transition_application(
            session,
            application_id,
            ApplicationStatus(status),
            actor="本地管理员",
            is_human_action=True,
            reason=reason or "HR 人工决定",
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, HumanActionRequired) as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"ok": True, "status": application.status.value}


@app.post("/api/applications/{application_id}/schedule")
def schedule_application(
    application_id: int,
    request: Request,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        application = lock_record(session, Application, application_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if application.status in {ApplicationStatus.INTERVIEW_PENDING, ApplicationStatus.NEXT_ROUND}:
        transition_application(
            session,
            application.id,
            ApplicationStatus.SCHEDULING,
            actor="排期Agent",
        )
    if application.status != ApplicationStatus.SCHEDULING:
        raise HTTPException(409, "当前状态不允许排期，请先由 HR 选择进入面试")

    interview = max(
        (
            item
            for item in application.interviews
            if item.status in {InterviewStatus.DRAFT, InterviewStatus.INVITED}
            and item.confirmed_start_at is None
        ),
        key=lambda item: item.created_at,
        default=None,
    )
    if interview is None:
        token = create_booking_token(
            {"application_id": application.id},
            get_setting("APP_SECRET_KEY", "change-this-local-secret"),
        )
        interview, _ = create_interview(
            session,
            application.id,
            InterviewCreate(
                round=max((i.round for i in application.interviews), default=0) + 1,
                interviewers=[
                    {"name": "虚构面试官-陈老师", "email": "interviewer@example.test"}
                ],
                booking_token=token,
                booking_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
            ),
            actor="排期Agent",
        )
    else:
        token = interview.booking_token
        expired = not interview.booking_token_expires_at or interview.booking_token_expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
        if token:
            try:
                verify_booking_token(token, get_setting("APP_SECRET_KEY", "change-this-local-secret"))
            except BookingTokenError:
                expired = True
        if token is None or expired:
            token = create_booking_token(
                {"application_id": application.id},
                get_setting("APP_SECRET_KEY", "change-this-local-secret"),
            )
            interview.booking_token = token
            interview.booking_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=48)
            for old_slot in interview.slots:
                old_slot.status = InterviewSlotStatus.RELEASED
            write_audit_log(session, actor="HR", action="interview.booking_reissued", entity_type="interview", entity_id=interview.id)
    busy = [
        {"start_at": item.confirmed_start_at, "end_at": item.confirmed_end_at}
        for item in session.scalars(
            select(Interview).where(Interview.confirmed_start_at.is_not(None))
        ).all()
    ]
    available = [s for s in interview.slots if s.status == InterviewSlotStatus.AVAILABLE and appointment_time(s.start_at) > datetime.now(timezone.utc)]
    slots = [] if available else generate_candidate_slots(
        datetime.now(timezone.utc) + timedelta(hours=12),
        datetime.now(timezone.utc) + timedelta(days=7),
        busy,
        limit=5,
        minimum_notice_minutes=60,
    )
    for slot in slots:
        saved_slot, _ = add_interview_slot(
            session,
            interview.id,
            InterviewSlotCreate(
                start_at=slot["start_at"], end_at=slot["end_at"]
            ),
            actor="排期Agent",
        )
        if saved_slot.status == InterviewSlotStatus.RELEASED:
            saved_slot.status = InterviewSlotStatus.AVAILABLE
    interview.status = InterviewStatus.INVITED
    session.commit()
    session.expire(interview, ["slots"])
    return {
        "ok": True,
        "interview_id": interview.id,
        "booking_url": public_url(f"/schedule/{token}"),
        "slots": [
            {
                "id": item.id,
                "start_at": item.start_at.isoformat(),
                "end_at": item.end_at.isoformat(),
            }
            for item in interview.slots if item.status == InterviewSlotStatus.AVAILABLE
        ],
    }


@app.post("/api/interviews/{interview_id}/feedback")
def submit_interview_feedback_api(
    interview_id: int,
    result: str = Form("通过"),
    comment: str = Form(""),
    next_action: str = Form("offer"),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        payload = submit_interview_feedback(
            session,
            interview_id,
            result=result,
            comment=comment,
            next_action=next_action,
        )
        session.commit()
        return {"ok": True, **payload}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/interviews/{interview_id}/send-invite")
def send_interview_invite_api(
    interview_id: int,
    request: Request,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    interview = session.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(404, "面试记录不存在")
    if not interview.booking_token:
        raise HTTPException(409, "当前面试没有可发送的预约链接")
    if interview.status not in {InterviewStatus.DRAFT, InterviewStatus.INVITED} or not interview.booking_token_expires_at or interview.booking_token_expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(409, "预约已确认或链接已过期，请在原轮次重新生成时段")
    candidate = interview.application.candidate
    if not candidate.email:
        raise HTTPException(422, "候选人邮箱缺失，请先在候选人详情补充邮箱")
    booking_url = public_url(f"/schedule/{interview.booking_token}")
    result = send_email_notification(
        session,
        recipient=candidate.email,
        subject=f"{interview.application.job.title} 面试时间确认",
        content=(
            f"{candidate.name or '同学'}，你好。\n\n"
            "请打开以下链接选择一个面试时间。确认后系统会锁定时段并创建 Mock 腾讯会议。\n\n"
            f"{booking_url}\n\n"
            "如时间不合适，请联系招聘负责人。"
        ),
        target_type="interview",
        target_id=interview.id,
        template="interview_invite_" + interview.booking_token[-24:],
    )
    notify_internal(
        session,
        title="面试预约链接已生成",
        content=f"{candidate.name or '候选人'} 的预约链接已发送/记录：{booking_url}",
        target_type="interview",
        target_id=interview.id,
    )
    write_audit_log(
        session,
        actor="排期Agent",
        action="interview.invite_sent",
        entity_type="interview",
        entity_id=interview.id,
        after_data={
            "email": _mask_email(candidate.email),
            "notification_status": result.status.value,
            "booking_url": booking_url,
        },
    )
    session.commit()
    return {
        **notification_result(result),
        "booking_url": booking_url,
    }


@app.post("/api/applications/{application_id}/offer")
def create_offer_api(
    application_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        application = session.get(Application, application_id)
        if application is None:
            raise ValueError("投递记录不存在")
        if application.status == ApplicationStatus.FEEDBACK_PENDING:
            transition_application(
                session,
                application.id,
                ApplicationStatus.OFFER_PENDING,
                actor="本地管理员",
                is_human_action=True,
                reason="HR 手动创建 Offer",
            )
        offer = create_offer(session, application_id)
        session.commit()
        return {"ok": True, "offer_id": offer.id, "status": offer.status.value}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/offers/{offer_id}/approve")
def approve_offer_api(
    offer_id: int,
    reason: str = Form("单级审批通过"),
    version: int = Form(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        offer = approve_offer(session, offer_id, reason=reason, version=version)
        session.commit()
        return {"ok": True, "offer_id": offer.id, "status": offer.status.value}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/offers/{offer_id}/generate-draft")
def generate_offer_draft_api(
    offer_id: int,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, "Offer 不存在")
    detail = _offer_detail(session, offer)
    write_audit_log(
        session,
        actor="OfferAgent",
        action="offer.draft_generated",
        entity_type="offer",
        entity_id=offer.id,
        after_data={
            "approval_summary": detail["approval_summary"] if detail else "",
            "pending_terms": detail["pending_terms"] if detail else [],
        },
    )
    session.commit()
    return {
        "ok": True,
        "message": "Offer 草稿和审批摘要已生成，敏感条款仍待 HR 填写",
        "approval_summary": detail["approval_summary"] if detail else "",
        "pending_terms": detail["pending_terms"] if detail else [],
    }


@app.post("/api/offers/{offer_id}/send")
def send_offer_api(
    offer_id: int, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        offer = send_offer(session, offer_id)
        session.commit()
        notification = session.scalar(select(Notification).where(Notification.target_type == "offer", Notification.target_id == offer.id, Notification.channel == "email").order_by(Notification.id.desc()))
        return {**(notification_result(notification) if notification else {"ok": True}),
                "offer_id": offer.id, "status": offer.status.value, "candidate_email": offer.candidate_email,
                "offer_url": public_url(f"/offer/{offer.candidate_token}")}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/offers")
def offers_api(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    offers = list_offers(session, limit=200)
    return [
        {
            "id": offer.id,
            "application_id": offer.application_id,
            "status": offer.status.value,
            "candidate_name": offer.candidate_name,
            "candidate_email": offer.candidate_email,
            "job_title": offer.job_title,
            "sent_at": offer.sent_at.isoformat() if offer.sent_at else None,
            "candidate_token": offer.candidate_token,
            "created_at": offer.created_at.isoformat(),
        }
        for offer in offers
    ]


@app.get("/offer/{token}")
def public_offer_page(token: str, request: Request, session: Session = Depends(get_db)):
    offer = session.scalar(select(Offer).where(Offer.candidate_token == token))
    if offer is None:
        raise HTTPException(404, "Offer 链接不存在")
    return templates.TemplateResponse(
        request=request,
        name="offer_public.html",
        context={"offer": offer, "token": token},
    )


@app.post("/offer/{token}/respond")
def respond_offer_page(
    token: str,
    accepted: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db),
):
    try:
        offer = respond_offer(
            session,
            token,
            accepted=accepted == "true",
            note=note,
        )
        session.commit()
        return RedirectResponse(
            f"/offer/{token}?responded={offer.status.value}", status_code=303
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/onboarding/{task_id}/complete")
def complete_onboarding_task_api(
    task_id: int,
    note: str = Form("HR 确认完成"),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        task = confirm_onboarding_task(session, task_id, note=note)
        session.commit()
        return {"ok": True, "task_id": task.id, "status": task.status.value}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/onboarding")
def onboarding_api(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    tasks = list_onboarding_tasks(session, limit=200)
    return [
        {
            "id": task.id,
            "offer_id": task.offer_id,
            "application_id": task.offer.application_id if task.offer else None,
            "candidate_name": task.offer.candidate_name if task.offer else "",
            "title": task.title,
            "status": task.status.value,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }
        for task in tasks
    ]


@app.post("/api/sync/tencent-docs")
def sync_tencent_docs_api(session: Session = Depends(get_db)) -> dict[str, Any]:
    rows = _export_rows(session)
    run = record_export_run(
        session,
        export_type="tencent_docs",
        status=ExportRunStatus.MOCKED,
        row_count=len(rows),
        sync_target="腾讯文档 Mock",
        message="未配置腾讯文档开放能力，已保留 XLSX/CSV 稳定出口。",
    )
    notify_internal(
        session,
        title="腾讯文档同步 Mock",
        content=f"已生成腾讯文档兼容数据视图，共 {len(rows)} 条记录；真实同步待接入。",
        target_type="export_run",
        target_id=run.id,
    )
    session.commit()
    return {"ok": True, "run_id": run.id, "status": run.status.value, "message": run.message}


@app.get("/schedule/{token}")
def booking_page(token: str, request: Request, session: Session = Depends(get_db)):
    try:
        payload = verify_booking_token(
            token, get_setting("APP_SECRET_KEY", "change-this-local-secret")
        )
    except BookingTokenError as exc:
        return templates.TemplateResponse(
            request=request,
            name="booking.html",
            context={"error": str(exc), "slots": [], "token": token},
            status_code=400,
        )
    interview = session.scalar(select(Interview).where(Interview.booking_token == token))
    if interview is None or payload.get("application_id") != interview.application_id:
        raise HTTPException(404, "预约不存在或已完成")
    if not interview.booking_token_expires_at or interview.booking_token_expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(410, "预约已过期，请联系 HR 在原轮次重签")
    return templates.TemplateResponse(
        request=request,
        name="booking.html",
        context={
            "token": token,
            "interview": interview,
            "application": interview.application,
            "slots": [
                item for item in interview.slots if item.status == InterviewSlotStatus.AVAILABLE
            ],
        },
    )


@app.post("/schedule/{token}/confirm")
def confirm_booking(
    token: str,
    request: Request,
    slot_id: int = Form(...),
    session: Session = Depends(get_db),
):
    # 所有预约争用同一 SQLite 写锁，再重新检查全局时段占用。
    from sqlalchemy import update
    session.execute(update(Interview).where(Interview.id == -1).values(id=Interview.id))
    try:
        verify_booking_token(
            token, get_setting("APP_SECRET_KEY", "change-this-local-secret")
        )
    except BookingTokenError as exc:
        raise HTTPException(400, str(exc)) from exc
    interview = session.scalar(select(Interview).where(Interview.booking_token == token))
    slot = session.get(InterviewSlot, slot_id)
    if interview is None or slot is None or slot.interview_id != interview.id:
        raise HTTPException(404, "预约时段不存在")
    if interview.confirmed_start_at or interview.status not in {InterviewStatus.DRAFT, InterviewStatus.INVITED}:
        raise HTTPException(409, "本轮预约已确认，不能覆盖")
    if not interview.booking_token_expires_at or interview.booking_token_expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(410, "预约链接已过期，请联系 HR 重签")
    if appointment_time(slot.start_at) <= datetime.now(timezone.utc):
        raise HTTPException(409, "预约时段已过期")
    if slot.status != InterviewSlotStatus.AVAILABLE:
        raise HTTPException(409, "该时段已不可用")
    confirmed = session.scalars(
        select(Interview).where(
            Interview.id != interview.id,
            Interview.confirmed_start_at.is_not(None),
        )
    ).all()
    for other in confirmed:
        if intervals_conflict(
            slot.start_at,
            slot.end_at,
            other.confirmed_start_at,
            other.confirmed_end_at,
            buffer_minutes=DEFAULT_BUFFER_MINUTES,
        ):
            raise HTTPException(409, "该时段刚被占用，请重新选择")

    try:
        meeting = create_mock_tencent_meeting(
            title=f"{interview.application.job.title}面试",
            start_at=slot.start_at,
            end_at=slot.end_at,
            participants=interview.interviewers or [],
            idempotency_key=f"interview-{interview.id}-{slot.id}",
        )
        meeting_url = meeting["join_url"]
        if not meeting_url:
            raise ValueError("Mock 会议缺少链接")
    except Exception as exc:
        interview.meeting_status = "mock_failed"
        write_audit_log(session, actor="会议程序", action="interview.meeting_failed", entity_type="interview", entity_id=interview.id,
                        details={"mode": "mock", "error_type": type(exc).__name__})
        session.commit()
        raise HTTPException(503, "Mock 会议创建失败，预约尚未确认，请稍后使用本链接重试") from exc

    for item in interview.slots:
        item.status = (
            InterviewSlotStatus.CONFIRMED
            if item.id == slot.id
            else InterviewSlotStatus.RELEASED
        )
    interview.confirmed_start_at = slot.start_at
    interview.confirmed_end_at = slot.end_at
    interview.status = InterviewStatus.CONFIRMED
    interview.meeting_url = meeting_url
    interview.meeting_status = "mock_created"
    interview.calendar_status = "demo_created"
    interview.booking_token = None
    if interview.application.status == ApplicationStatus.SCHEDULING:
        transition_application(
            session,
            interview.application_id,
            ApplicationStatus.INTERVIEW_CONFIRMED,
            actor="候选人预约",
        )
    write_audit_log(
        session,
        actor="候选人",
        action="interview.confirmed",
        entity_type="interview",
        entity_id=interview.id,
        after_data={
            "start_at": slot.start_at.isoformat(),
            "meeting_mode": "mock",
        },
    )
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="booking_success.html",
        context={},
    )


@app.get("/api/follow-ups")
def followups_api(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    tasks = session.scalars(select(FollowUpTask).order_by(FollowUpTask.due_at)).all()
    return [
        {
            "id": task.id,
            "status": task.status.value,
            "owner": task.owner,
            "reason": task.reason,
            "suggested_action": task.suggested_action,
            "due_at": _value(task.due_at),
        }
        for task in tasks
    ]


@app.post("/api/follow-ups/{task_id}/resolve")
def resolve_followup(task_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        task = resolve_follow_up(session, task_id, actor="本地管理员")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    session.commit()
    return {"ok": True, "id": task.id, "status": task.status.value}


def _export_rows(session: Session) -> list[dict[str, Any]]:
    records = []
    for row in _application_rows(session):
        application = session.get(Application, row["id"])
        latest_offer = max(application.offers, key=lambda item: item.created_at) if application and application.offers else None
        onboarding_open = (
            sum(1 for task in latest_offer.onboarding_tasks if task.status == OnboardingStatus.OPEN)
            if latest_offer
            else 0
        )
        records.append({
            "candidate_name": row["candidate_name"],
            "job_title": row["job_title"],
            "status": row["status_label"],
            "total_score": row["score"],
            "owner": row["owner"],
            "interview_time": row["interview_time"],
            "offer_status": latest_offer.status.value if latest_offer else "",
            "offer_sent_at": latest_offer.sent_at if latest_offer else "",
            "onboarding_open": onboarding_open,
            "assessment": row["exam_decisions"].get("assessment", {}).get("result", "历史未记录 / 未办理"),
            "written": row["exam_decisions"].get("written", {}).get("result", "历史未记录 / 未办理"),
            "onboarding_state": (application.extra_data or {}).get("onboarding", {}).get("state", ""),
            "actual_date": (application.extra_data or {}).get("onboarding", {}).get("actual_date", ""),
            "updated_at": row["updated_at"],
        })
    return records


@app.get("/api/exports/recruitment.xlsx")
def export_xlsx_api(session: Session = Depends(get_db)) -> Response:
    rows = _export_rows(session)
    content = export_recruitment_xlsx(rows)
    record_export_run(
        session,
        export_type="xlsx",
        status=ExportRunStatus.SUCCESS,
        file_name="recruitment.xlsx",
        row_count=len(rows),
        message="已生成腾讯文档兼容 XLSX 文件",
    )
    session.commit()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="recruitment.xlsx"'},
    )


@app.get("/api/exports/recruitment.csv")
def export_csv_api(session: Session = Depends(get_db)) -> Response:
    rows = _export_rows(session)
    content = export_recruitment_csv(rows)
    record_export_run(
        session,
        export_type="csv",
        status=ExportRunStatus.SUCCESS,
        file_name="recruitment.csv",
        row_count=len(rows),
        message="已生成腾讯文档兼容 CSV 文件",
    )
    session.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="recruitment.csv"'},
    )


@app.get("/api/dashboard/summary")
def dashboard_summary(session: Session = Depends(get_db)) -> dict[str, Any]:
    return _dashboard_data(session)


@app.get("/api/audit")
def audit_api(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    logs = session.scalars(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(200)
    ).all()
    return [
        {
            "id": log.id,
            "actor": log.actor,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]


from pathlib import Path
from fastapi.responses import FileResponse
from app.models import ExamResource, ExamDispatch
from app.services import exams


@app.get("/exams")
def exams_page(request: Request, application_id: int | None = None, resource_id: int | None = None, session: Session = Depends(get_db)):
    application = session.get(Application, application_id) if application_id else None
    resource = session.get(ExamResource, resource_id) if resource_id else None
    preview, preview_error = None, None
    if application and resource:
        try:
            preview = exams.preview_dispatch(application, resource)
        except ValueError as exc:
            preview_error = str(exc)
    dispatches = session.scalars(select(ExamDispatch).order_by(ExamDispatch.id.desc())).all()
    return _render(request, session, "exams.html", "exams", kinds=exams.KINDS,
        resources=session.scalars(select(ExamResource).order_by(ExamResource.id.desc())).all(),
        jobs=session.scalars(select(Job).order_by(Job.id)).all(),
        applications=session.scalars(select(Application).where(Application.status.in_([ApplicationStatus.ASSESSMENT_PENDING, ApplicationStatus.WRITTEN_PENDING]))).all(),
        selected_application=application, preview=preview, preview_error=preview_error, resource_request_key=secrets.token_hex(16),
        dispatches=[{"record": d, "notification": exams.dispatch_notification(session, d.id)} for d in dispatches])


@app.post("/api/exams/resources")
async def create_exam_resource(kind: str = Form(...), title: str = Form(...), url: str = Form(...), job_id: int = Form(...),
                               request_key: str | None = Form(None), file: UploadFile | None = File(None), session: Session = Depends(get_db)):
    stored = None
    try:
        exams.validate_resource(kind, title, url)
        if session.get(Job, job_id) is None:
            raise ValueError("请选择已有岗位")
        from hashlib import sha256
        from io import BytesIO
        from zipfile import ZipFile, BadZipFile
        content = b""
        if file and file.filename:
            if Path(file.filename).suffix.lower() not in {".pdf", ".docx"}:
                raise ValueError("内部试卷文件仅支持 PDF 或 DOCX")
            content = await file.read(int(get_setting("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))) + 1)
            if not content:
                raise ValueError("内部试卷文件不能为空")
            if Path(file.filename).suffix.lower() == ".pdf" and not content.startswith(b"%PDF-"):
                raise ValueError("文件内容不是 PDF")
            if Path(file.filename).suffix.lower() == ".docx":
                try:
                    with ZipFile(BytesIO(content)) as archive:
                        if "word/document.xml" not in archive.namelist():
                            raise ValueError("文件内容不是 DOCX")
                except BadZipFile as exc:
                    raise ValueError("DOCX 文件结构损坏") from exc
        signature = sha256(f"{job_id}|{kind}|{title.strip()}|{url.strip()}|".encode() + content).hexdigest()
        request_key = request_key or signature
        if len(request_key) > 64:
            raise ValueError("请求标识过长")
        lock_record(session, Job, job_id)
        existing = session.scalar(select(ExamResource).where(ExamResource.request_key == request_key))
        if existing:
            previous_content = Path(existing.file_path).read_bytes() if existing.file_path and Path(existing.file_path).is_file() else b""
            if (existing.job_id, existing.kind, existing.title, existing.url, previous_content) != (job_id, kind, title.strip(), url.strip(), content):
                raise ValueError("同一请求的资源内容已改变，请刷新页面后重新登记")
            return {"ok": True, "resource_id": existing.id, "redirect": "/exams", "message": "该请求已登记"}
        if file and file.filename:
            stored = store_upload_bytes(original_filename=file.filename, content=content, content_type=file.content_type)
        resource = ExamResource(kind=kind, title=title.strip(), url=url.strip(), job_id=job_id, request_key=request_key,
                                file_path=str(stored.file_path.resolve()) if stored else None,
                                filename=stored.original_filename if stored else None)
        session.add(resource)
        session.flush()
        write_audit_log(session, actor="HR", action="exam_resource.created", entity_type="exam_resource", entity_id=resource.id,
                        after_data={"kind": kind, "title": title, "url": url})
        session.commit()
        return {"ok": True, "resource_id": resource.id, "redirect": "/exams"}
    except ValueError as exc:
        session.rollback()
        if stored:
            stored.file_path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    finally:
        if file:
            await file.close()


@app.get("/api/exams/resources/{resource_id}/file")
def download_exam_resource(resource_id: int, session: Session = Depends(get_db)):
    resource = session.get(ExamResource, resource_id)
    if not resource or not resource.file_path or not Path(resource.file_path).is_file():
        raise HTTPException(404, "内部留存文件不存在")
    return FileResponse(resource.file_path, filename=resource.filename, media_type="application/octet-stream")


@app.post("/api/exams/resources/{resource_id}/delete")
def delete_exam_resource(resource_id: int, session: Session = Depends(get_db)):
    try:
        resource = session.get(ExamResource, resource_id)
        path = Path(resource.file_path) if resource and resource.file_path else None
        action = exams.delete_or_disable(session, resource_id)
        session.commit()
        if action == "deleted" and path:
            path.unlink(missing_ok=True)
        return {"ok": True, "message": "资源已停用，历史发放保留" if action == "disabled" else "资源已删除"}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/applications/{application_id}/exams/send")
def send_exam_api(application_id: int, resource_id: int = Form(...), candidate_revision: int = Form(...), session: Session = Depends(get_db)):
    try:
        notification = exams.send_exam(session, application_id, resource_id, candidate_revision=candidate_revision)
        return notification_result(notification)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/applications/{application_id}/exams/decision")
def decide_exam_api(application_id: int, kind: str = Form(...), result: str = Form(...), note: str = Form(...), version: int = Form(...), session: Session = Depends(get_db)):
    try:
        application = exams.decide_exam(session, application_id, kind=kind, result=result, note=note, version=version)
        session.commit()
        return {"ok": True, "status": application.status.value, "message": "HR 结论已保存", "redirect": f"/candidates/{application.candidate_id}?application_id={application.id}"}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


def notification_result(notification: Notification) -> dict:
    channel = "内部通知" if notification.channel == "wecom" else "邮件"
    return {"ok": notification.status in exams.DELIVERED, "notification_id": notification.id, "status": notification.status.value,
            "message": {"mocked": f"已生成 Mock {channel}记录（未真实发送）", "sent": f"{channel}已发送", "failed": f"{channel}发送失败，可检查配置后重试",
                        "sending": "发送处理中，请勿重复提交", "unknown": "送达结果未知，请先在通知中心核实"}.get(notification.status.value, notification.status.value)}


@app.post("/api/offers/{offer_id}/terms")
async def edit_offer_terms_api(offer_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        form = dict(await request.form())
        offer = save_offer_terms(session, offer_id, form, version=int(form.pop("version")))
        session.commit()
        return {"ok": True, "message": "条款已保存，需要重新审批", "revision": offer.terms["revision"]}
    except (ValueError, KeyError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/offers/{offer_id}/return")
def return_offer_api(offer_id: int, version: int = Form(...), reason: str = Form(...), session: Session = Depends(get_db)):
    try:
        return_offer(session, offer_id, version=version, reason=reason)
        session.commit()
        return {"ok": True, "message": "已退回修改"}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/applications/{application_id}/onboarding")
async def onboarding_registration_api(application_id: int, request: Request, session: Session = Depends(get_db)):
    from app.services.onboarding import update_onboarding
    try:
        form = dict(await request.form())
        application = update_onboarding(session, application_id, action=form.pop("action"), version=int(form.pop("version")), values=form)
        session.commit()
        return {"ok": True, "status": application.status.value, "message": "入职办理记录已保存"}
    except (ValueError, KeyError) as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/notifications/{notification_id}/verify")
def verify_delivery(notification_id: int, delivered: str = Form(...), note: str = Form(...), session: Session = Depends(get_db)):
    try:
        notification = lock_record(session, Notification, notification_id)
        if notification.status != NotificationStatus.UNKNOWN or delivered not in {"true", "false"} or not note.strip():
            raise ValueError("仅能核实未知送达记录，且必须填写人工核实依据")
        notification.status = NotificationStatus.SENT if delivered == "true" else NotificationStatus.FAILED
        write_audit_log(session, actor="HR", action="notification.verified", entity_type="notification", entity_id=notification.id,
                        after_data={"delivered": delivered, "note": note})
        if delivered == "true" and notification.target_type == "offer":
            record_offer_delivery(session, notification.target_id, notification)
        session.commit()
        return {"ok": True, "message": "核实已保存；确认未送达后可从原业务入口重试"}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/notifications/{notification_id}/retry")
def retry_internal_notification(notification_id: int, session: Session = Depends(get_db)):
    try:
        previous = lock_record(session, Notification, notification_id)
        if previous.channel != "wecom" or previous.status != NotificationStatus.FAILED:
            raise ValueError("仅可重试失败的内部通知；邮件请从原业务入口处理")
        retry_id = (previous.provider_response or {}).get("retry_id")
        if retry_id:
            return notification_result(session.get(Notification, retry_id))
        notification = notify_internal(session, title=previous.subject or "内部提醒", content=previous.content,
                                       target_type=previous.target_type, target_id=previous.target_id)
        previous.provider_response = {**(previous.provider_response or {}), "retry_id": notification.id}
        notification.provider_response = {**(notification.provider_response or {}), "retry_of": previous.id}
        write_audit_log(session, actor="HR", action="notification.retried", entity_type="notification", entity_id=notification.id,
                        details={"previous_id": previous.id, "status": notification.status.value})
        session.commit()
        return notification_result(notification)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
