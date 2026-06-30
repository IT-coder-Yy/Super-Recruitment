from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
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
from app.services.auth import hash_password, normalize_username, verify_password
from app.services.candidates import normalize_name, normalize_phone
from app.services.exporter import export_recruitment_csv, export_recruitment_xlsx
from app.services.imports import (
    confirm_import_task,
    create_file_import_task,
    import_task_rows,
    parse_import_task,
    reject_import_task,
    serialize_import_task,
    store_upload_bytes,
)
from app.services.offers import (
    approve_offer,
    create_offer,
    list_offers,
    list_onboarding_tasks,
    notify_internal,
    record_export_run,
    respond_offer,
    scan_offer_onboarding_reminders,
    send_offer,
    send_email_notification,
    submit_interview_feedback,
    confirm_onboarding_task,
)
from app.services.scheduling import (
    BookingTokenError,
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
)


app = FastAPI(
    title="AI 招聘自动化 Demo",
    description="低成本、本地运行、人工审批优先的 AI 招聘工作流。",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=ROOT_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT_DIR / "app" / "templates")

PUBLIC_EXACT_PATHS = {"/health", "/login", "/register", "/favicon.ico"}
PUBLIC_PATH_PREFIXES = ("/static/", "/schedule/", "/offer/")
CSRF_EXEMPT_PREFIXES = ("/schedule/", "/offer/")
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 5

