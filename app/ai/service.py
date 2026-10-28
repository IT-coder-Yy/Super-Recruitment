"""候选人结构化与 JD 证据化评分服务。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.ai.client import LLMClient, create_llm_client
from app.ai.prompts import (
    EVALUATION_VERSION,
    PROMPT_VERSION,
    SCORING_DIMENSIONS,
    STRUCTURE_SCHEMA_VERSION,
    build_evaluation_messages,
    build_structure_messages,
)
from app.services.redaction import extract_contact_info, redact_data, redact_text
from app.services.imports import build_rule_parse, _contains_term, _positive_fragments


EXPERIENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "project_experience": ("name", "role", "description", "evidence"),
    "internship_experience": ("company", "role", "description", "evidence"),
    "work_experience": ("company", "role", "description", "evidence"),
}


def _as_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} 必须是 JSON 对象")
    return dict(value)


def _nullable_text(value: Any, *, limit: int = 2000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("文本字段类型错误")
    text = value.strip()
    return text[:limit] if text else None


def _text(value: Any, *, limit: int = 2000) -> str:
    return _nullable_text(value, limit=limit) or ""


def _string_list(value: Any, *, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("列表字段类型错误")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("列表元素必须是字符串")
        text = item.strip()
        if text and text not in result:
            result.append(text[:500])
        if len(result) >= limit:
            break
    return result


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence 必须是数字")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError("confidence 必须在 0 到 1 之间")
    return round(number, 2)


def _score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score 必须是数字")
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError("score 必须在 0 到 100 之间")
    return round(number, 2)


def _object_list(
    value: Any,
    *,
    fields: tuple[str, ...],
    limit: int = 20,
) -> list[dict[str, str | None]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("经历字段必须是列表")
    result: list[dict[str, str | None]] = []
    for item in value[:limit]:
        entry = _as_dict(item, "经历项")
        normalized: dict[str, str | None] = {}
        for field in fields:
            normalized[field] = _nullable_text(entry.get(field), limit=1000)
        result.append(normalized)
    return result


def _normalize_structure(
    raw: dict[str, Any],
    contacts: dict[str, str | None],
) -> dict[str, Any]:
    candidate = _as_dict(raw.get("candidate"), "candidate")
    chat = _as_dict(raw.get("chat"), "chat")
    job = _as_dict(raw.get("job"), "job")
    quality = _as_dict(raw.get("quality"), "quality")

    normalized_candidate: dict[str, Any] = {
        "name": _nullable_text(candidate.get("name"), limit=100),
        "phone": contacts.get("phone"),
        "email": contacts.get("email"),
        "summary": _nullable_text(candidate.get("summary"), limit=2000),
        "skills": _string_list(candidate.get("skills"), limit=50),
        "education": _object_list(
            candidate.get("education"),
            fields=("school", "major", "degree", "evidence"),
        ),
    }
    for field, fields in EXPERIENCE_FIELDS.items():
        normalized_candidate[field] = _object_list(
            candidate.get(field),
            fields=fields,
        )

    return {
        "schema_version": STRUCTURE_SCHEMA_VERSION,
        "candidate": normalized_candidate,
        "chat": {
            "summary": _nullable_text(chat.get("summary"), limit=2000),
            "intent": _nullable_text(chat.get("intent"), limit=500),
            "availability": _nullable_text(chat.get("availability"), limit=500),
            "start_date": _nullable_text(chat.get("start_date"), limit=100),
            "evidence": _string_list(chat.get("evidence"), limit=20),
        },
        "job": {
            "title": _nullable_text(job.get("title"), limit=200),
            "responsibilities": _string_list(job.get("responsibilities")),
            "required_skills": _string_list(job.get("required_skills")),
            "preferred_skills": _string_list(job.get("preferred_skills")),
            "experience_requirements": _string_list(
                job.get("experience_requirements")
            ),
            "education_requirements": _string_list(
                job.get("education_requirements")
            ),
            "domain": _nullable_text(job.get("domain"), limit=200),
            "other_requirements": _string_list(job.get("other_requirements")),
        },
        "quality": {
            "confidence": _confidence(quality.get("confidence")),
            "missing_fields": _string_list(quality.get("missing_fields")),
            "warnings": _string_list(quality.get("warnings")),
        },
    }


def _normalize_evaluation(raw: dict[str, Any]) -> dict[str, Any]:
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, list):
        raise ValueError("dimensions 必须是列表")

    by_name: dict[str, dict[str, Any]] = {}
    for item in dimensions:
        dimension = _as_dict(item, "dimension")
        name = _text(dimension.get("name"), limit=100)
        if name in by_name:
            raise ValueError(f"评分维度重复: {name}")
        by_name[name] = dimension

    normalized_dimensions: list[dict[str, Any]] = []
    total = 0.0
    for definition in SCORING_DIMENSIONS:
        name = definition["name"]
        if name not in by_name:
            raise ValueError(f"缺少评分维度: {name}")
        item = by_name[name]
        score = _score(item.get("score"))
        weight = float(definition["weight"])
        weighted_score = round(score * weight, 2)
        total += weighted_score
        normalized_dimensions.append(
            {
                "name": name,
                "label": definition["label"],
                "score": score,
                "weight": weight,
                "weighted_score": weighted_score,
                "jd_requirement": _text(
                    item.get("jd_requirement"),
                    limit=1000,
                ),
                "resume_evidence": _text(
                    item.get("resume_evidence"),
                    limit=1000,
                ),
                "missing_or_conflicting": _string_list(
                    item.get("missing_or_conflicting"),
                    limit=20,
                ),
                "confidence": _confidence(item.get("confidence")),
                "references": item.get("references", []),
            }
        )

    reason = _text(raw.get("recommendation_reason"), limit=100)
    return {
        "evaluation_version": EVALUATION_VERSION,
        "dimensions": normalized_dimensions,
        "total_score": round(total, 2),
        "missing_information": _string_list(
            raw.get("missing_information"),
            limit=50,
        ),
        "recommendation_reason": reason,
    }


def _lines(text: str) -> list[str]:
    return [line.strip(" \t-•") for line in text.splitlines() if line.strip()]


def _snippet(text: str, *, limit: int = 300) -> str | None:
    compact = " ".join(text.split())
    return compact[:limit] if compact else None


def _find_labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:^|\n)\s*(?:{label_pattern})\s*[:：]\s*([^\n]+)",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def _matching_lines(text: str, keywords: tuple[str, ...], limit: int = 8) -> list[str]:
    result = []
    for line in _lines(text):
        if any(keyword.casefold() in line.casefold() for keyword in keywords):
            result.append(line[:500])
        if len(result) >= limit:
            break
    return result


def _mock_structure(resume_text: str, chat_text: str, jd_text: str) -> dict[str, Any]:
    title = _find_labeled_value(jd_text, ("岗位", "职位", "job title", "title"))
    if not title:
        title = next((line for line in _lines(jd_text) if len(line) <= 60), None)
    result = build_rule_parse(resume_text, job_title=title, jd_text=jd_text)
    result["chat"] = {
        "summary": _snippet(chat_text),
        "intent": _snippet("；".join(_matching_lines(chat_text, ("应聘", "求职", "有意", "希望")))),
        "availability": _snippet("；".join(_matching_lines(chat_text, ("到岗", "每周", "时间")))),
        "start_date": _find_labeled_value(chat_text, ("到岗时间", "预计到岗", "start date")),
        "evidence": _lines(chat_text),
    }
    result["job"]["domain"] = _find_labeled_value(jd_text, ("业务领域", "domain"))
    return result


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    return []


def _weekly_days(text: str) -> tuple[int | None, str]:
    # shortcut: 仅识别明确的每周N天；区间、冲突和复杂安排交HR核对。
    text = re.sub(r"(?m)^\s*(?:意向|到岗意向|求职意向)\s*[:：]", "", text)
    found = {}
    lines = [line for line in text.splitlines()
             if not re.match(r"\s*(?:HR|招聘方|招聘人员|面试官|recruiter)\s*[:：]", line, re.IGNORECASE)]
    if any("每周" in line and re.search(r"[?？]|吗|能否|是否|不能|没有|无法|不保证|不确定|未确定|不一定|可能|大概|最多", line) for line in lines):
        return None, ""
    for fragment in _positive_fragments("\n".join(lines)):
        for match in re.finditer(r"每周\s*(?:可|至少|能够|工作|实习|出勤|到岗|\s)*([一二三四五六七1-7])\s*天(?!\s*(?:或|到|至|[-~～/]))", fragment):
            value = match.group(1)
            days = int(value) if value.isdigit() else "一二三四五六七".index(value) + 1
            found[days] = fragment
    return next(iter(found.items())) if len(found) == 1 else (None, "")


def _mock_evaluation(structured_data: dict[str, Any]) -> dict[str, Any]:
    candidate = _as_dict(structured_data.get("candidate"), "candidate")
    chat = _as_dict(structured_data.get("chat"), "chat")
    job = _as_dict(structured_data.get("job"), "job")
    required_skills = _string_list(job.get("required_skills"))
    candidate_skills = _string_list(candidate.get("skills"))
    # 人工确认的技能优先，不能从summary或旧简历把删除的技能重新加回来。
    matched_skills = [skill for skill in required_skills
                      if any(skill.casefold() == value.casefold() for value in candidate_skills)]

    def coverage(matches: list[str]) -> float:
        return round(100 * len(matches) / len(required_skills), 2) if required_skills else 0

    def evidence_for(fields: tuple[str, ...]) -> tuple[str, list[str]]:
        evidence = list(dict.fromkeys(
            _text(item.get("evidence") or item.get("description"), limit=1000)
            for field in fields for item in candidate.get(field) or []
            if isinstance(item, Mapping) and (item.get("evidence") or item.get("description"))
        ))
        positive = "\n".join(_positive_fragments("\n".join(evidence)))
        matches = [skill for skill in required_skills if _contains_term(positive, skill)]
        return "；".join(evidence)[:1000], matches

    project_evidence, project_matches = evidence_for(("project_experience",))
    experience_evidence, experience_matches = evidence_for(("internship_experience", "work_experience"))
    domain = _nullable_text(job.get("domain"), limit=200)
    domain_text = "\n".join(_positive_fragments(project_evidence + "\n" + experience_evidence))
    domain_match = bool(domain and _contains_term(domain_text, domain))
    # 仅用本投递聊天；无聊天时参考简历自述，不推断为已核实。
    chat_text = _text(chat.get("summary")) or "\n".join(
        _text(message.get("content")) for message in chat.get("messages") or []
        if isinstance(message, Mapping) and message.get("role") == "candidate"
    ) or "\n".join(_flatten_strings({key: chat.get(key) for key in ("intent", "availability")}))
    availability_source = "本投递沟通" if chat_text.strip() else "简历自述（未核实）"
    availability_text = chat_text if chat_text.strip() else "\n".join(
        _text(document.get("text"), limit=100000) for document in structured_data.get("source_documents") or []
    ) or _text(candidate.get("summary"))
    available_days, availability_evidence = _weekly_days(availability_text)
    requirement_text = job.get("raw_jd") or "\n".join(_flatten_strings(job))
    required_days, _ = _weekly_days(requirement_text)
    availability_known = available_days is not None and required_days is not None
    availability_matches = availability_known and available_days >= required_days

    skill_missing = [f"未找到已确认技能：{skill}" for skill in required_skills if skill not in matched_skills]
    full_requirements = _string_list(job.get("other_requirements")) or ([requirement_text] if requirement_text else [])
    missing = ["Mock未校准：分数仅为词项/明确出勤覆盖演示值，不代表熟练度、完整JD符合或录用建议",
               "项目与实习/工作仅检查明确技术词；迁移能力、本人职责、测试质量及经验计分口径需人工确认"]
    missing.extend(f"完整要求仍待逐项核对：{value}" for value in full_requirements)
    missing.extend(skill_missing)
    if not required_skills:
        missing.append("JD没有可识别的必备技能，覆盖分未知")
    if not domain:
        missing.append("JD未明确业务领域；缺证记0不代表能力不足")
    if not availability_known:
        availability_note = "每周出勤要求或可用天数未知/冲突，暂不计算意向分；不等于不满足"
    elif availability_matches:
        availability_note = f"{availability_source}每周{available_days}天，达到文本中的{required_days}天要求；仍需HR核实"
    else:
        availability_note = f"{availability_source}每周{available_days}天，少于文本中的{required_days}天要求；由HR核对"
    missing.append(availability_note)

    values = (
        ("required_skills", coverage(matched_skills), "、".join(required_skills), "、".join(matched_skills), skill_missing),
        ("project_evidence", coverage(project_matches), "项目中已识别技能的覆盖", project_evidence,
         [f"项目未找到明确词项：{skill}" for skill in required_skills if skill not in project_matches]),
        ("relevant_experience", coverage(experience_matches), "实习/工作中已识别技能的覆盖，相关性待核实", experience_evidence,
         [f"实习/工作未找到明确词项：{skill}" for skill in required_skills if skill not in experience_matches]),
        ("domain_match", 100 if domain_match else 0, domain or "JD未明确业务领域", domain if domain_match else "",
         [] if domain_match else ["领域证据不足，未知"]),
        ("communication_intent", 100 if availability_matches else 0, "仅核对JD明确的每周出勤天数", availability_evidence, [availability_note]),
    )
    dimensions = [{"name": name, "score": score, "jd_requirement": requirement or "未知",
                   "resume_evidence": evidence or "未找到明确证据，未知",
                   "missing_or_conflicting": notes, "confidence": 0.3 if evidence else 0}
                  for name, score, requirement, evidence, notes in values]
    return {
        "evaluation_version": EVALUATION_VERSION,
        "dimensions": dimensions,
        "missing_information": missing,
        "recommendation_reason": "Mock未校准：仅演示已确认技能及经历词项、明确出勤的覆盖；不按条目数加分，完整要求与匹配合理性须人工核对。",
    }


class AIService:
    """面向主线程的同步 AI 服务，所有公开结果均为普通 dict。"""

    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or create_llm_client()

    def structure_candidate(
        self,
        resume_text: str,
        chat_text: str = "",
        jd_text: str = "",
    ) -> dict[str, Any]:
        if not isinstance(resume_text, str):
            raise TypeError("resume_text 必须是字符串")
        if not isinstance(chat_text, str) or not isinstance(jd_text, str):
            raise TypeError("chat_text 和 jd_text 必须是字符串")

        contacts = extract_contact_info(resume_text)
        messages = build_structure_messages(
            resume_text=redact_text(resume_text),
            chat_text=redact_text(chat_text),
            jd_text=redact_text(jd_text),
        )
        result = self.client.generate_json(
            messages=messages,
            fallback=lambda: _mock_structure(resume_text, chat_text, jd_text),
            validator=lambda value: _normalize_structure(value, contacts),
        )
        output = result["content"]
        output["ai_meta"] = {
            **result["meta"],
            "prompt_version": PROMPT_VERSION,
        }
        return output

    def evaluate_candidate(
        self,
        structured_data: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(structured_data, dict):
            raise TypeError("structured_data 必须是 dict")

        safe_data = redact_data(
            {
                key: value
                for key, value in structured_data.items()
                if key != "ai_meta"
            }
        )
        messages = build_evaluation_messages(safe_data)
        result = self.client.generate_json(
            messages=messages,
            fallback=lambda: _mock_evaluation(structured_data),
            validator=_normalize_evaluation,
        )
        output = result["content"]
        output.update(
            {
                "model": result["meta"]["model"],
                "provider": result["meta"]["provider"],
                "fallback": result["meta"]["fallback"],
                "fallback_reason": result["meta"]["fallback_reason"],
                "prompt_version": PROMPT_VERSION,
            }
        )
        if safe_data.get("evidence_catalog") and result["meta"]["provider"] != "mock":
            catalog = safe_data["evidence_catalog"]
            for dimension in output["dimensions"]:
                references = dimension["references"]
                if not isinstance(references, list) or (dimension["score"] > 0 and not references):
                    raise ValueError("评分缺少可定位证据引用")
                for reference in references:
                    if not isinstance(reference, dict) or reference.get("id") not in catalog or not reference.get("quote") or reference["quote"] not in catalog[reference["id"]]:
                        raise ValueError("模型引用不存在或并非输入原文")
        return output

    def analyze_candidate(
        self,
        resume_text: str,
        chat_text: str = "",
        jd_text: str = "",
    ) -> dict[str, Any]:
        structured = self.structure_candidate(
            resume_text=resume_text,
            chat_text=chat_text,
            jd_text=jd_text,
        )
        evaluation = self.evaluate_candidate(structured)
        return {
            "structured": structured,
            "evaluation": evaluation,
        }


def structure_candidate(
    resume_text: str,
    chat_text: str = "",
    jd_text: str = "",
) -> dict[str, Any]:
    return AIService().structure_candidate(resume_text, chat_text, jd_text)


def evaluate_candidate(structured_data: dict[str, Any]) -> dict[str, Any]:
    return AIService().evaluate_candidate(structured_data)


def analyze_candidate(
    resume_text: str,
    chat_text: str = "",
    jd_text: str = "",
) -> dict[str, Any]:
    return AIService().analyze_candidate(resume_text, chat_text, jd_text)
