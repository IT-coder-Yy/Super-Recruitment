"""旧 main 人工补充资料兼容：局部编辑、评分与清空语义的反例回归。"""
from copy import deepcopy
import json

import pytest

from app.main import _candidate_profile
from app.models import Application, Candidate, Evaluation
from app.services.candidates import candidate_facts, revise_candidate
from app.services.evaluations import evaluate_application_record, matching_input
from app.services.imports import build_human_summary
from app.services.workflow import create_application, get_or_create_job_version
from tests.test_demo_contracts import application, env, post


def legacy_record(session, *, fields=None, missing_facts=False):
    record = application(session)
    previous = evaluate_application_record(session, record)["evaluation_id"]
    structured = deepcopy(record.candidate.structured_data)
    structured.pop("revision")
    if missing_facts:
        structured["candidate"] = {}
    structured["human_summary"] = {
        "skills": ["Rust"],
        "projects": [{"title": "人工补充经历 1", "description": "Rust 订单服务"}],
        "note": "HR 已核实原简历识别不正确",
        "chat_summary": "旧岗位专属聊天",
    }
    structured["manual_supplement"] = {
        "updated_at": "2026-10-20T10:00:00+00:00",
        "fields": fields if fields is not None else ["skills", "experience", "note", "chat_summary"],
    }
    record.candidate.structured_data = structured
    session.commit()
    return record, previous


@pytest.mark.parametrize("fields", [["skills", "experience", "note"], ["email"]])
def test_legacy_candidate_regressions_email_preserves_manual_facts(env, fields):
    client, headers, factory = env
    with factory() as session:
        record, previous_id = legacy_record(session, fields=fields)
        saved = deepcopy(record.candidate.structured_data)
        before = matching_input(record)["candidate"]
        assert before["skills"] == ["Rust"]
        assert before["project_experience"][0]["description"] == "Rust 订单服务"
        assert record.candidate.structured_data == saved  # 匹配输入保持只读。
        record_id, candidate_id = record.id, record.candidate_id
    post(client, headers, f"/api/candidates/{candidate_id}/supplement", email="updated@example.test", version=0)
    with factory() as session:
        record = session.get(Application, record_id)
        profile = _candidate_profile(record.candidate)
        assert profile["skills"] == ["Rust"]
        assert profile["projects"][0]["description"] == "Rust 订单服务"
        assert profile["note"] == "HR 已核实原简历识别不正确"
        after = matching_input(record)["candidate"]
        assert {**before, "email": "updated@example.test"} == after
        assert session.get(Evaluation, previous_id).is_stale


@pytest.mark.parametrize("value", ["Go", ""])
def test_legacy_candidate_regressions_explicit_change_and_clear_win(env, value):
    _, _, factory = env
    with factory() as session:
        record, _ = legacy_record(session)
        legacy_summary = deepcopy(record.candidate.structured_data["human_summary"])
        changes = {"skills": value, "experience": value, "note": value}
        revise_candidate(session, record.candidate, changes, version=0)
        session.commit()
        facts = deepcopy(record.candidate.structured_data["candidate"])
        assert facts["skills"] == ([value] if value else [])
        assert [item["description"] for item in facts["project_experience"]] == ([value] if value else [])
        assert facts["note"] == value
        # 即使还留有旧摘要/标记，也不能覆盖后来明确填写或清空的字段。
        stale = deepcopy(record.candidate.structured_data)
        stale["human_summary"] = legacy_summary
        record.candidate.structured_data = stale
        session.commit()
        assert candidate_facts(record.candidate)[1] == set()
        revise_candidate(session, record.candidate, {"email": "second@example.test"}, version=1)
        session.commit()
        assert record.candidate.structured_data["candidate"] == {**facts, "email": "second@example.test"}
        assert _candidate_profile(record.candidate)["note"] == value


def test_legacy_candidate_regressions_missing_facts_keep_parse_fallback(env):
    _, _, factory = env
    with factory() as session:
        record, _ = legacy_record(session, missing_facts=True)
        saved = deepcopy(record.candidate.structured_data)
        before = matching_input(record)
        assert before["candidate"]["skills"] == ["Rust"]
        assert before["candidate"]["project_experience"][0]["description"] == "Rust 订单服务"
        assert "education" in before["candidate"]
        assert before["input_version"]["candidate_revision"] == 0
        assert record.candidate.structured_data == saved
        revise_candidate(session, record.candidate, {"email": "fallback@example.test"}, version=0)
        session.commit()
        assert matching_input(record)["candidate"] == {**before["candidate"], "email": "fallback@example.test"}


def test_legacy_candidate_regressions_generated_summary_is_not_manual(env):
    _, _, factory = env
    with factory() as session:
        record = application(session)
        structured = deepcopy(record.candidate.structured_data)
        structured["human_summary"] = build_human_summary(structured)
        structured["manual_supplement"] = {"fields": ["email"]}
        record.candidate.structured_data = structured
        session.commit()
        assert candidate_facts(record.candidate)[1] == set()
        original = deepcopy(structured["candidate"])
        # 无旧人工标识时，摘要中的不同文字不能覆盖候选人事实。
        structured = deepcopy(structured)
        structured.pop("manual_supplement")
        structured["human_summary"]["skills"] = ["仅摘要出现的词"]
        record.candidate.structured_data = structured
        assert matching_input(record)["candidate"] == original


@pytest.mark.parametrize("missing_facts", [False, True])
def test_legacy_candidate_regressions_evaluation_migrates_once_and_stales_history(env, missing_facts):
    _, _, factory = env
    with factory() as session:
        record, previous_id = legacy_record(session, missing_facts=missing_facts)
        result = evaluate_application_record(session, record)
        corrected = session.get(Evaluation, result["evaluation_id"])
        assert corrected.input_snapshot["candidate"]["skills"] == ["Rust"]
        assert not corrected.is_stale
        assert session.get(Evaluation, previous_id).is_stale
        saved = deepcopy(record.candidate.structured_data)
        assert saved["revision"] == 1
        assert saved["candidate"]["note"] == "HR 已核实原简历识别不正确"
        assert candidate_facts(record.candidate)[1] == set()
        evaluate_application_record(session, record)
        assert record.candidate.structured_data == saved
        assert not corrected.is_stale


def test_legacy_candidate_regressions_does_not_share_old_global_chat(env):
    _, _, factory = env
    with factory() as session:
        record, _ = legacy_record(session)
        record.extra_data = {"manual_chat": "本次投递的已确认聊天"}
        job, jd, _ = get_or_create_job_version(session, {
            "platform": "legacy-test", "source_job_id": "other", "title": "另一岗位", "jd_raw": "Rust",
        })
        other, _ = create_application(session, {
            "candidate_id": record.candidate_id, "job_id": job.id, "job_version_id": jd.id,
        })
        session.commit()
        first = matching_input(record)
        second = matching_input(other)
        assert first["chat"]["summary"] == "本次投递的已确认聊天"
        assert second["chat"] == {"summary": "", "messages": []}
        assert "旧岗位专属聊天" not in json.dumps([first, second], ensure_ascii=False)
        assert "本次投递的已确认聊天" not in json.dumps(second, ensure_ascii=False)