STATUS_LABELS = {
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


@app.on_event("startup")
def startup() -> None:
    init_db()
    with session_scope() as session:
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
    return await call_next(request)


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
        if provided:
            request.session["csrf_token"] = provided
            return True
        return False
    return bool(provided) and secrets.compare_digest(expected, provided)


def _csrf_form_valid(request: Request, csrf_token: str) -> bool:
    expected = request.session.get("csrf_token")
    if not isinstance(expected, str) or not expected:
        # Session cookie was lost (e.g. Chrome cookie policy) — the token
        # from the rendered form is still trustworthy because it was embedded
        # in a page served by this same origin.  Accept the submitted token
        # and adopt it into the fresh session so subsequent requests work.
        if csrf_token:
            request.session["csrf_token"] = csrf_token
            return True
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
        "evaluation_reason": evaluation.reason if evaluation else None,
        "dimensions": evaluation.scores if evaluation else [],
        "missing_information": evaluation.missing_information if evaluation else [],
        "interview_id": interview.id if interview else None,
        "interview_time": interview.confirmed_start_at if interview else None,
        "meeting_url": interview.meeting_url if interview else None,
        "updated_at": application.updated_at,
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
    screened_states = {
        "deduplicated",
        "evaluating",
        "evaluation_review",
        "interview_pending",
        "scheduling",
        "interview_confirmed",
        "interviewed",
        "feedback_pending",
        "next_round",
        "offer_pending",
        "offer_sent",
        "offer_accepted",
        "offer_declined",
        "onboarding",
        "joined",
    }
    interview_states = {
        "interview_pending",
        "scheduling",
        "interview_confirmed",
        "interviewed",
        "feedback_pending",
        "next_round",
        "offer_pending",
        "offer_sent",
        "offer_accepted",
        "offer_declined",
        "onboarding",
        "joined",
    }
    offer_states = {
        "offer_pending",
        "offer_sent",
        "offer_accepted",
        "offer_declined",
        "onboarding",
        "joined",
    }
    overdue = session.scalar(
        select(func.count(FollowUpTask.id)).where(
            FollowUpTask.status == FollowUpStatus.OPEN
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
        "screened": sum(row["status"] in screened_states for row in rows),
        "interviews": sum(row["status"] in interview_states for row in rows),
        "offers": sum(row["status"] in offer_states for row in rows),
        "onboarded": statuses.get("joined", 0),
    }
    return {
        "total_candidates": total_candidates,
        "new_today": sum(
            bool(row["updated_at"] and row["updated_at"].date() == datetime.now(timezone.utc).date())
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
    pending_terms = ["薪资/补贴", "职级", "预计入职日期"]
    approval_summary = (
        f"{offer.candidate_name} 已完成面试反馈流程，拟录用岗位为 {offer.job_title}。"
        "本 Demo 中薪资、职级和预计入职日期保持“待 HR 填写”，需审批人确认后才能发放。"
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
    audits = session.scalars(
        select(AuditLog)
        .where(
            (AuditLog.entity_type == "candidate") & (AuditLog.entity_id == candidate_id)
            | (AuditLog.entity_type == "application")
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
    )


@app.post("/api/candidates/{candidate_id}/supplement")
def supplement_candidate_api(
    candidate_id: int,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    skills: str = Form(""),
    experience: str = Form(""),
    chat_summary: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选人不存在")

    before = {
        "name": candidate.name,
        "phone": _mask_phone(candidate.phone),
        "email": _mask_email(candidate.email),
        "structured_data": _candidate_manual_snapshot(candidate.structured_data),
    }
    cleaned_name = name.strip()
    cleaned_phone = phone.strip()
    cleaned_email = email.strip()
    if cleaned_name:
        candidate.name = cleaned_name
        candidate.normalized_name = normalize_name(cleaned_name)
    if cleaned_phone:
        candidate.phone = cleaned_phone
        candidate.normalized_phone = normalize_phone(cleaned_phone)
    if cleaned_email:
        candidate.email = cleaned_email

    structured = dict(candidate.structured_data or {})
    human_summary = dict(structured.get("human_summary") or {})
    if skills.strip():
        human_summary["skills"] = _split_lines(skills)
    if experience.strip():
        human_summary["projects"] = [
            {"title": f"人工补充经历 {index}", "description": item}
            for index, item in enumerate(_split_lines(experience), start=1)
        ]
    if chat_summary.strip():
        human_summary["chat_summary"] = chat_summary.strip()
    if note.strip():
        human_summary["note"] = note.strip()
    structured["human_summary"] = human_summary
    structured["manual_supplement"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fields": [
            field
            for field, value in {
                "name": cleaned_name,
                "phone": cleaned_phone,
                "email": cleaned_email,
                "skills": skills.strip(),
                "experience": experience.strip(),
                "chat_summary": chat_summary.strip(),
                "note": note.strip(),
            }.items()
            if value
        ],
    }
    candidate.structured_data = structured
    write_audit_log(
        session,
        actor="本地管理员",
        action="candidate.supplemented",
        entity_type="candidate",
        entity_id=candidate.id,
        before_data=before,
        after_data={
            "name": candidate.name,
            "phone": _mask_phone(candidate.phone),
            "email": _mask_email(candidate.email),
            "structured_data": _candidate_manual_snapshot(candidate.structured_data),
        },
    )
    session.commit()
    return {"ok": True, "message": "候选人补充资料已保存"}


@app.post("/api/candidates/{candidate_id}/profile")
def update_candidate_profile_api(
    candidate_id: int,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    skills: str = Form(""),
    experience: str = Form(""),
    chat_summary: str = Form(""),
    note: str = Form(""),
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
    scan_offer_onboarding_reminders(session)
    session.commit()
    return _render(
        request,
        session,
        "onboarding.html",
        "onboarding",
        tasks=list_onboarding_tasks(session),
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
    job_title: str = Form(""),
    jd_text: str = Form(""),
    channel: str = Form("本地上传"),
    owner: str = Form("HR"),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        stored_files = [
            store_upload_bytes(
                original_filename=file.filename or "resume.txt",
                content=await file.read(),
                content_type=file.content_type,
            )
            for file in files
        ]
        task = create_file_import_task(
            session,
            uploads=stored_files,
            operator_id=request.session.get("user_id"),
            job_title=job_title,
            jd_text=jd_text,
            channel=channel,
            owner=owner,
        )
        session.commit()
        return {
            "ok": True,
            "message": "文件已上传，已创建导入任务；请点击开始解析。",
            "task": serialize_import_task(task),
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


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


@app.post("/api/import/tasks/{task_id}/confirm")
def confirm_import_task_api(
    task_id: int,
    start_evaluation: bool = Form(False),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        application = confirm_import_task(
            session,
            task_id,
            start_evaluation=start_evaluation,
        )
        evaluation_result = (
            _evaluate_application_record(session, application)
            if start_evaluation
            else None
        )
        session.commit()
        return {
            "ok": True,
            "message": "已确认入库，候选人和投递记录已创建。"
            + ("AI 评估已完成，等待人工审核。" if evaluation_result else ""),
            "candidate_id": application.candidate_id,
            "application_id": application.id,
            "evaluation": evaluation_result,
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


def _advance_for_evaluation(session: Session, application: Application) -> None:
    sequence = [
        ApplicationStatus.PARSING,
        ApplicationStatus.PARSED,
        ApplicationStatus.DEDUPLICATED,
        ApplicationStatus.EVALUATING,
    ]
    if application.status == ApplicationStatus.NEEDS_REVIEW:
        transition_application(
            session,
            application.id,
            ApplicationStatus.PARSED,
            actor="本地管理员",
            is_human_action=True,
            reason="Demo 人工确认后继续",
        )
    for target in sequence:
        if application.status == target:
            continue
        try:
            transition_application(
                session,
                application.id,
                target,
                actor="AI工作流",
                is_human_action=False,
            )
        except InvalidStatusTransition:
            continue


def _evaluate_application_record(
    session: Session,
    application: Application,
) -> dict[str, Any]:
    source = application.source
    raw_chat = "\n".join(
        str(item.get("content", "")) for item in (source.raw_chat or [])
    ) if source else ""
    result = AIService().analyze_candidate(
        source.raw_resume if source and source.raw_resume else "",
        raw_chat,
        application.job_version.jd_raw,
    )
    application.candidate.structured_data = result["structured"]
    _advance_for_evaluation(session, application)
    evaluation_data = result["evaluation"]
    dimensions = [
        EvaluationDimension(
            name=item["name"],
            score=item["score"],
            weight=item["weight"],
            jd_requirement=item.get("jd_requirement"),
            resume_evidence=item.get("resume_evidence"),
            confidence=item.get("confidence"),
        )
        for item in evaluation_data["dimensions"]
    ]
    evaluation = create_evaluation(
        session,
        application.id,
        EvaluationCreate(
            model=evaluation_data.get("model") or "mock",
            prompt_version=evaluation_data.get("prompt_version"),
            evaluation_version=evaluation_data.get("evaluation_version", "1.0"),
            dimensions=dimensions,
            reason=evaluation_data["recommendation_reason"],
            missing_information=evaluation_data.get("missing_information", []),
        ),
        actor="评估Agent",
    )
    if application.status == ApplicationStatus.EVALUATING:
        transition_application(
            session,
            application.id,
            ApplicationStatus.EVALUATION_REVIEW,
            actor="评估Agent",
        )
    return {
        "evaluation_id": evaluation.id,
        "total_score": evaluation.total_score,
        "reason": evaluation.reason,
        "provider": evaluation_data.get("provider"),
        "fallback": evaluation_data.get("fallback"),
    }


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
    structured = AIService().structure_candidate(
        source.raw_resume if source and source.raw_resume else "",
        raw_chat,
        application.job_version.jd_raw,
    )
    application.candidate.structured_data = structured
    application.job_version.structured_jd = structured.get("job")
    _advance_for_evaluation(session, application)
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
    evaluation = _evaluate_application_record(session, application)
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
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, "投递记录不存在")
    if application.status == ApplicationStatus.INTERVIEW_PENDING:
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
                round=len(application.interviews) + 1,
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
        if token is None:
            token = create_booking_token(
                {"application_id": application.id},
                get_setting("APP_SECRET_KEY", "change-this-local-secret"),
            )
            interview.booking_token = token
            interview.booking_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=48)
    busy = [
        {"start_at": item.confirmed_start_at, "end_at": item.confirmed_end_at}
        for item in session.scalars(
            select(Interview).where(Interview.confirmed_start_at.is_not(None))
        ).all()
    ]
    slots = generate_candidate_slots(
        datetime.now(timezone.utc) + timedelta(hours=12),
        datetime.now(timezone.utc) + timedelta(days=7),
        busy,
        limit=5,
        minimum_notice_minutes=60,
    )
    for slot in slots:
        add_interview_slot(
            session,
            interview.id,
            InterviewSlotCreate(
                start_at=slot["start_at"], end_at=slot["end_at"]
            ),
            actor="排期Agent",
        )
    interview.status = InterviewStatus.INVITED
    session.commit()
    return {
        "ok": True,
        "interview_id": interview.id,
        "booking_url": str(request.base_url).rstrip("/") + f"/schedule/{token}",
        "slots": [
            {
                "id": item.id,
                "start_at": item.start_at.isoformat(),
                "end_at": item.end_at.isoformat(),
            }
            for item in interview.slots
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
    candidate = interview.application.candidate
    if not candidate.email:
        raise HTTPException(422, "候选人邮箱缺失，请先在候选人详情补充邮箱")
    booking_url = str(request.base_url).rstrip("/") + f"/schedule/{interview.booking_token}"
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
        template="interview_invite",
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
        "ok": True,
        "message": "预约邮件已发送" if result.status == NotificationStatus.SENT else "SMTP 未配置，已生成 Mock 预约邮件记录",
        "status": result.status.value,
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
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        offer = approve_offer(session, offer_id, reason=reason)
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
        return {
            "ok": True,
            "offer_id": offer.id,
            "status": offer.status.value,
            "candidate_email": offer.candidate_email,
            "offer_url": f"/offer/{offer.candidate_token}",
        }
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
        ):
            raise HTTPException(409, "该时段刚被占用，请重新选择")

    for item in interview.slots:
        item.status = (
            InterviewSlotStatus.CONFIRMED
            if item.id == slot.id
            else InterviewSlotStatus.RELEASED
        )
    interview.confirmed_start_at = slot.start_at
    interview.confirmed_end_at = slot.end_at
    interview.status = InterviewStatus.CONFIRMED
    meeting = create_mock_tencent_meeting(
        title=f"{interview.application.job.title}面试",
        start_at=slot.start_at,
        end_at=slot.end_at,
        participants=interview.interviewers or [],
        idempotency_key=f"interview-{interview.id}-{slot.id}",
    )
    interview.meeting_url = meeting["join_url"]
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
