"""候选人结构化与 JD 证据化评分服务。"""

from __future__ import annotations

import re
import json
from collections import Counter
from datetime import date
from collections.abc import Mapping, Sequence
from typing import Any

from app.ai.client import LLMClient, create_llm_client
from app.ai.prompts import (
    EVALUATION_VERSION,
    PROMPT_VERSION,
    SCORING_DIMENSIONS,
    STRUCTURE_SCHEMA_VERSION,
    build_evaluation_messages,
    build_scoring_contract,
    build_structure_messages,
)
from app.services.redaction import extract_contact_info, redact_data, redact_text
from app.services.imports import build_rule_parse, _contains_term, _positive_fragments, _extract_known_skills


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


def _evaluation_summary(dimensions: list[dict[str, Any]], checks: dict[str, Any]) -> str:
    # 摘要只汇总已校验条款，不再次推断技能与项目的关联。
    counts = Counter(entry["status"] for item in dimensions if item["applicable"] for entry in item["criteria"])
    labels = {"quantified": "量化", "implemented": "实现", "transfer": "迁移",
              "self_report": "自述", "unknown": "缺证", "conflict": "冲突"}
    evidence = "、".join(f"{label}{counts[status]}项" for status, label in labels.items() if counts[status])
    attendance = "；".join(f"{name}{check['status']}" for name, check in checks.items())
    return f"条款证据：{evidence}。{attendance}（均不计分）。缺证不代表不会，详情交HR核实。"


def _normalize_evaluation(raw: dict[str, Any], structured_data: dict[str, Any]) -> dict[str, Any]:
    contract = build_scoring_contract(structured_data)
    if not any(item["weight"] for item in contract):
        raise ValueError("JD未提供可评分的岗位要求，请先补充JD")
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, list):
        raise ValueError("dimensions必须是列表")
    by_name = {item.get("name"): item for item in dimensions if isinstance(item, dict)}
    if len(by_name) != len(dimensions) or set(by_name) != {item["name"] for item in contract}:
        raise ValueError("评分维度缺失、重复或未知")
    # 总缺口由计分条款和出勤核验汇总，避免模型在全局说明中新增门槛或改写来源。
    missing = []
    normalized = []
    for definition in contract:
        name = definition["name"]
        item = by_name[name]
        expected = definition["requirements"] if definition["applicable"] else []
        # 维度是否计分由程序决定；出勤另行核验，不采用模型额外返回的条目。
        entries = item.get("criteria") if definition["applicable"] else []
        if not isinstance(entries, list):
            raise ValueError("缺少逐条评分criteria")
        indexed = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
        if len(indexed) != len(entries) or set(indexed) != {entry["id"] for entry in expected}:
            raise ValueError("JD评分条款缺失、重复或被替换")
        criteria = []
        for requirement in expected:
            entry = indexed[requirement["id"]]
            score = _score(entry.get("score"))
            status = entry.get("status")
            allowed = {"unknown": (0,), "conflict": (0,), "self_report": (30,),
                       "implemented": (70,), "quantified": (100,), "transfer": (30, 70)}
            if status not in allowed or score not in allowed[status]:
                raise ValueError("要求评分不符合0/30/70/100证据档位")
            evidence = _text(entry.get("resume_evidence"))
            references = entry.get("references", [])
            if not isinstance(references, list):
                raise ValueError("references必须是列表")
            quotes = [str(ref.get("quote", "")) for ref in references if isinstance(ref, dict)] or [evidence]
            if score >= 70 and (not entry.get("implementation_evidence") or not any(entry["implementation_evidence"] in quote for quote in quotes)):
                raise ValueError("70/100分必须有逐字本人实现证据")
            if score == 100:
                verification, metric = entry.get("verification_evidence"), entry.get("metric_evidence")
                if not verification or not any(verification in quote for quote in quotes) or not metric or not any(metric in quote for quote in quotes) or not re.search(r"\d+(?:\.\d+)?\s*(?:%|ms|毫秒|秒|倍|条|个(?!月|人|项目)|次|QPS|TPS)", metric, re.I):
                    raise ValueError("100分须有成果/验证过程和相关量化指标的逐字证据")
                catalog = structured_data.get("evidence_catalog") or {}
                context = "\n".join(catalog.get(ref.get("id"), ref.get("quote", "")) for ref in references if isinstance(ref, dict)) or evidence
                metric_clauses = [part for part in re.split(r"[，,；;。\n]", context) if metric in part]
                if metric_clauses and all(re.search(r"团队|队友|全组|小组|公司|部门", part) and not re.search(r"本人|我(?:的|负责|实现|开发)", part) for part in metric_clauses):
                    raise ValueError("100分不能使用未归属本人的团队量化指标")
            notes = _string_list(entry.get("missing_or_conflicting"))
            if status == "unknown":
                notes.insert(0, "简历未提供该项证据，待HR核实；不代表不会")
            elif status == "conflict":
                notes.insert(0, "材料与要求存在明确冲突，交HR判断")
            elif status == "transfer":
                notes.insert(0, "存在可迁移依据；已覆盖内容及其余缺口仍需HR核实")
            missing.extend(f"{requirement['text']}：{note}" for note in notes)
            quoted_evidence = "；".join(dict.fromkeys(str(ref.get("quote", "")) for ref in references if isinstance(ref, dict) and ref.get("quote")))
            criteria.append({**entry, "requirement": requirement["text"], "score": score,
                             "resume_evidence": quoted_evidence or "未提供可引用证据",
                             "missing_or_conflicting": notes, "references": references})
        score = round(sum(entry["score"] for entry in criteria) / len(criteria), 2) if criteria else 0
        weight = definition["weight"]
        references = [ref for entry in criteria for ref in entry["references"]]
        normalized.append({"name": name, "label": definition["label"], "score": score,
                           "weight": weight, "weighted_score": round(score * weight, 2),
                           "applicable": definition["applicable"], "criteria": criteria,
                           "jd_requirement": "；".join(entry["text"] for entry in definition["requirements"]) or "JD未要求，不参与评分",
                           "resume_evidence": "；".join(dict.fromkeys(entry.get("resume_evidence", "") for entry in criteria)) or "未提供",
                           "references": references, "missing_or_conflicting": [note for entry in criteria for note in entry["missing_or_conflicting"]],
                           "confidence": _confidence(item.get("confidence", 0.3)) if definition["applicable"] else 0})
    checks = _eligibility_checks(structured_data)
    communication = next(item for item in normalized if item["name"] == "communication_intent")
    communication["eligibility"] = checks
    communication["resume_evidence"] = "；".join(f"{key}：{value['status']}；{value['note']}" for key, value in checks.items())
    missing.append(communication["resume_evidence"])
    return {"evaluation_version": EVALUATION_VERSION, "dimensions": normalized,
            "total_score": round(sum(item["score"] * item["weight"] for item in normalized), 2),
            "missing_information": list(dict.fromkeys(missing)),
            "recommendation_reason": _evaluation_summary(normalized, checks)}


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


