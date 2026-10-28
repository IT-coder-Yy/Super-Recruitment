from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditLog, Candidate, CandidateSource, DedupStatus, Evaluation
from app.services.auth import current_actor
from copy import deepcopy
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

    weak_match = session.scalar(select(Candidate).where(or_(
        Candidate.normalized_name == normalized_name, Candidate.normalized_phone == normalized_phone
    )).limit(1)) if normalized_name and normalized_phone else None
    if weak_match:
        structured = deepcopy(values.get("structured_data") or {})
        structured.setdefault("quality", {}).setdefault("warnings", []).append("存在同名或同手机号的其他记录，请 HR 核对身份；未自动合并")
        values["structured_data"] = structured
    candidate = Candidate(
        name=values.get("name"),
        normalized_name=normalized_name,
        phone=values.get("phone"),
        normalized_phone=normalized_phone,
        email=values.get("email"),
        structured_data=values.get("structured_data"),
        dedup_status=(
            DedupStatus.CONFIRMED
            if normalized_name and normalized_phone and not weak_match
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

    candidate_values = _to_dict(candidate_data)
    validate_identity(candidate_values)
    candidate, candidate_created = get_or_create_candidate(
        session, candidate_values, actor=actor
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
            actor=current_actor.get() or actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            after_data=after_data,
        )
    )
    session.flush()


def validate_identity(values: dict[str, Any]) -> None:
    name = str(values.get("name") or "").strip()
    phone = normalize_phone(values.get("phone")) or ""
    email = str(values.get("email") or "").strip()
    if not normalize_name(name) or name in {"待补充", "待补充姓名", "未知"}:
        raise ValueError("请填写有效姓名")
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise ValueError("请填写有效手机号（支持中国大陆手机号及 +86／0086 前缀）")
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", email):
        raise ValueError("请填写有效邮箱")
    values.update(name=name, phone=phone, email=email)


def candidate_facts(candidate: Candidate, *, sources: list[CandidateSource] | None = None) -> tuple[dict[str, Any], set[str]]:
    """兼容旧人工补充，只返回副本；新版明确修改（包括清空）始终优先。"""
    from app.services.imports import build_rule_parse

    structured = deepcopy(candidate.structured_data or {})
    if not structured.get("candidate"):
        sources = candidate.sources if sources is None else sources
        parsed = build_rule_parse("\n".join(source.raw_resume or "" for source in sources))
        structured = {**parsed, **structured, "candidate": parsed["candidate"]}
    legacy = structured.get("manual_supplement") or {}
    if not legacy:
        return structured, set()
    facts = structured["candidate"]
    summary = structured.get("human_summary") or {}
    fields = set(legacy.get("fields") or [])
    # 旧 fields 只记录最后一次提交；这些差异/标记能识别更早的人工补充。
    if summary.get("skills") and summary["skills"] != facts.get("skills", []):
        fields.add("skills")
    projects = summary.get("projects") or []
    if projects and all(re.fullmatch(r"人工补充经历 \d+", project.get("title", "")) for project in projects):
        fields.add("experience")
    if summary.get("note"):
        fields.add("note")
    fields -= set(structured.get("manual_fields") or [])
    migrated = set()
    if "skills" in fields and "skills" in summary:
        facts["skills"] = deepcopy(summary["skills"])
        migrated.add("skills")
    if "experience" in fields and "projects" in summary:
        facts["project_experience"] = [{"name": project.get("title", "人工确认经历"),
                                        "description": project.get("description", ""),
                                        "evidence": project.get("description", "")} for project in projects]
        migrated.add("experience")
    if "note" in fields and "note" in summary:
        facts["note"] = summary["note"]
        migrated.add("note")
    if migrated:
        structured["manual_fields"] = sorted(set(structured.get("manual_fields") or []) | migrated)
    return structured, migrated


def revise_candidate(session: Session, candidate: Candidate, changes: dict[str, Any], *, version: int | None = None, application_id: int | None = None) -> None:
    from app.services.workflow import write_audit_log
    from app.services.imports import build_human_summary

    from sqlalchemy import update
    session.execute(update(Candidate).where(Candidate.id == candidate.id).values(id=Candidate.id))
    session.refresh(candidate)
    before = deepcopy(candidate.structured_data or {})
    revision = int(before.get("revision", 0))
    if version is not None and version != revision:
        raise ValueError("资料已更新，请刷新后再保存")
    identity = {key: changes.get(key, getattr(candidate, key)) for key in ("name", "phone", "email")}
    validate_identity(identity)
    duplicate = find_duplicate_candidate(session, name=identity["name"], phone=identity["phone"])
    if duplicate and duplicate.id != candidate.id:
        raise ValueError("姓名与手机号已属于其他候选人，请人工核对，未自动合并")
    structured, migrated = candidate_facts(candidate)
    facts = structured.setdefault("candidate", {})
    facts.update(identity)
    if "skills" in changes:
        facts["skills"] = [x.strip() for x in re.split(r"[\n,，;；]", changes["skills"] or "") if x.strip()]
    if "experience" in changes:
        facts["project_experience"] = [{"name": "人工确认经历", "description": x.strip(), "evidence": x.strip()}
                                       for x in (changes["experience"] or "").splitlines() if x.strip()]
    if "chat_summary" in changes:
        applications = [a for a in candidate.applications if application_id is None or a.id == application_id]
        if len(applications) != 1:
            raise ValueError("沟通记录需要指定本次投递，避免混入其他岗位")
        application = applications[0]
        application.extra_data = {**(application.extra_data or {}), "manual_chat": changes["chat_summary"]}
        write_audit_log(session, actor="HR", action="application.chat_revised", entity_type="application", entity_id=application.id,
                        after_data={"manual_chat": changes["chat_summary"], "revision": revision + 1})
    if "note" in changes:
        facts["note"] = changes["note"]
    structured["revision"] = revision + 1
    structured["manual_fields"] = sorted(set(structured.get("manual_fields", [])) | set(changes))
    structured["human_summary"] = build_human_summary(structured)
    structured["human_summary"]["note"] = facts.get("note", "")
    for key, value in identity.items():
        setattr(candidate, key, value)
    candidate.normalized_name = normalize_name(candidate.name)
    candidate.normalized_phone = normalize_phone(candidate.phone)
    candidate.structured_data = structured
    changed_fields = set(changes) | migrated
    if changed_fields & {"skills", "experience", "chat_summary"}:
        for application in candidate.applications:
            if changed_fields == {"chat_summary"} and application_id and application.id != application_id:
                continue
            for evaluation in application.evaluations:
                evaluation.is_stale = True
    write_audit_log(session, actor="HR", action="candidate.supplemented", entity_type="candidate", entity_id=candidate.id,
                    before_data=before, after_data=structured, details={"revision": revision + 1})
    session.flush()
