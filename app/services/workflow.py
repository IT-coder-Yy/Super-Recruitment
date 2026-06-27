from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationStatus,
    AuditLog,
    Evaluation,
    FollowUpStatus,
    FollowUpTask,
    Interview,
    InterviewSlot,
    Job,
    JobVersion,
    utc_now,
)
from app.schemas import (
    ApplicationCreate,
    EvaluationCreate,
    FollowUpCreate,
    InterviewCreate,
    InterviewSlotCreate,
    JobCreate,
)


class InvalidStatusTransition(ValueError):
    pass


class HumanActionRequired(PermissionError):
    pass


APPLICATION_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.CAPTURED: frozenset({ApplicationStatus.PARSING}),
    ApplicationStatus.PARSING: frozenset(
        {ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.PARSED}
    ),
    ApplicationStatus.NEEDS_REVIEW: frozenset({ApplicationStatus.PARSED}),
    ApplicationStatus.PARSED: frozenset({ApplicationStatus.DEDUPLICATED}),
    ApplicationStatus.DEDUPLICATED: frozenset({ApplicationStatus.EVALUATING}),
    ApplicationStatus.EVALUATING: frozenset(
        {ApplicationStatus.EVALUATION_REVIEW}
    ),
    ApplicationStatus.EVALUATION_REVIEW: frozenset(
        {
            ApplicationStatus.INTERVIEW_PENDING,
            ApplicationStatus.ON_HOLD,
            ApplicationStatus.REJECTED,
        }
    ),
    ApplicationStatus.ON_HOLD: frozenset(
        {
            ApplicationStatus.INTERVIEW_PENDING,
            ApplicationStatus.EVALUATING,
            ApplicationStatus.REJECTED,
        }
    ),
    ApplicationStatus.INTERVIEW_PENDING: frozenset(
        {ApplicationStatus.SCHEDULING}
    ),
    ApplicationStatus.SCHEDULING: frozenset(
        {ApplicationStatus.INTERVIEW_CONFIRMED}
    ),
    ApplicationStatus.INTERVIEW_CONFIRMED: frozenset(
        {ApplicationStatus.INTERVIEWED}
    ),
    ApplicationStatus.INTERVIEWED: frozenset(
        {ApplicationStatus.FEEDBACK_PENDING}
    ),
    ApplicationStatus.FEEDBACK_PENDING: frozenset(
        {
            ApplicationStatus.NEXT_ROUND,
            ApplicationStatus.OFFER_PENDING,
            ApplicationStatus.REJECTED,
        }
    ),
    ApplicationStatus.NEXT_ROUND: frozenset(
        {ApplicationStatus.INTERVIEW_PENDING, ApplicationStatus.SCHEDULING}
    ),
    ApplicationStatus.OFFER_PENDING: frozenset(
        {ApplicationStatus.OFFER_SENT, ApplicationStatus.REJECTED}
    ),
    ApplicationStatus.OFFER_SENT: frozenset(
        {
            ApplicationStatus.OFFER_ACCEPTED,
            ApplicationStatus.OFFER_DECLINED,
        }
    ),
    ApplicationStatus.OFFER_ACCEPTED: frozenset(
        {ApplicationStatus.ONBOARDING}
    ),
    ApplicationStatus.ONBOARDING: frozenset({ApplicationStatus.JOINED}),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.OFFER_DECLINED: frozenset(),
    ApplicationStatus.JOINED: frozenset(),
}

HUMAN_ACTION_TARGETS = frozenset(
    {
        ApplicationStatus.INTERVIEW_PENDING,
        ApplicationStatus.ON_HOLD,
        ApplicationStatus.REJECTED,
        ApplicationStatus.NEXT_ROUND,
        ApplicationStatus.OFFER_PENDING,
        ApplicationStatus.OFFER_SENT,
        ApplicationStatus.OFFER_ACCEPTED,
        ApplicationStatus.OFFER_DECLINED,
        ApplicationStatus.ONBOARDING,
        ApplicationStatus.JOINED,
    }
)


