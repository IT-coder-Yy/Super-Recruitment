"""岗位匹配skill加载边界；不调用模型或验证生成质量。"""
from copy import deepcopy
import hashlib
import json
import re

import pytest

from app.ai import prompts


def test_runtime_skill_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = prompts._load_evaluation_skill()
    messages = prompts.build_evaluation_messages({"job": {"raw_jd": "要求：Python"}})
    assert messages[0]["content"] == body == prompts.EVALUATION_SYSTEM_PROMPT
    assert not body.startswith("---")
    assert "system_prompt_sha256:" not in messages[0]["content"]


def test_skill_metadata_is_not_sent_as_instructions(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: fixture\ndescription: 不应发送的元数据\n---\n\n只发送正文\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "_EVALUATION_SKILL_PATH", path)
    assert prompts._load_evaluation_skill() == "只发送正文"


@pytest.mark.parametrize("text", [
    "没有frontmatter",
    "---\nname: fixture\n正文未与元数据分隔",
    "---\nname: fixture\n---\n  \n",
])
def test_invalid_skill_is_rejected_instead_of_using_empty_or_fallback_prompt(tmp_path, monkeypatch, text):
    path = tmp_path / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(prompts, "_EVALUATION_SKILL_PATH", path)
    with pytest.raises(ValueError, match="非空指令正文"):
        prompts._load_evaluation_skill()


def test_missing_skill_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_EVALUATION_SKILL_PATH", tmp_path / "missing" / "SKILL.md")
    with pytest.raises(FileNotFoundError):
        prompts._load_evaluation_skill()


def test_skill_version_and_digest_match_runtime_prompt():
    header = prompts._EVALUATION_SKILL_PATH.read_text(encoding="utf-8").partition("\n---\n")[0]
    for key, expected in {
        "prompt_version": prompts.PROMPT_VERSION,
        "evaluation_version": prompts.EVALUATION_VERSION,
        "system_prompt_sha256": hashlib.sha256(prompts.EVALUATION_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
    }.items():
        assert re.search(rf'  {key}: "([^"]+)"', header).group(1) == expected


@pytest.mark.parametrize("jd,counts", [
    ("要求：Python；SQL；说明本人负责的实现；每周四天", [2, 1, 0, 0, 0]),
    ("要求：Go；项目成果；三年开发经验；业务领域：物流；每周四天", [1, 1, 1, 1, 0]),
    ("每周四天", [0, 0, 0, 0, 0]),
])
def test_output_template_tracks_jd_without_prefilling_scores_or_mutating_input(jd, counts):
    data = {"candidate": {"skills": ["Python"]}, "job": {"raw_jd": jd}}
    before = deepcopy(data)
    payload = json.loads(prompts.build_evaluation_messages(data)[1]["content"])
    template = payload["output_template"]
    assert data == before
    assert [len(d["criteria"]) for d in template["dimensions"]] == counts
    for dim, contract in zip(template["dimensions"], payload["scoring_contract"]):
        assert dim["name"] == contract["name"]
        assert [c["id"] for c in dim["criteria"]] == [r["id"] for r in contract["requirements"] if contract["applicable"]]
        assert dim["confidence"] is None
        assert all(c["score"] is c["status"] is None and c["references"] == [] for c in dim["criteria"])


def test_unfilled_template_is_rejected_and_all_unknown_still_requires_each_clause():
    from app.ai.service import _normalize_evaluation

    data = {"job": {"raw_jd": "要求：Python；SQL；说明本人负责的实现"}}
    template = json.loads(prompts.build_evaluation_messages(data)[1]["content"])["output_template"]
    with pytest.raises(ValueError, match="score 必须是数字"):
        _normalize_evaluation(template, data)
    for dim in template["dimensions"]:
        dim["confidence"] = 0.3
        for criterion in dim["criteria"]:
            criterion.update(score=0, status="unknown", missing_or_conflicting=["简历未提供"])
    result = _normalize_evaluation(template, data)
    assert result["total_score"] == 0
    assert [c["id"] for d in result["dimensions"] for c in d["criteria"]] == [
        "required_skills:1", "required_skills:2", "project_evidence:1",
    ]
    template["dimensions"][0].pop("criteria")
    with pytest.raises(ValueError, match="缺少逐条评分criteria"):
        _normalize_evaluation(template, data)
