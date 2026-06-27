from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_type: type[Enum], *, name: str) -> SAEnum:
    return SAEnum(
        enum_type,
        name=name,
        native_enum=False,
        values_callable=lambda values: [item.value for item in values],
        validate_strings=True,
    )


class ApplicationStatus(str, Enum):
    CAPTURED = "captured"
    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    PARSED = "parsed"
    DEDUPLICATED = "deduplicated"
    EVALUATING = "evaluating"
    EVALUATION_REVIEW = "evaluation_review"
    INTERVIEW_PENDING = "interview_pending"
    ON_HOLD = "on_hold"
    REJECTED = "rejected"
    SCHEDULING = "scheduling"
    INTERVIEW_CONFIRMED = "interview_confirmed"
    INTERVIEWED = "interviewed"
    FEEDBACK_PENDING = "feedback_pending"
    NEXT_ROUND = "next_round"
    OFFER_PENDING = "offer_pending"
    OFFER_SENT = "offer_sent"
    OFFER_ACCEPTED = "offer_accepted"
    OFFER_DECLINED = "offer_declined"
    ONBOARDING = "onboarding"
    JOINED = "joined"


class DedupStatus(str, Enum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"


class EvaluationStatus(str, Enum):
    COMPLETED = "completed"
    REVIEWED = "reviewed"


class InterviewStatus(str, Enum):
    DRAFT = "draft"
    INVITED = "invited"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InterviewSlotStatus(str, Enum):
    AVAILABLE = "available"
    HELD = "held"
    CONFIRMED = "confirmed"
    RELEASED = "released"


class FollowUpStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class OfferStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class OfferApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class OnboardingStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class NotificationStatus(str, Enum):
    MOCKED = "mocked"
    SENT = "sent"
    FAILED = "failed"


class ExportRunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    MOCKED = "mocked"


class ImportTaskStatus(str, Enum):
    UPLOADED = "uploaded"
    EXTRACTING_TEXT = "extracting_text"
    RULE_PARSING = "rule_parsing"
    AI_NORMALIZING = "ai_normalizing"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"


class ImportFileStatus(str, Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"


class ParseRunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("normalized_username", name="uq_user_normalized_username"),
        Index("ix_users_normalized_username", "normalized_username"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_username: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint(
            "normalized_name",
            "normalized_phone",
            name="uq_candidate_normalized_identity",
        ),
        Index("ix_candidates_normalized_name", "normalized_name"),
        Index("ix_candidates_normalized_phone", "normalized_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100))
    normalized_name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(40))
    normalized_phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(255))
    dedup_status: Mapped[DedupStatus] = mapped_column(
        enum_column(DedupStatus, name="candidate_dedup_status"),
        default=DedupStatus.CONFIRMED,
        nullable=False,
    )
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    sources: Mapped[list[CandidateSource]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class CandidateSource(TimestampMixin, Base):
    __tablename__ = "candidate_sources"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "source_application_id",
            name="uq_candidate_source_application",
        ),
        Index("ix_candidate_sources_candidate_id", "candidate_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    source_candidate_id: Mapped[str | None] = mapped_column(String(120))
    source_application_id: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(100))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    raw_resume: Mapped[str | None] = mapped_column(Text)
    raw_chat: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidate: Mapped[Candidate] = relationship(back_populates="sources")
    application: Mapped[Application | None] = relationship(back_populates="source")


class ImportTask(TimestampMixin, Base):
    __tablename__ = "import_tasks"
    __table_args__ = (
        Index("ix_import_tasks_status", "status"),
        Index("ix_import_tasks_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(50), default="file", nullable=False)
    status: Mapped[ImportTaskStatus] = mapped_column(
        enum_column(ImportTaskStatus, name="import_task_status"),
        default=ImportTaskStatus.UPLOADED,
        nullable=False,
    )
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    job_title: Mapped[str | None] = mapped_column(String(200))
    jd_text: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(String(100))
    owner: Mapped[str | None] = mapped_column(String(120))
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    preview_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    duplicate_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    confirmed_application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )

    files: Mapped[list[ImportFile]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    parse_runs: Mapped[list[ResumeParseRun]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class ImportFile(TimestampMixin, Base):
    __tablename__ = "import_files"
    __table_args__ = (Index("ix_import_files_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("import_tasks.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extract_status: Mapped[ImportFileStatus] = mapped_column(
        enum_column(ImportFileStatus, name="import_file_status"),
        default=ImportFileStatus.PENDING,
        nullable=False,
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_text_hash: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    task: Mapped[ImportTask] = relationship(back_populates="files")


class ResumeParseRun(TimestampMixin, Base):
    __tablename__ = "resume_parse_runs"
    __table_args__ = (Index("ix_resume_parse_runs_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("import_tasks.id", ondelete="CASCADE"), nullable=False
    )
    parser_name: Mapped[str] = mapped_column(String(120), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[ParseRunStatus] = mapped_column(
        enum_column(ParseRunStatus, name="resume_parse_run_status"),
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)

    task: Mapped[ImportTask] = relationship(back_populates="parse_runs")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("platform", "source_job_id", name="uq_job_source"),
        Index("ix_jobs_title", "title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    source_job_id: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str | None] = mapped_column(String(120))
    owner: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    versions: Mapped[list[JobVersion]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobVersion.version_number",
    )
    applications: Mapped[list[Application]] = relationship(back_populates="job")


class JobVersion(TimestampMixin, Base):
    __tablename__ = "job_versions"
    __table_args__ = (
        UniqueConstraint("job_id", "version_hash", name="uq_job_version_hash"),
        UniqueConstraint("job_id", "version_number", name="uq_job_version_number"),
        Index("ix_job_versions_current", "job_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    jd_raw: Mapped[str] = mapped_column(Text, nullable=False)
    structured_jd: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    job: Mapped[Job] = relationship(back_populates="versions")
    applications: Mapped[list[Application]] = relationship(
        back_populates="job_version"
    )
    evaluations: Mapped[list[Evaluation]] = relationship(back_populates="job_version")


class Application(TimestampMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_application_source"),
        Index("ix_applications_candidate_id", "candidate_id"),
        Index("ix_applications_job_status", "job_id", "status"),
        Index("ix_applications_owner_status", "owner", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    job_version_id: Mapped[int] = mapped_column(
        ForeignKey("job_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_sources.id", ondelete="SET NULL")
    )
    channel: Mapped[str | None] = mapped_column(String(100))
    owner: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus, name="application_status"),
        default=ApplicationStatus.CAPTURED,
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    candidate: Mapped[Candidate] = relationship(back_populates="applications")
    job: Mapped[Job] = relationship(back_populates="applications")
    job_version: Mapped[JobVersion] = relationship(back_populates="applications")
    source: Mapped[CandidateSource | None] = relationship(
        back_populates="application"
    )
    evaluations: Mapped[list[Evaluation]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    interviews: Mapped[list[Interview]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    offers: Mapped[list[Offer]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class Evaluation(TimestampMixin, Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        Index("ix_evaluations_application_id", "application_id"),
        Index("ix_evaluations_total_score", "total_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    job_version_id: Mapped[int] = mapped_column(
        ForeignKey("job_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[EvaluationStatus] = mapped_column(
        enum_column(EvaluationStatus, name="evaluation_status"),
        default=EvaluationStatus.COMPLETED,
        nullable=False,
    )
    evaluation_version: Mapped[str] = mapped_column(
        String(30), default="1.0", nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    scores: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    missing_information: Mapped[list[str] | None] = mapped_column(JSON)

    application: Mapped[Application] = relationship(back_populates="evaluations")
    job_version: Mapped[JobVersion] = relationship(back_populates="evaluations")


class Interview(TimestampMixin, Base):
    __tablename__ = "interviews"
    __table_args__ = (
        UniqueConstraint("application_id", "round", name="uq_interview_round"),
        Index("ix_interviews_status", "status"),
        Index("ix_interviews_confirmed_start", "confirmed_start_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[InterviewStatus] = mapped_column(
        enum_column(InterviewStatus, name="interview_status"),
        default=InterviewStatus.DRAFT,
        nullable=False,
    )
    interviewers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    confirmed_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    confirmed_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    booking_token: Mapped[str | None] = mapped_column(String(255), unique=True)
    booking_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    meeting_url: Mapped[str | None] = mapped_column(Text)
    meeting_status: Mapped[str | None] = mapped_column(String(50))
    calendar_status: Mapped[str | None] = mapped_column(String(50))
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    application: Mapped[Application] = relationship(back_populates="interviews")
    slots: Mapped[list[InterviewSlot]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )


class InterviewSlot(TimestampMixin, Base):
    __tablename__ = "interview_slots"
    __table_args__ = (
        UniqueConstraint(
            "interview_id",
            "start_at",
            "end_at",
            name="uq_interview_slot_time",
        ),
        Index("ix_interview_slots_time", "start_at", "end_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[InterviewSlotStatus] = mapped_column(
        enum_column(InterviewSlotStatus, name="interview_slot_status"),
        default=InterviewSlotStatus.AVAILABLE,
        nullable=False,
    )

    interview: Mapped[Interview] = relationship(back_populates="slots")


class Offer(TimestampMixin, Base):
    __tablename__ = "offers"
    __table_args__ = (
        Index("ix_offers_application_id", "application_id"),
        Index("ix_offers_status", "status"),
        Index("ix_offers_token", "candidate_token"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[OfferStatus] = mapped_column(
        enum_column(OfferStatus, name="offer_status"),
        default=OfferStatus.DRAFT,
        nullable=False,
    )
    candidate_name: Mapped[str] = mapped_column(String(100), nullable=False)
    candidate_email: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str] = mapped_column(String(120), default="Testin", nullable=False)
    job_title: Mapped[str] = mapped_column(String(200), nullable=False)
    template_version: Mapped[str] = mapped_column(String(30), default="testin-offer-2026-06", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    report_time: Mapped[str | None] = mapped_column(String(120))
    report_location: Mapped[str | None] = mapped_column(String(255))
    office_location: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidate_token: Mapped[str | None] = mapped_column(String(255), unique=True)
    response_note: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="offers")
    approvals: Mapped[list[OfferApproval]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", order_by="OfferApproval.id"
    )
    onboarding_tasks: Mapped[list[OnboardingTask]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )


class OfferApproval(TimestampMixin, Base):
    __tablename__ = "offer_approvals"
    __table_args__ = (Index("ix_offer_approvals_offer_id", "offer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    approver: Mapped[str] = mapped_column(String(120), default="管理员", nullable=False)
    status: Mapped[OfferApprovalStatus] = mapped_column(
        enum_column(OfferApprovalStatus, name="offer_approval_status"),
        default=OfferApprovalStatus.PENDING,
        nullable=False,
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    offer: Mapped[Offer] = relationship(back_populates="approvals")


class OnboardingTask(TimestampMixin, Base):
    __tablename__ = "onboarding_tasks"
    __table_args__ = (
        Index("ix_onboarding_offer_id", "offer_id"),
        Index("ix_onboarding_status_due", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(120))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[OnboardingStatus] = mapped_column(
        enum_column(OnboardingStatus, name="onboarding_status"),
        default=OnboardingStatus.OPEN,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    offer: Mapped[Offer] = relationship(back_populates="onboarding_tasks")


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status", "status"),
        Index("ix_notifications_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(255))
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[int | None] = mapped_column(Integer)
    template: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus, name="notification_status"),
        nullable=False,
    )
    subject: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)


class ExportRun(TimestampMixin, Base):
    __tablename__ = "export_runs"
    __table_args__ = (Index("ix_export_runs_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    export_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[ExportRunStatus] = mapped_column(
        enum_column(ExportRunStatus, name="export_run_status"),
        nullable=False,
    )
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_target: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str | None] = mapped_column(Text)


class FollowUpTask(TimestampMixin, Base):
    __tablename__ = "follow_up_tasks"
    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "rule_code",
            "window_key",
            name="uq_follow_up_rule_window",
        ),
        Index("ix_follow_up_status_due", "status", "due_at"),
        Index("ix_follow_up_owner_status", "owner", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    window_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[FollowUpStatus] = mapped_column(
        enum_column(FollowUpStatus, name="follow_up_status"),
        default=FollowUpStatus.OPEN,
        nullable=False,
    )
    owner: Mapped[str | None] = mapped_column(String(120))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    reminder_draft: Mapped[str | None] = mapped_column(Text)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