def hash_jd_content(jd_raw: str) -> str:
    canonical = jd_raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_or_create_job_version(
    session: Session,
    data: JobCreate | Mapping[str, Any],
    *,
    actor: str = "system",
) -> tuple[Job, JobVersion, bool]:
    values = _to_dict(data)
    platform = str(values["platform"]).strip().casefold()
    source_job_id = str(values["source_job_id"]).strip()
    version_hash = hash_jd_content(str(values["jd_raw"]))

    job = session.scalar(
        select(Job).where(
            Job.platform == platform,
            Job.source_job_id == source_job_id,
        )
    )
    if job is None:
        job = Job(
            platform=platform,
            source_job_id=source_job_id,
            title=values["title"],
            department=values.get("department"),
            owner=values.get("owner"),
        )
        session.add(job)
        session.flush()
        write_audit_log(
            session,
            actor=actor,
            action="job.created",
            entity_type="job",
            entity_id=job.id,
            after_data={
                "platform": job.platform,
                "source_job_id": job.source_job_id,
                "title": job.title,
            },
        )
    else:
        job.title = values["title"]
        job.department = values.get("department")
        job.owner = values.get("owner")

    existing_version = session.scalar(
        select(JobVersion).where(
            JobVersion.job_id == job.id,
            JobVersion.version_hash == version_hash,
        )
    )
    if existing_version:
        return job, existing_version, False

    session.execute(
        update(JobVersion)
        .where(JobVersion.job_id == job.id, JobVersion.is_current.is_(True))
        .values(is_current=False)
    )
    latest_version = session.scalar(
        select(func.max(JobVersion.version_number)).where(
            JobVersion.job_id == job.id
        )
    )
    job_version = JobVersion(
        job_id=job.id,
        version_number=(latest_version or 0) + 1,
        version_hash=version_hash,
        jd_raw=values["jd_raw"],
        structured_jd=values.get("structured_jd"),
        is_current=True,
    )
    session.add(job_version)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="job_version.created",
        entity_type="job_version",
        entity_id=job_version.id,
        after_data={
            "job_id": job.id,
            "version_number": job_version.version_number,
            "version_hash": job_version.version_hash,
        },
    )
    return job, job_version, True


def create_application(
    session: Session,
    data: ApplicationCreate | Mapping[str, Any],
    *,
    actor: str = "system",
) -> tuple[Application, bool]:
    values = _to_dict(data)
    source_id = values.get("source_id")
    if source_id is not None:
        existing = session.scalar(
            select(Application).where(Application.source_id == source_id)
        )
        if existing:
            return existing, False

    job_version = session.get(JobVersion, values["job_version_id"])
    if job_version is None:
        raise ValueError("JD 版本不存在")
    if job_version.job_id != values["job_id"]:
        raise ValueError("JD 版本不属于指定岗位")

    application = Application(
        candidate_id=values["candidate_id"],
        job_id=values["job_id"],
        job_version_id=values["job_version_id"],
        source_id=source_id,
        channel=values.get("channel"),
        owner=values.get("owner"),
        applied_at=values.get("applied_at"),
        extra_data=values.get("extra_data"),
    )
    session.add(application)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="application.created",
        entity_type="application",
        entity_id=application.id,
        after_data={
            "candidate_id": application.candidate_id,
            "job_id": application.job_id,
            "job_version_id": application.job_version_id,
            "status": application.status.value,
        },
    )
    return application, True


def allowed_transitions(
    status: ApplicationStatus | str,
) -> frozenset[ApplicationStatus]:
    current = ApplicationStatus(status)
    return APPLICATION_TRANSITIONS[current]


def transition_application(
    session: Session,
    application_id: int,
    to_status: ApplicationStatus | str,
    *,
    actor: str,
    is_human_action: bool = False,
    reason: str | None = None,
) -> Application:
    application = session.get(Application, application_id)
    if application is None:
        raise LookupError("投递记录不存在")

    target = ApplicationStatus(to_status)
    current = application.status
    if target == current:
        return application
    if target not in APPLICATION_TRANSITIONS[current]:
        raise InvalidStatusTransition(
            f"不允许从 {current.value} 转换到 {target.value}"
        )
    if target in HUMAN_ACTION_TARGETS and not is_human_action:
        raise HumanActionRequired(f"转换到 {target.value} 需要人工操作")

    before = {"status": current.value, "decision_reason": application.decision_reason}
    application.status = target
    application.status_changed_at = utc_now()
    if reason is not None:
        application.decision_reason = reason
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="application.status_changed",
        entity_type="application",
        entity_id=application.id,
        before_data=before,
        after_data={
            "status": target.value,
            "decision_reason": application.decision_reason,
        },
        details={"is_human_action": is_human_action},
    )
    return application


