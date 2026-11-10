"""招聘信息结构化与证据化评分提示词。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PROMPT_VERSION = "3.8-rules-v2.1"
STRUCTURE_SCHEMA_VERSION = "1.0"
EVALUATION_VERSION = "2.1-r1-r5"

SCORING_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {"name": "required_skills", "label": "必备技能匹配", "weight": 0.35},
    {"name": "project_evidence", "label": "项目与成果证据", "weight": 0.25},
    {"name": "relevant_experience", "label": "相关实践匹配", "weight": 0.20},
    {"name": "domain_match", "label": "业务领域匹配", "weight": 0.10},
    {"name": "communication_intent", "label": "出勤与到岗核验（不计分）", "weight": 0.0},
)


def build_scoring_contract(structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    """按JD明确条款分组；候选人缺证不得使该条款或其权重消失。"""
    job = structured_data.get("job") or {}
    grouped = {item["name"]: [] for item in SCORING_DIMENSIONS}
    if job.get("raw_jd"):
        sources = [(job["raw_jd"], "required_skills")]
    else:
        sources = [(str(value), dimension) for key, dimension in (
            ("required_skills", "required_skills"), ("responsibilities", "project_evidence"),
            ("experience_requirements", "relevant_experience"), ("other_requirements", "required_skills")
        ) for value in job.get(key) or []]
        if job.get("domain"):
            sources.append((f"业务领域：{job['domain']}", "domain_match"))
    seen = set()
    for source, default in sources:
        for raw in re.split(r"[；;\n]", source):
            raw = raw.strip(" \t-•")
            if not raw or raw == job.get("title") or re.fullmatch(r"[^，,：:；;]*(?:工程师|实习生)", raw):
                continue
            if re.match(r"^(?:岗位|职位|job title)[:：]", raw, re.I):
                requirement = re.search(r"(?:任职要求|岗位要求|要求|职责|工作内容)\s*[:：]?", raw)
                if not requirement:
                    if re.search(r"[，,]", raw):
                        raise ValueError("岗位标题与要求混写，请用换行或分号分开")
                    continue
                raw = raw[requirement.end():]
            text = re.sub(r"^(?:任职要求|岗位要求|要求|职责|工作内容)\s*[:：]\s*", "", raw)
            if not text or text in seen or re.match(r"^(?:不要求|无需)", text):
                continue
            seen.add(text)
            if re.search(r"每周|出勤|到岗|入职时间|实习时间", text):
                # shortcut: 混合条款先由HR拆分，避免技术要求被静默归为零权重。
                if re.search(r"[，,]|开发|熟悉|掌握|编程|技术|接口|测试|项目|经验", text):
                    raise ValueError("出勤与技术要求混写，请用分号分开后再评分")
                name = "communication_intent"
            elif re.match(r"^(?:业务领域|行业|domain)[:：]", text, re.I):
                name = "domain_match"
            elif re.search(r"经验|经历|实践|年限", text):
                name = "relevant_experience"
            elif re.search(r"项目|成果|本人|负责的实现", text):
                name = "project_evidence"
            else:
                name = default
            grouped[name].append({"id": f"{name}:{len(grouped[name]) + 1}", "text": text})
    total = sum(item["weight"] for item in SCORING_DIMENSIONS if grouped[item["name"]])
    return [{**item, "base_weight": item["weight"], "applicable": bool(grouped[item["name"]]) and item["weight"] > 0,
             "weight": item["weight"] / total if total and grouped[item["name"]] else 0,
             "requirements": grouped[item["name"]]} for item in SCORING_DIMENSIONS]


STRUCTURE_SYSTEM_PROMPT = """
你是招聘信息结构化助手。只提取输入中明确出现的信息，不得猜测或补全。
无法确定的标量返回 null，无法确定的列表返回空数组。证据必须来自输入原文。
联系方式可能已经脱敏，不要尝试还原。忽略性别、年龄、民族、籍贯、婚育、照片、
与岗位无关的健康信息等敏感属性。只输出一个合法 JSON 对象，不要输出 Markdown。

JSON 顶层必须且只能表达以下结构：
{
  "schema_version": "1.0",
  "candidate": {
    "name": string|null,
    "summary": string|null,
    "skills": [string],
    "education": [{"school": string|null, "major": string|null, "degree": string|null, "evidence": string}],
    "project_experience": [{"name": string|null, "role": string|null, "description": string|null, "evidence": string}],
    "internship_experience": [{"company": string|null, "role": string|null, "description": string|null, "evidence": string}],
    "work_experience": [{"company": string|null, "role": string|null, "description": string|null, "evidence": string}]
  },
  "chat": {
    "summary": string|null,
    "intent": string|null,
    "availability": string|null,
    "start_date": string|null,
    "evidence": [string]
  },
  "job": {
    "title": string|null,
    "responsibilities": [string],
    "required_skills": [string],
    "preferred_skills": [string],
    "experience_requirements": [string],
    "education_requirements": [string],
    "domain": string|null,
    "other_requirements": [string]
  },
  "quality": {
    "confidence": number,
    "missing_fields": [string],
    "warnings": [string]
  }
}
""".strip()


_EVALUATION_SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "job-match-evaluation" / "SKILL.md"


def _load_evaluation_skill() -> str:
    text = _EVALUATION_SKILL_PATH.read_text(encoding="utf-8")
    header, separator, body = text.partition("\n---\n")
    if not header.startswith("---\n") or not separator or not body.strip():
        raise ValueError("岗位匹配skill须有frontmatter和非空指令正文")
    return body.strip()


EVALUATION_SYSTEM_PROMPT = _load_evaluation_skill()


def build_structure_messages(
    *,
    resume_text: str,
    chat_text: str,
    jd_text: str,
) -> list[dict[str, str]]:
    payload = {
        "resume_text": resume_text,
        "chat_text": chat_text,
        "jd_text": jd_text,
    }
    return [
        {"role": "system", "content": STRUCTURE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]


def build_evaluation_messages(
    structured_data: dict[str, Any],
) -> list[dict[str, str]]:
    contract = build_scoring_contract(structured_data)
    dimensions = [
        {"name": item["name"], "label": item["label"]}
        for item in SCORING_DIMENSIONS
    ]
    payload = {
        "structured_data": structured_data,
        "dimensions": dimensions,
        "scoring_contract": contract,
        "output_template": {
            "evaluation_version": EVALUATION_VERSION,
            "dimensions": [{
                "name": item["name"],
                "criteria": [{
                    "id": requirement["id"], "resume_evidence": "", "references": [],
                    "implementation_evidence": "", "verification_evidence": "", "metric_evidence": "",
                    "missing_or_conflicting": [], "status": None, "score": None,
                } for requirement in item["requirements"] if item["applicable"]],
                "confidence": None,
            } for item in contract],
            "missing_information": [], "recommendation_reason": "",
        },
    }
    return [
        {"role": "system", "content": EVALUATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]