def _eligibility_checks(structured_data: dict[str, Any]) -> dict[str, Any]:
    candidate, chat, job = (structured_data.get(key) or {} for key in ("candidate", "chat", "job"))
    chat_text = _text(chat.get("summary")) or "\n".join(
        _text(message.get("content")) for message in chat.get("messages") or []
        if isinstance(message, Mapping) and message.get("role") == "candidate"
    ) or "\n".join(_flatten_strings({key: chat.get(key) for key in ("intent", "availability")}))
    source = "本投递沟通" if chat_text.strip() else "简历自述"
    hr_verified = chat.get("summary_source") == "hr_manual" and bool(re.match(r"\s*HR已(?:确认|核实)[:：]", _text(chat.get("summary"))))
    available_text = chat_text if chat_text.strip() else "\n".join(
        _text(doc.get("text"), limit=100000) for doc in structured_data.get("source_documents") or []
    ) or _text(candidate.get("summary"))
    requirement_text = job.get("raw_jd") or "\n".join(_flatten_strings(job))
    available, evidence = _weekly_days(available_text)
    required, _ = _weekly_days(requirement_text)
    if available is None or required is None:
        status, note = "未知", "要求或可用天数未说明/有歧义，待HR核实"
    elif available < required:
        status, note = "明确冲突", f"{source}每周{available}天，少于要求的{required}天"
    else:
        status = "自述满足"
        note = f"{source}每周{available}天，满足文本要求{required}天，尚未经核实"
    # 只有本投递保存的人工核实说明才能标已核实，模型不能自批。
    if status == "自述满足" and hr_verified:
        status, note = "已核实", f"本投递HR记录：每周{available}天，要求{required}天"
    def start_date_in(text: str):
        text = re.sub(r"^\s*HR已(?:确认|核实)[:：]", "", text)
        values = set(re.findall(r"(?:^|\n)\s*(?:到岗时间|预计到岗)\s*[:：]\s*([^\n]+)", text.replace("；", "\n").replace(";", "\n")))
        if len(values) != 1:
            return None
        value = values.pop()
        try:
            return date.fromisoformat(value.strip()) if value else None
        except ValueError:
            return None
    start_text = available_text if re.search(r"到岗时间|预计到岗", available_text) else f"到岗时间：{chat.get('start_date') or ''}"
    start = start_date_in(start_text)
    deadline = start_date_in(requirement_text)
    start_status = "未知"
    start_note = f"自述：{start or '未提供明确日期'}；JD日期：{deadline or '未指定'}，待HR核实"
    if start and deadline:
        start_status = "自述满足" if start <= deadline else "明确冲突"
        start_note = f"自述到岗{start}，JD要求不晚于{deadline}"
        if start_status == "自述满足" and hr_verified:
            start_status = "已核实"
    return {"每周出勤": {"status": status, "note": note, "evidence": evidence},
            "到岗时间": {"status": start_status, "note": start_note}}