def create_evaluation(
    session: Session,
    application_id: int,
    data: EvaluationCreate | Mapping[str, Any],
    *,
    actor: str = "evaluation_agent",
) -> Evaluation:
    application = session.get(Application, application_id)
    if application is None:
        raise LookupError("投递记录不存在")
    values = _to_dict(data)
    dimensions = values["dimensions"]
    weights = [float(item["weight"]) for item in dimensions]
    if abs(sum(weights) - 1.0) > 0.001:
        raise ValueError("评估维度权重之和必须为 1")
    for item in dimensions:
        score = float(item["score"])
        if not 0 <= score <= 100:
            raise ValueError("评估分数必须在 0 到 100 之间")
    total_score = round(
        sum(float(item["score"]) * float(item["weight"]) for item in dimensions),
        2,
    )

    evaluation = Evaluation(
        application_id=application.id,
        job_version_id=application.job_version_id,
        evaluation_version=values.get("evaluation_version", "1.0"),
        model=values["model"],
        prompt_version=values.get("prompt_version"),
        scores=dimensions,
        total_score=total_score,
        reason=values["reason"],
        missing_information=values.get("missing_information") or [],
    )
    session.add(evaluation)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="evaluation.created",
        entity_type="evaluation",
        entity_id=evaluation.id,
        after_data={
            "application_id": application.id,
            "job_version_id": evaluation.job_version_id,
            "total_score": evaluation.total_score,
            "model": evaluation.model,
        },
    )
    return evaluation


def create_interview(
    session: Session,
    application_id: int,
    data: InterviewCreate | Mapping[str, Any],
    *,
    actor: str = "system",
) -> tuple[Interview, bool]:
    values = _to_dict(data)
    interview_round = int(values.get("round", 1))
    existing = session.scalar(
        select(Interview).where(
            Interview.application_id == application_id,
            Interview.round == interview_round,
        )
    )
    if existing:
        return existing, False

    interview = Interview(
        application_id=application_id,
        round=interview_round,
        interviewers=values.get("interviewers") or [],
        booking_token=values.get("booking_token"),
        booking_token_expires_at=values.get("booking_token_expires_at"),
    )
    session.add(interview)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="interview.created",
        entity_type="interview",
        entity_id=interview.id,
        after_data={
            "application_id": application_id,
            "round": interview.round,
            "status": interview.status.value,
        },
    )
    return interview, True


def add_interview_slot(
    session: Session,
    interview_id: int,
    data: InterviewSlotCreate | Mapping[str, Any],
    *,
    actor: str = "system",
) -> tuple[InterviewSlot, bool]:
    values = _to_dict(data)
    start_at = values["start_at"]
    end_at = values["end_at"]
    if end_at <= start_at:
        raise ValueError("面试结束时间必须晚于开始时间")

    existing = session.scalar(
        select(InterviewSlot).where(
            InterviewSlot.interview_id == interview_id,
            InterviewSlot.start_at == start_at,
            InterviewSlot.end_at == end_at,
        )
    )
    if existing:
        return existing, False

    slot = InterviewSlot(
        interview_id=interview_id,
        start_at=start_at,
        end_at=end_at,
    )
    session.add(slot)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="interview_slot.created",
        entity_type="interview_slot",
        entity_id=slot.id,
        after_data={
            "interview_id": interview_id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
        },
    )
    return slot, True


def get_or_create_follow_up(
    session: Session,
    data: FollowUpCreate | Mapping[str, Any],
    *,
    actor: str = "follow_up_agent",
) -> tuple[FollowUpTask, bool]:
    values = _to_dict(data)
    existing = session.scalar(
        select(FollowUpTask).where(
            FollowUpTask.target_type == values["target_type"],
            FollowUpTask.target_id == values["target_id"],
            FollowUpTask.rule_code == values["rule_code"],
            FollowUpTask.window_key == values["window_key"],
        )
    )
    if existing:
        return existing, False

    task = FollowUpTask(**values)
    session.add(task)
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="follow_up.created",
        entity_type="follow_up_task",
        entity_id=task.id,
        after_data={
            "target_type": task.target_type,
            "target_id": task.target_id,
            "rule_code": task.rule_code,
            "window_key": task.window_key,
        },
    )
    return task, True


def resolve_follow_up(
    session: Session,
    task_id: int,
    *,
    actor: str,
) -> FollowUpTask:
    task = session.get(FollowUpTask, task_id)
    if task is None:
        raise LookupError("跟进任务不存在")
    if task.status == FollowUpStatus.RESOLVED:
        return task

    before = {"status": task.status.value}
    task.status = FollowUpStatus.RESOLVED
    task.resolved_at = utc_now()
    session.flush()
    write_audit_log(
        session,
        actor=actor,
        action="follow_up.resolved",
        entity_type="follow_up_task",
        entity_id=task.id,
        before_data=before,
        after_data={
            "status": task.status.value,
            "resolved_at": task.resolved_at.isoformat(),
        },
    )
    return task


def write_audit_log(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    log = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_data=before_data,
        after_data=after_data,
        details=details,
    )
    session.add(log)
    session.flush()
    return log


def _to_dict(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return dict(data)

