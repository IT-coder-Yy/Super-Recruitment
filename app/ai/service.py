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


KNOWN_SKILLS = (
    "Python",
    "FastAPI",
    "Django",
    "Flask",
    "Java",
    "Spring Boot",
    "JavaScript",
    "TypeScript",
    "React",
    "Vue",
    "SQL",
    "MySQL",
    "PostgreSQL",
    "SQLite",
    "Redis",
    "Docker",
    "Kubernetes",
    "Git",
    "Linux",
    "PyTorch",
    "TensorFlow",
    "LangChain",
    "RAG",
    "LLM",
    "大模型",
    "机器学习",
    "深度学习",
    "自然语言处理",
    "向量检索",
)

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


def _extract_skills(text: str) -> list[str]:
    lower = text.casefold()
    return [skill for skill in KNOWN_SKILLS if skill.casefold() in lower]


def _matching_lines(text: str, keywords: tuple[str, ...], limit: int = 8) -> list[str]:
    result = []
    for line in _lines(text):
        if any(keyword.casefold() in line.casefold() for keyword in keywords):
            result.append(line[:500])
        if len(result) >= limit:
            break
    return result


def _mock_structure(
    resume_text: str,
    chat_text: str,
    jd_text: str,
) -> dict[str, Any]:
    contacts = extract_contact_info(resume_text)
    name = _find_labeled_value(resume_text, ("姓名", "name", "候选人"))
    skills = _extract_skills(resume_text)
    project_lines = _matching_lines(
        resume_text,
        ("项目", "系统", "平台", "project"),
        limit=5,
    )
    internship_lines = _matching_lines(
        resume_text,
        ("实习", "intern"),
        limit=5,
    )
    work_lines = _matching_lines(
        resume_text,
        ("工作经历", "任职", "有限公司", "公司"),
        limit=5,
    )
    education_lines = _matching_lines(
        resume_text,
        ("大学", "学院", "本科", "硕士", "博士", "education"),
        limit=3,
    )

    title = _find_labeled_value(jd_text, ("岗位", "职位", "job title", "title"))
    if not title:
        title = next((line for line in _lines(jd_text) if len(line) <= 60), None)

    required_skills = _extract_skills(jd_text)
    responsibilities = _matching_lines(jd_text, ("负责", "职责", "工作内容"))
    preferred = _matching_lines(jd_text, ("优先", "加分"))
    experience_requirements = _matching_lines(
        jd_text,
        ("经验", "经历", "年及以上"),
    )
    education_requirements = _matching_lines(
        jd_text,
        ("学历", "本科", "硕士", "博士"),
    )
    domain = next(
        (
            value
            for value in (
                "人工智能",
                "大模型",
                "电商",
                "金融",
                "教育",
                "医疗",
                "游戏",
                "企业服务",
            )
            if value in jd_text
        ),
        None,
    )

    availability_evidence = _matching_lines(
        chat_text,
        ("到岗", "实习", "每周", "时间", "日期"),
        limit=3,
    )
    intent_evidence = _matching_lines(
        chat_text,
        ("应聘", "求职", "有意", "希望", "考虑"),
        limit=3,
    )
    start_date = _find_labeled_value(
        chat_text,
        ("到岗时间", "预计到岗", "start date"),
    )

    missing: list[str] = []
    for field, value in (
        ("candidate.name", name),
        ("candidate.phone", contacts["phone"]),
        ("candidate.skills", skills),
        ("job.title", title),
        ("job.required_skills", required_skills),
        ("chat.summary", _snippet(chat_text)),
    ):
        if not value:
            missing.append(field)

    evidence_count = sum(
        bool(value)
        for value in (name, skills, project_lines, title, required_skills, chat_text)
    )
    confidence = round(0.45 + evidence_count * 0.08, 2)

    def experience_items(
        lines: list[str],
        *,
        name_field: str,
    ) -> list[dict[str, str | None]]:
        return [
            {
                name_field: None,
                "role": None,
                "description": line,
                "evidence": line,
            }
            for line in lines
        ]

    return {
        "schema_version": STRUCTURE_SCHEMA_VERSION,
        "candidate": {
            "name": name,
            "summary": _snippet(resume_text),
            "skills": skills,
            "education": [
                {
                    "school": None,
                    "major": None,
                    "degree": None,
                    "evidence": line,
                }
                for line in education_lines
            ],
            "project_experience": experience_items(
                project_lines,
                name_field="name",
            ),
            "internship_experience": experience_items(
                internship_lines,
                name_field="company",
            ),
            "work_experience": experience_items(
                work_lines,
                name_field="company",
            ),
        },
        "chat": {
            "summary": _snippet(chat_text),
            "intent": _snippet("；".join(intent_evidence), limit=500),
            "availability": _snippet(
                "；".join(availability_evidence),
                limit=500,
            ),
            "start_date": start_date,
            "evidence": list(dict.fromkeys(intent_evidence + availability_evidence)),
        },
        "job": {
            "title": title,
            "responsibilities": responsibilities,
            "required_skills": required_skills,
            "preferred_skills": preferred,
            "experience_requirements": experience_requirements,
            "education_requirements": education_requirements,
            "domain": domain,
            "other_requirements": [],
        },
        "quality": {
            "confidence": min(confidence, 0.93),
            "missing_fields": missing,
            "warnings": ["当前结果由本地 Mock 规则生成"],
        },
    }


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


