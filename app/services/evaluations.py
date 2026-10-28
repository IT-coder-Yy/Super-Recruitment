"""岗位匹配只读分析；尝试、输入版本和成功结果独立保存。"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.service import AIService
from app.models import Application, ApplicationStatus, Candidate, EvaluationAttempt, JobVersion
from app.services.candidates import candidate_facts
from app.services.imports import build_rule_parse
from app.services.workflow import create_evaluation, lock_record, transition_application, write_audit_log


def _application_sources(application: Application):
    sources = [link.source for link in application.source_links]
    if not sources and application.source:
        sources = [application.source]
    return sources


def matching_input(application: Application) -> dict:
    sources = _application_sources(application)
    structured, _ = candidate_facts(application.candidate, sources=sources)
    structured["candidate"].update(name=application.candidate.name, phone=application.candidate.phone, email=application.candidate.email)
    structured["job"] = build_rule_parse("", job_title=application.job.title, jd_text=application.job_version.jd_raw)["job"]
    structured["job"]["raw_jd"] = application.job_version.jd_raw
    # 沟通属于投递，不能沿用候选人全局的其他岗位聊天。
    structured["chat"] = {"summary": (application.extra_data or {}).get("manual_chat", ""),
                          "messages": [m for s in sources for m in (s.raw_chat or [])]}
    structured["source_documents"] = [{"source_id": s.id, "text": s.raw_resume or ""} for s in sources]
    structured.pop("human_summary", None)
    structured["candidate"].pop("note", None)
    structured["evidence_catalog"] = {
        f"source:{s.id}:line:{n}": line for s in sources
        for n, line in enumerate((s.raw_resume or "").splitlines(), 1) if line.strip()
    }
    for key, value in structured["candidate"].items():
        if key not in {"name", "phone", "email"} and value:
            structured["evidence_catalog"][f"facts:{structured.get('revision', 0)}:{key}"] = json.dumps(value, ensure_ascii=False)
    if structured["chat"]["summary"] or structured["chat"]["messages"]:
        structured["evidence_catalog"][f"application:{application.id}:chat"] = json.dumps(structured["chat"], ensure_ascii=False)
    structured["input_version"] = {"candidate_revision": structured.get("revision", 0),
                                   "job_version_id": application.job_version_id,
                                   "jd_hash": application.job_version.version_hash,
                                   "application_id": application.id}
    return structured


def evaluate_application_record(session: Session, application: Application) -> dict:
    structured, migrated = candidate_facts(application.candidate, sources=_application_sources(application))
    if migrated:
        lock_record(session, Candidate, application.candidate_id)
        structured, migrated = candidate_facts(application.candidate, sources=_application_sources(application))
    if migrated:
        structured["revision"] = int(structured.get("revision", 0)) + 1
        before = application.candidate.structured_data
        application.candidate.structured_data = structured
        if migrated & {"skills", "experience"}:
            for record in application.candidate.applications:
                for evaluation in record.evaluations:
                    evaluation.is_stale = True
        write_audit_log(session, actor="system", action="candidate.legacy_supplement_migrated", entity_type="candidate",
                        entity_id=application.candidate_id, before_data=before, after_data=structured,
                        details={"fields": sorted(migrated)})
    # 重评使用岗位当前 JD，历史评估保留自己的 JD 和输入快照。
    current = session.scalar(select(JobVersion).where(JobVersion.job_id == application.job_id, JobVersion.is_current.is_(True)))
    if current:
        application.job_version = current
    session.flush()
    snapshot = matching_input(application)
    attempt = EvaluationAttempt(application_id=application.id, input_snapshot=snapshot, status="running")
    session.add(attempt)
    session.commit()
    try:
        result = AIService().evaluate_candidate(snapshot)
        evaluation = create_evaluation(session, application.id, {
            "model": result["model"], "prompt_version": result["prompt_version"],
            "evaluation_version": result.get("evaluation_version", "1.0"),
            "dimensions": result["dimensions"], "reason": result["recommendation_reason"],
            "missing_information": result.get("missing_information", []),
        })
        evaluation.input_snapshot = snapshot
        # 模型调用期间若 HR 修改资料，新结果也必须标记过期。
        session.refresh(application.candidate)
        session.refresh(application)
        latest_jd = session.scalar(select(JobVersion).where(JobVersion.job_id == application.job_id, JobVersion.is_current.is_(True)))
        evaluation.is_stale = matching_input(application) != snapshot or bool(latest_jd and latest_jd.id != snapshot["input_version"]["job_version_id"])
        attempt.status, attempt.evaluation_id = "succeeded", evaluation.id
        sequence = [ApplicationStatus.CAPTURED, ApplicationStatus.PARSING, ApplicationStatus.NEEDS_REVIEW,
                    ApplicationStatus.PARSED, ApplicationStatus.DEDUPLICATED, ApplicationStatus.EVALUATING, ApplicationStatus.EVALUATION_REVIEW]
        if application.status in sequence[:-1]:
            remaining = sequence[sequence.index(application.status) + 1:]
            for target in remaining:
                if target == ApplicationStatus.NEEDS_REVIEW:
                    continue
                transition_application(session, application.id, target, actor="岗位匹配工作流", is_human_action=True)
        write_audit_log(session, actor="岗位匹配Agent", action="evaluation.succeeded", entity_type="application", entity_id=application.id,
                        details={"attempt_id": attempt.id, "evaluation_id": evaluation.id, "input_version": snapshot["input_version"]})
        session.commit()
        return {"evaluation_id": evaluation.id, "attempt_id": attempt.id, "total_score": evaluation.total_score,
                "reason": evaluation.reason, "provider": result["provider"], "fallback": False, "is_stale": evaluation.is_stale}
    except Exception as exc:
        session.rollback()
        attempt.status, attempt.error = "failed", f"岗位匹配失败（{type(exc).__name__}），检查模型配置后可重试"
        write_audit_log(session, actor="岗位匹配Agent", action="evaluation.failed", entity_type="application", entity_id=application.id,
                        details={"attempt_id": attempt.id, "error": attempt.error})
        session.commit()
        raise ValueError(attempt.error) from exc
