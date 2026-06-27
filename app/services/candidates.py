from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditLog, Candidate, CandidateSource, DedupStatus
from app.schemas import CandidateCreate, CandidateSourceCreate


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    normalized = unicodedata.normalize("NFKC", name).strip()
    normalized = "".join(character for character in normalized if character.isalnum())
    normalized = normalized.casefold()
    return normalized or None


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    normalized = unicodedata.normalize("NFKC", phone)
    digits = re.sub(r"\D", "", normalized)
    if digits.startswith("0086") and len(digits) == 15:
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits or None


def find_duplicate_candidate(
    session: Session,
    *,
    name: str | None,
    phone: str | None,
) -> Candidate | None:
    normalized_name = normalize_name(name)
    normalized_phone = normalize_phone(phone)
    if not normalized_name or not normalized_phone:
        return None
    return session.scalar(
        select(Candidate).where(
            Candidate.normalized_name == normalized_name,
            Candidate.normalized_phone == normalized_phone,
        )
    )


def get_or_create_candidate(
    session: Session,
    data: CandidateCreate | Mapping[str, Any],
    *,
    actor: str = "system",
) -> tuple[Candidate, bool]:
    values = _to_dict(data)
    normalized_name = normalize_name(values.get("name"))
    normalized_phone = normalize_phone(values.get("phone"))

    if normalized_name and normalized_phone:
        existing = session.scalar(
            select(Candidate).where(
                Candidate.normalized_name == normalized_name,
                Candidate.normalized_phone == normalized_phone,
            )
        )
        if existing:
            return existing, False

    candidate = Candidate(
        name=values.get("name"),
        normalized_name=normalized_name,
        phone=values.get("phone"),
        normalized_phone=normalized_phone,
        email=values.get("email"),
        structured_data=values.get("structured_data"),
        dedup_status=(
            DedupStatus.CONFIRMED
            if normalized_name and normalized_phone
            else DedupStatus.NEEDS_REVIEW
        ),
    )

    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
    except IntegrityError:
        if not normalized_name or not normalized_phone:
            raise
        existing = session.scalar(
            select(Candidate).where(
                Candidate.normalized_name == normalized_name,
                Candidate.normalized_phone == normalized_phone,
            )
        )
        if existing is None:
            raise
        return existing, False

    _write_audit(
        session,
        actor=actor,
        action="candidate.created",
        entity_type="candidate",
        entity_id=candidate.id,
        after_data={
            "name": candidate.name,
            "normalized_name": candidate.normalized_name,
            "normalized_phone": candidate.normalized_phone,
            "dedup_status": candidate.dedup_status.value,
        },
    )
    return candidate, True


def get_or_create_source_record(
    session: Session,
    *,
    candidate_id: int,
    data: CandidateSourceCreate | Mapping[str, Any],
    actor: str = "system",
) -> tuple[CandidateSource, bool]:
    values = _to_dict(data)
    platform = str(values["platform"]).strip().casefold()
    source_application_id = str(values["source_application_id"]).strip()

    existing = session.scalar(
        select(CandidateSource).where(
            CandidateSource.platform == platform,
            CandidateSource.source_application_id == source_application_id,
        )
    )
    if existing:
        if existing.candidate_id != candidate_id:
            raise ValueError("同一来源投递记录不能关联到不同候选人")
        return existing, False

    source = CandidateSource(
        candidate_id=candidate_id,
        platform=platform,
        source_candidate_id=values.get("source_candidate_id"),
        source_application_id=source_application_id,
        channel=values.get("channel"),
        raw_payload=values.get("raw_payload"),
        raw_resume=values.get("raw_resume"),
        raw_chat=values.get("raw_chat"),
        captured_at=values.get("captured_at"),
    )
    try:
        with session.begin_nested():
            session.add(source)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(CandidateSource).where(
                CandidateSource.platform == platform,
                CandidateSource.source_application_id == source_application_id,
            )
        )
        if existing is None:
            raise
        if existing.candidate_id != candidate_id:
            raise ValueError("同一来源投递记录不能关联到不同候选人")
        return existing, False

    _write_audit(
        session,
        actor=actor,
        action="candidate_source.created",
        entity_type="candidate_source",
        entity_id=source.id,
        after_data={
            "candidate_id": source.candidate_id,
            "platform": source.platform,
            "source_application_id": source.source_application_id,
        },
    )
    return source, True


def ingest_candidate(
    session: Session,
    *,
    candidate_data: CandidateCreate | Mapping[str, Any],
    source_data: CandidateSourceCreate | Mapping[str, Any],
    actor: str = "system",
) -> tuple[Candidate, CandidateSource, bool]:
    source_values = _to_dict(source_data)
    platform = str(source_values["platform"]).strip().casefold()
    source_application_id = str(source_values["source_application_id"]).strip()
    existing_source = session.scalar(
        select(CandidateSource).where(
            CandidateSource.platform == platform,
            CandidateSource.source_application_id == source_application_id,
        )
    )
    if existing_source:
        return existing_source.candidate, existing_source, False

    candidate, candidate_created = get_or_create_candidate(
        session, candidate_data, actor=actor
    )
    source, _ = get_or_create_source_record(
        session,
        candidate_id=candidate.id,
        data=source_data,
        actor=actor,
    )
    return candidate, source, candidate_created


def _to_dict(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return dict(data)


def _write_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    after_data: dict[str, Any],
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            after_data=after_data,
        )
    )
    session.flush()