def _mock_evaluation(structured_data: dict[str, Any]) -> dict[str, Any]:
    candidate = _as_dict(structured_data.get("candidate"), "candidate")
    skills = _string_list(candidate.get("skills"))
    job_terms = _extract_known_skills((structured_data.get("job") or {}).get("raw_jd") or "\n".join(_flatten_strings(structured_data.get("job") or {})))
    dimensions = []
    for definition in build_scoring_contract(structured_data):
        name = definition["name"]
        fields = ("project_experience", "internship_experience", "work_experience")
        practice = list(dict.fromkeys(_text(item.get("evidence") or item.get("description"))
                    for field in fields for item in candidate.get(field) or [] if isinstance(item, Mapping)))
        criteria = []
        for requirement in definition["requirements"] if definition["applicable"] else []:
            terms = _extract_known_skills(requirement["text"])
            matched = terms and all(any(term.casefold() == skill.casefold() for skill in skills) for term in terms)
            score, status, evidence, implementation = (30, "self_report", "、".join(terms), "") if matched else (0, "unknown", "", "")
            for line in practice:
                positive = "；".join(_positive_fragments(line))
                # shortcut: Mock只识别显式技术词和本人动作，复合要求语义留给真实模型/HR。
                supported = terms and all(_contains_term(positive, term) for term in terms)
                if name == "project_evidence" and "本人" in requirement["text"]:
                    supported = bool(re.search(r"本人|独立|负责", positive)) and any(_contains_term(positive, term) for term in job_terms)
                if supported and re.search(r"编写|实现|开发|修复|完成|负责", positive):
                    score, status, evidence, implementation = 70, "implemented", line, line
                    break
            references = []
            catalog = (structured_data.get("evidence_catalog") or {}).items()
            if status == "self_report":
                catalog = sorted(catalog, key=lambda item: not item[0].endswith(":skills"))
            for quote in terms if status == "self_report" else ([evidence] if score else []):
                reference = next(({"id": key, "quote": quote} for key, text in catalog if quote and quote in text), None)
                if reference and reference not in references:
                    references.append(reference)
            criteria.append({"id": requirement["id"], "score": score, "status": status,
                             "resume_evidence": evidence, "implementation_evidence": implementation,
                             "verification_evidence": "", "metric_evidence": "", "references": references,
                             "missing_or_conflicting": ["Mock仅作保守词项演示，完整条款及本人贡献仍待语义审核"]})
        dimensions.append({"name": name, "criteria": criteria, "confidence": 0.3,
                           "score": round(sum(row["score"] for row in criteria) / len(criteria), 2) if criteria else 0})
    return {"evaluation_version": EVALUATION_VERSION, "dimensions": dimensions,
            "missing_information": ["Mock未校准；0是缺证占位，不能用于断言能力不足或自动招聘决定"],
            "recommendation_reason": "Mock未校准：按R1—R5演示0/30/70档位；完整语义和量化证据待HR核对，出勤不计分，同岗同规则才可比较。"}


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
        output["job"]["raw_jd"] = jd_text
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
        if not safe_data.get("evidence_catalog"):
            safe_data["evidence_catalog"] = {
                f"candidate:{key}": json.dumps(value, ensure_ascii=False)
                for key, value in (safe_data.get("candidate") or {}).items()
                if key not in {"name", "phone", "email"} and value
            }
        messages = build_evaluation_messages(safe_data)
        result = self.client.generate_json(
            messages=messages,
            fallback=lambda: _mock_evaluation(safe_data),
            validator=lambda raw: _normalize_evaluation(raw, safe_data),
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
        if result["meta"]["provider"] == "mock":
            output["recommendation_reason"] = "Mock未校准；" + output["recommendation_reason"]
        else:
            catalog = safe_data["evidence_catalog"]
            for dimension in output["dimensions"]:
                for entry in dimension["criteria"]:
                    references = entry["references"]
                    if (entry["score"] > 0 or entry["status"] == "conflict") and not references:
                        raise ValueError("评分或冲突判断缺少可定位证据引用")
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
