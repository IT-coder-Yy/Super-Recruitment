"""招聘信息结构化与证据化评分提示词。"""

from __future__ import annotations

import json
from typing import Any


PROMPT_VERSION = "1.0"
STRUCTURE_SCHEMA_VERSION = "1.0"
EVALUATION_VERSION = "1.0"

SCORING_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {"name": "required_skills", "label": "必备技能匹配", "weight": 0.35},
    {"name": "project_evidence", "label": "项目与成果证据", "weight": 0.25},
    {"name": "relevant_experience", "label": "实习/工作相关度", "weight": 0.20},
    {"name": "domain_match", "label": "业务领域匹配", "weight": 0.10},
    {"name": "communication_intent", "label": "沟通信息与求职意向", "weight": 0.10},
)


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


EVALUATION_SYSTEM_PROMPT = """
你是招聘匹配评估助手。根据结构化候选人、聊天和 JD 逐项查找证据并评分。
不得臆造证据，不得根据性别、年龄、民族、籍贯、婚育、照片、与岗位无关的健康信息
或其他敏感属性评分。缺乏信息时应降低置信度并写入 missing_information。
你不能给出录用、淘汰或通过决定。每个 score 必须是 0 到 100 的数字。
只输出一个合法 JSON 对象，不要输出 Markdown。

输出结构：
{
  "evaluation_version": "1.0",
  "dimensions": [
    {
      "name": "固定维度英文名",
      "score": number,
      "jd_requirement": string,
      "resume_evidence": string,
      "missing_or_conflicting": [string],
      "confidence": number
    }
  ],
  "missing_information": [string],
  "recommendation_reason": "不超过100个汉字的客观总结"
}

必须完整返回以下五个维度，名称不可修改：
required_skills、project_evidence、relevant_experience、domain_match、
communication_intent。权重由应用程序计算，不要自行计算总分。
""".strip()


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
    dimensions = [
        {"name": item["name"], "label": item["label"]}
        for item in SCORING_DIMENSIONS
    ]
    payload = {
        "structured_data": structured_data,
        "dimensions": dimensions,
    }
    return [
        {"role": "system", "content": EVALUATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]
