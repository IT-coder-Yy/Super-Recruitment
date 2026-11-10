"""证据摘要只读条款和核验结果，不采纳模型额外推断或重新打分。"""
from copy import deepcopy

import pytest

from app.ai.client import LLMClient
from app.ai.service import AIService, _evaluation_summary, _mock_evaluation, _normalize_evaluation
from app.services.imports import build_rule_parse


def test_model_narrative_cannot_upgrade_self_report_or_change_scoring():
    data = build_rule_parse("技能：Python、PyTorch\n项目：训练图片分类模型", jd_text="要求：Python；说明本人负责的实现")
    raw = _mock_evaluation(data)
    original = _normalize_evaluation(raw, data)
    raw["recommendation_reason"] = "已经用Python/PyTorch完成项目并通过HR核验" * 30
    snapshot = deepcopy(raw)
    result = _normalize_evaluation(raw, data)
    assert result == original and raw == snapshot
    assert "自述1项" in result["recommendation_reason"]
    assert "Python" not in result["recommendation_reason"]
    assert "已核实" not in result["recommendation_reason"]
    assert result["recommendation_reason"].endswith("详情交HR核实。")


@pytest.mark.parametrize("text,status", [
    ("每周四天", "自述满足"), ("每周三天", "明确冲突"),
    ("未说明", "未知"), ("HR已核实：每周四天", "已核实"),
])
def test_summary_attendance_uses_existing_verification_status(text, status):
    data = build_rule_parse("技能：Python", jd_text="要求：Python；每周四天")
    data["chat"] = {"summary": text, "summary_source": "hr_manual"}
    result = _normalize_evaluation(_mock_evaluation(data), data)
    assert f"每周出勤{status}" in result["recommendation_reason"]
    assert "到岗时间未知（均不计分）" in result["recommendation_reason"]


def test_summary_keeps_every_evidence_status_and_excludes_inapplicable_dimensions():
    statuses = ["quantified", "implemented", "transfer", "self_report", "unknown", "conflict"]
    dimensions = [{"applicable": True, "criteria": [{"status": value} for value in statuses]},
                  {"applicable": False, "criteria": [{"status": "quantified"}]}]
    checks = {"每周出勤": {"status": "自述满足"}, "到岗时间": {"status": "未知"}}
    snapshot = deepcopy((dimensions, checks))
    result = _evaluation_summary(dimensions, checks)
    assert "量化1项、实现1项、迁移1项、自述1项、缺证1项、冲突1项" in result
    assert result.endswith("详情交HR核实。") and len(result) <= 100
    assert (dimensions, checks) == snapshot


def test_mock_summary_still_discloses_uncalibrated_scores():
    data = build_rule_parse("技能：Python", jd_text="要求：Python")
    result = AIService(LLMClient(provider="mock")).evaluate_candidate(data)
    assert result["recommendation_reason"].startswith("Mock未校准；条款证据：")


def test_global_gaps_use_criterion_evidence_and_verified_attendance_only():
    data = build_rule_parse("技能：Python\n意向：每周可实习四天", jd_text="要求：Python；SQL；每周四天")
    raw = _mock_evaluation(data)
    criterion = raw["dimensions"][0]["criteria"][0]
    criterion["missing_or_conflicting"] = ["Python仅技能自述，实际使用待核实"]
    original = _normalize_evaluation(raw, data)
    raw["missing_information"] = ["仅一个项目，需要更多模块", "项目名称和角色字段为空", "本人角色未提供", "出勤仅见于摘要且已核实"]
    snapshot = deepcopy(raw)
    result = _normalize_evaluation(raw, data)
    assert result == original and raw == snapshot
    assert any("Python仅技能自述" in note for note in result["missing_information"])
    assert any("SQL：简历未提供" in note for note in result["missing_information"])
    assert "自述满足" in result["missing_information"][-1]
    assert not any("仅一个项目" in note or "字段为空" in note or "仅见于摘要" in note for note in result["missing_information"])


@pytest.mark.parametrize("applicable", [False, True])
def test_only_inapplicable_confidence_can_be_ignored(applicable):
    data = build_rule_parse("技能：Python", jd_text="要求：Python")
    raw = _mock_evaluation(data)
    raw["dimensions"][0 if applicable else 2]["confidence"] = None
    if applicable:
        with pytest.raises(ValueError, match="confidence"):
            _normalize_evaluation(raw, data)
    else:
        result = _normalize_evaluation(raw, data)
        assert result["total_score"] == 30
        assert result["dimensions"][2]["confidence"] == 0


@pytest.mark.parametrize("has_reference", [True, False])
def test_displayed_evidence_uses_quotes_without_model_reinterpretation(has_reference):
    text = "项目：训练图片分类模型，记录验证集精度；未部署 Web 服务。"
    data = build_rule_parse(text, jd_text="要求：Python接口开发；说明本人负责的实现")
    raw = _mock_evaluation(data)
    entry = raw["dimensions"][1]["criteria"][0]
    references = [{"id": "source:1", "quote": text}] if has_reference else []
    entry.update(score=0, status="unknown", resume_evidence="未说明本人具体动作", references=references,
                 missing_or_conflicting=["缺少目标岗位相关的后端接口实现证据"])
    before = deepcopy(raw)
    result = _normalize_evaluation(raw, data)
    project = result["dimensions"][1]
    expected = text if has_reference else "未提供可引用证据"
    assert project["resume_evidence"] == project["criteria"][0]["resume_evidence"] == expected
    assert project["criteria"][0]["references"] == references
    assert project["score"] == 0 and project["criteria"][0]["status"] == "unknown"
    assert any("后端接口实现" in note for note in result["missing_information"])
    assert raw == before
