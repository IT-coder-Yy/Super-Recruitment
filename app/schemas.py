from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    ApplicationStatus,
    DedupStatus,
    EvaluationStatus,
    FollowUpStatus,
    InterviewSlotStatus,
    InterviewStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CandidateCreate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    structured_data: dict[str, Any] | None = None


class CandidateRead(ORMModel):
    id: int
    name: str | None
    normalized_name: str | None
    phone: str | None
    normalized_phone: str | None
    email: str | None
    dedup_status: DedupStatus
    structured_data: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class CandidateSourceCreate(BaseModel):
    platform: str = Field(min_length=1, max_length=50)
    source_candidate_id: str | None = Field(default=None, max_length=120)
    source_application_id: str = Field(min_length=1, max_length=120)
    channel: str | None = Field(default=None, max_length=100)
    raw_payload: dict[str, Any] | None = None
    raw_resume: str | None = None
    raw_chat: list[dict[str, Any]] | None = None
    captured_at: datetime | None = None


class CandidateSourceRead(ORMModel):
    id: int
    candidate_id: int
    platform: str
    source_candidate_id: str | None
    source_application_id: str
    channel: str | None
    raw_payload: dict[str, Any] | None
    raw_resume: str | None
    raw_chat: list[dict[str, Any]] | None
    captured_at: datetime | None
    created_at: datetime


class JobVersionCreate(BaseModel):
    jd_raw: str = Field(min_length=1)
    structured_jd: dict[str, Any] | None = None


class JobCreate(BaseModel):
    platform: str = Field(min_length=1, max_length=50)
    source_job_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    department: str | None = Field(default=None, max_length=120)
    owner: str | None = Field(default=None, max_length=120)
    jd_raw: str = Field(min_length=1)
    structured_jd: dict[str, Any] | None = None


class JobVersionRead(ORMModel):
    id: int
    job_id: int
    version_number: int
    version_hash: str
    jd_raw: str
    structured_jd: dict[str, Any] | None
    is_current: bool
    created_at: datetime


class JobRead(ORMModel):
    id: int
    platform: str
    source_job_id: str
    title: str
    department: str | None
    owner: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ApplicationCreate(BaseModel):
    candidate_id: int
    job_id: int
    job_version_id: int
    source_id: int | None = None
    channel: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=120)
    applied_at: datetime | None = None
    extra_data: dict[str, Any] | None = None


class ApplicationRead(ORMModel):
    id: int
    candidate_id: int
    job_id: int
    job_version_id: int
    source_id: int | None
    channel: str | None
    owner: str | None
    status: ApplicationStatus
    applied_at: datetime | None
    status_changed_at: datetime
    decision_reason: str | None
    extra_data: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ApplicationTransition(BaseModel):
    status: ApplicationStatus
    actor: str = Field(min_length=1, max_length=120)
    is_human_action: bool = False
    reason: str | None = None


class EvaluationDimension(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    score: float = Field(ge=0, le=100)
    weight: float = Field(gt=0, le=1)
    jd_requirement: str | None = None
    resume_evidence: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class EvaluationCreate(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    prompt_version: str | None = Field(default=None, max_length=50)
    evaluation_version: str = Field(default="1.0", max_length=30)
    dimensions: list[EvaluationDimension] = Field(min_length=1)
    reason: str = Field(min_length=1)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_weights(self) -> EvaluationCreate:
        total = sum(item.weight for item in self.dimensions)
        if abs(total - 1.0) > 0.001:
            raise ValueError("评估维度权重之和必须为 1")
        return self


class EvaluationRead(ORMModel):
    id: int
    application_id: int
    job_version_id: int
    status: EvaluationStatus
    evaluation_version: str
    model: str
    prompt_version: str | None
    scores: list[dict[str, Any]]
    total_score: float
    reason: str
    missing_information: list[str] | None
    created_at: datetime


class InterviewCreate(BaseModel):
    round: int = Field(default=1, ge=1)
    interviewers: list[dict[str, Any]] = Field(default_factory=list)
    booking_token: str | None = Field(default=None, max_length=255)
    booking_token_expires_at: datetime | None = None


class InterviewSlotCreate(BaseModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_time_range(self) -> InterviewSlotCreate:
        if self.end_at <= self.start_at:
            raise ValueError("面试结束时间必须晚于开始时间")
        return self


class InterviewSlotRead(ORMModel):
    id: int
    interview_id: int
    start_at: datetime
    end_at: datetime
    status: InterviewSlotStatus


class InterviewRead(ORMModel):
    id: int
    application_id: int
    round: int
    status: InterviewStatus
    interviewers: list[dict[str, Any]] | None
    confirmed_start_at: datetime | None
    confirmed_end_at: datetime | None
    booking_token_expires_at: datetime | None
    meeting_url: str | None
    meeting_status: str | None
    calendar_status: str | None
    feedback: dict[str, Any] | None
    created_at: datetime


class FollowUpCreate(BaseModel):
    target_type: str = Field(min_length=1, max_length=50)
    target_id: int
    rule_code: str = Field(min_length=1, max_length=100)
    window_key: str = Field(min_length=1, max_length=100)
    owner: str | None = Field(default=None, max_length=120)
    due_at: datetime | None = None
    reason: str | None = None
    suggested_action: str | None = None
    reminder_draft: str | None = None
    next_check_at: datetime | None = None


class FollowUpRead(ORMModel):
    id: int
    target_type: str
    target_id: int
    rule_code: str
    window_key: str
    status: FollowUpStatus
    owner: str | None
    due_at: datetime | None
    reason: str | None
    suggested_action: str | None
    reminder_draft: str | None
    next_check_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class AuditLogRead(ORMModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: int | None
    before_data: dict[str, Any] | None
    after_data: dict[str, Any] | None
    details: dict[str, Any] | None
    created_at: datetime