def _mock_evaluation(structured_data: dict[str, Any]) -> dict[str, Any]:
    candidate = _as_dict(structured_data.get("candidate"), "candidate")
    chat = _as_dict(structured_data.get("chat"), "chat")
    job = _as_dict(structured_data.get("job"), "job")

    candidate_skills = _string_list(candidate.get("skills"))
    required_skills = _string_list(job.get("required_skills"))
    candidate_text = " ".join(_flatten_strings(candidate))
    candidate_lower = candidate_text.casefold()
    matched_skills = [
        skill for skill in required_skills if skill.casefold() in candidate_lower
    ]
    if required_skills:
        required_score = round(100 * len(matched_skills) / len(required_skills), 2)
        required_confidence = 0.9
    else:
        required_score = 60
        required_confidence = 0.4

    projects = candidate.get("project_experience") or []
    project_score = min(100, 65 + len(projects) * 10) if projects else (
        35 if candidate_skills else 10
    )
    project_evidence = "；".join(
        _text(item.get("evidence"), limit=300)
        for item in projects[:3]
        if isinstance(item, Mapping) and item.get("evidence")
    )

    experiences = list(candidate.get("internship_experience") or []) + list(
        candidate.get("work_experience") or []
    )
    experience_requirements = _string_list(job.get("experience_requirements"))
    experience_score = min(100, 65 + len(experiences) * 8) if experiences else 20
    experience_evidence = "；".join(
        _text(item.get("evidence"), limit=300)
        for item in experiences[:3]
        if isinstance(item, Mapping) and item.get("evidence")
    )

    domain = _nullable_text(job.get("domain"), limit=200)
    if not domain:
        domain_score = 60
        domain_confidence = 0.35
        domain_evidence = ""
    elif domain.casefold() in candidate_lower:
        domain_score = 90
        domain_confidence = 0.85
        domain_evidence = domain
    else:
        domain_score = 30
        domain_confidence = 0.65
        domain_evidence = ""

    chat_summary = _nullable_text(chat.get("summary"), limit=1000)
    intent = _nullable_text(chat.get("intent"), limit=500)
    availability = _nullable_text(chat.get("availability"), limit=500)
    communication_score = 20 + (40 if intent else 0) + (40 if availability else 0)
    communication_evidence = "；".join(
        item for item in (intent, availability, chat_summary) if item
    )[:1000]

    missing: list[str] = []
    if not required_skills:
        missing.append("JD 未明确可识别的必备技能")
    if not projects:
        missing.append("未识别到明确的项目证据")
    if not experiences:
        missing.append("未识别到明确的实习或工作经历")
    if not domain:
        missing.append("JD 未明确业务领域")
    if not chat_summary:
        missing.append("缺少初步聊天信息")

    dimensions = [
        {
            "name": "required_skills",
            "score": required_score,
            "jd_requirement": "、".join(required_skills)
            or "JD 未明确可识别的必备技能",
            "resume_evidence": "、".join(matched_skills)
            or "未找到明确匹配证据",
            "missing_or_conflicting": [
                f"未找到技能证据：{skill}"
                for skill in required_skills
                if skill not in matched_skills
            ],
            "confidence": required_confidence,
        },
        {
            "name": "project_evidence",
            "score": project_score,
            "jd_requirement": "以 JD 职责和技能要求为准",
            "resume_evidence": project_evidence or "未找到明确项目证据",
            "missing_or_conflicting": [] if projects else ["缺少项目证据"],
            "confidence": 0.8 if projects else 0.45,
        },
        {
            "name": "relevant_experience",
            "score": experience_score,
            "jd_requirement": "；".join(experience_requirements)
            or "JD 未明确经验年限",
            "resume_evidence": experience_evidence or "未找到明确经历证据",
            "missing_or_conflicting": [] if experiences else ["缺少相关经历证据"],
            "confidence": 0.8 if experiences else 0.45,
        },
        {
            "name": "domain_match",
            "score": domain_score,
            "jd_requirement": domain or "JD 未明确业务领域",
            "resume_evidence": domain_evidence or "未找到明确领域证据",
            "missing_or_conflicting": [] if domain_evidence else ["领域证据不足"],
            "confidence": domain_confidence,
        },
        {
            "name": "communication_intent",
            "score": communication_score,
            "jd_requirement": "结合沟通信息核对求职意向与到岗安排",
            "resume_evidence": communication_evidence or "缺少聊天证据",
            "missing_or_conflicting": [] if chat_summary else ["缺少聊天信息"],
            "confidence": 0.85 if chat_summary else 0.3,
        },
    ]

    if required_skills and len(matched_skills) == len(required_skills):
        skill_summary = "必备技能均有明确匹配"
    elif matched_skills:
        skill_summary = "部分必备技能有明确匹配"
    else:
        skill_summary = "必备技能证据不足"
    reason = (
        f"{skill_summary}；项目、经历及求职意向评分均基于现有文本证据，"
        "缺失项建议在人工面试中确认。"
    )[:100]

    return {
        "evaluation_version": EVALUATION_VERSION,
        "dimensions": dimensions,
        "missing_information": missing,
        "recommendation_reason": reason,
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
