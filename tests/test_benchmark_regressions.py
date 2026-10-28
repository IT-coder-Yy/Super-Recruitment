"""固定反例与不变性验证；不把Mock分数当作HR质量标准。"""
from copy import deepcopy
from pathlib import Path

import pytest

from app.ai.client import LLMClient
from app.ai.service import AIService, _mock_evaluation, _weekly_days
from app.services.imports import build_rule_parse


SAMPLES = Path(__file__).parent / "fixtures/demo"


def scores(data):
    return {item["name"]: item["score"] for item in _mock_evaluation(data)["dimensions"]}


@pytest.mark.parametrize("text,present,absent", [
    ("技能：JavaScript、MySQL、ReactNative", ["JavaScript", "MySQL"], ["Java", "SQL", "React"]),
    ("项目：未使用 FastAPI；使用 Flask", ["Flask"], ["FastAPI"]),
    ("项目：不使用 FastAPI，使用 Flask", ["Flask"], ["FastAPI"]),
    ("技能：无Java经验", [], ["Java"]),
    ("项目：队友负责 FastAPI，本人负责 React", ["React"], ["FastAPI"]),
    ("意向：希望从事Java开发\n技能：Python3.12", ["Python"], ["Java"]),
    ("项目：React无障碍修复并补充Playwright回归", ["React", "Playwright"], []),
])
def test_skills_are_positive_bounded_and_personal(text, present, absent):
    parsed = build_rule_parse(text)["candidate"]
    mock = AIService(LLMClient(provider="mock")).structure_candidate(text)["candidate"]
    assert parsed["skills"] == mock["skills"]
    assert set(present) <= set(parsed["skills"])
    assert not set(absent) & set(parsed["skills"])


@pytest.mark.parametrize("text", [
    "经历：课程项目，无企业实习。\n意向：每周可实习四天\n声明：公司均虚构",
    "实习：尚未参加企业实习；仅有课程项目",
    "实习：我没有实习经历；在学校做过课程项目",
    "工作经历：无工作经历；仅有课程项目",
])
def test_negative_experience_and_intent_are_not_employment(text):
    candidate = build_rule_parse(text)["candidate"]
    assert candidate["internship_experience"] == []
    assert candidate["work_experience"] == []
    assert candidate["summary"]


def test_explicit_sections_are_separate_and_deduplicated():
    text = "项目经历\n使用Python完成接口\n使用Python完成接口\n实习：虚构公司后端实习三个月，未说明职责细节\n工作经历：虚构公司正式任职\n意向：每周四天"
    candidate = build_rule_parse(text)["candidate"]
    assert len(candidate["project_experience"]) == 1
    assert len(candidate["internship_experience"]) == 1
    assert len(candidate["work_experience"]) == 1


@pytest.mark.parametrize("text,expected", [
    ("意向：每周可实习四天", 4), ("每周至少4天", 4),
    ("每周三天", 3), ("未说明可实习时间", None),
    ("每周四天；每周三天", None), ("每周4-5天", None),
    ("每周4天或5天", None), ("不能每周四天到岗", None),
    ("HR：每周四天可以吗？", None), ("我不保证每周四天", None),
    ("每周四天，但无法保证", None), ("HR：每周四天", None),
    ("每周四天\n目前不能每周四天", None),
])
def test_availability_does_not_invent_a_schedule(text, expected):
    assert _weekly_days(text)[0] == expected


@pytest.mark.parametrize("sample", [f"r{i:02d}" for i in range(1, 13)])
@pytest.mark.parametrize("job", ["backend", "frontend"])
def test_demo_duplicate_and_unrelated_entries_do_not_raise_scores(sample, job):
    raw = (SAMPLES / f"{sample}.txt").read_text(encoding="utf-8")
    jd = (SAMPLES / f"jd-{job}.txt").read_text(encoding="utf-8")
    data = build_rule_parse(raw, jd_text=jd)
    data["source_documents"] = [{"text": raw}]
    baseline = scores(data)
    changed = deepcopy(data)
    for field in ("project_experience", "internship_experience", "work_experience"):
        changed["candidate"][field] *= 3
        changed["candidate"][field].append({"evidence": "活动海报与视觉设计"})
    assert scores(changed) == baseline
    assert baseline["domain_match"] == 0
    assert baseline["communication_intent"] == (0 if sample == "r09" else 100)
    assert "未校准" in _mock_evaluation(data)["recommendation_reason"]


def test_no_evidence_means_no_base_score_and_confirmed_skills_win():
    data = {"candidate": {"skills": [], "summary": "熟练Python和FastAPI"}, "chat": {},
            "job": {"required_skills": ["Python", "FastAPI"]}}
    assert all(value == 0 for value in scores(data).values())


def test_current_application_chat_wins_over_resume_availability():
    data = build_rule_parse("技能：Python", jd_text="要求：Python；每周至少四天")
    data["source_documents"] = [{"text": "意向：每周可实习四天"}]
    data["chat"] = {"summary": "目前每周只能实习三天"}
    # 无法识别的安排也不能退回旧简历而宣布满足。
    assert scores(data)["communication_intent"] == 0
    data["chat"] = {"summary": "每周三天"}
    assert "少于" in _mock_evaluation(data)["missing_information"][-1]


def test_recruiter_message_is_not_candidate_availability():
    data = build_rule_parse("技能：Python", jd_text="要求：Python；每周至少四天")
    data["chat"] = {"messages": [{"role": "hr", "content": "每周四天"}]}
    assert scores(data)["communication_intent"] == 0
    data["chat"]["messages"].append({"role": "candidate", "content": "每周三天"})
    assert "少于" in _mock_evaluation(data)["missing_information"][-1]
    data["chat"]["summary"] = "HR已确认：每周四天"
    assert scores(data)["communication_intent"] == 100
