"""真实协议替身 + 隔离 SQLite 验证；不使用真实 Token 或网络。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from threading import Event

import httpx
import pytest
from sqlalchemy import select, text

from app.adapters import tencent_meeting as adapter
from app.models import ApplicationStatus, AuditLog, Interview, InterviewSlot, InterviewStatus, Notification
from app.services import meetings
from app.services.periodic import recover_inflight
from tests.test_demo_contracts import env, application, enter_interview, post


def reply(snapshot, **changes):
    item = {"meeting_id": "123456789012345", "meeting_code": "123456789",
            "join_url": "https://meeting.tencent.com/dm/synthetic-test",
            **{key: snapshot[key] for key in ("subject", "start_time", "end_time")},
            "host_key": "must-not-persist", "password": "must-not-persist", **changes}
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({
        "status_code": 200, "body": json.dumps({"meeting_info_list": [item]})})}]}}


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setenv("MEETING_PROVIDER", "tencent_mcp")
    monkeypatch.setenv("TENCENT_MEETING_TOKEN", "synthetic-meeting-token")
    calls, responses = [], []
    def handler(request):
        assert str(request.url) == adapter.ENDPOINT
        assert request.headers["x-tencent-meeting-token"] == "synthetic-meeting-token"
        assert request.headers["x-skill-version"] == "v1.1.0"
        params = json.loads(request.content)["params"]
        calls.append(params)
        if responses:
            outcome = responses.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return outcome(params)
            return outcome
        if params["name"] == "schedule_meeting":
            return httpx.Response(200, json=reply(params["arguments"]))
        raise AssertionError("查询必须使用明确的替身响应")
    def transport(**kwargs):
        assert kwargs == {"retries": 0}
        return httpx.MockTransport(handler)
    monkeypatch.setattr(adapter.httpx, "HTTPTransport", transport)
    return calls, responses


def invite(env):
    client, headers, factory = env
    with factory() as session:
        record = application(session)
    enter_interview(client, headers, record.id)
    booking = post(client, headers, f"/api/applications/{record.id}/schedule")
    return record, booking


def confirm(env, booking):
    return env[0].post(booking["booking_url"] + "/confirm", data={"slot_id": booking["slots"][0]["id"]})


def test_real_booking_success_single_create_and_no_notifications(env, wire):
    client, headers, factory = env
    calls, _ = wire
    record, booking = invite(env)
    response = confirm(env, booking)
    assert response.status_code == 200, response.text
    assert "打开腾讯会议" in response.text and "Mock 演示安排" not in response.text
    assert len(calls) == 1
    args = calls[0]["arguments"]
    assert args["auto_record_type"] == "none" and args["auto_asr"] is False
    assert "invitees" not in args and "participants" not in args
    assert args["start_time"].endswith("+08:00") and "TF-" in args["subject"]
    assert confirm(env, booking).status_code == 404
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        assert row.status == InterviewStatus.CONFIRMED and row.meeting_status == "real_created"
        assert row.meeting_id == "123456789012345" and row.calendar_status == "local_created"
        assert row.application.status == ApplicationStatus.INTERVIEW_CONFIRMED and not row.booking_token
        assert list(session.scalars(select(Notification))) == []
        saved = json.dumps([row.meeting_request, [a.details for a in session.scalars(select(AuditLog))]])
        assert "synthetic-meeting-token" not in saved and "must-not-persist" not in saved
    assert "真实会议已创建" in client.get("/interviews").text
    assert client.post(f"/api/candidates/{record.candidate_id}/delete", headers=headers, data={"confirm": "DELETE"}).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("secret must not leak"), httpx.WriteError("secret must not leak"),
                                   httpx.Response(503, text="secret must not leak"), httpx.Response(200, json={"bad": "format"})])
def test_uncertain_blocks_resends_replan_and_recovers_by_query(env, wire, failure):
    client, headers, factory = env
    calls, responses = wire
    record, booking = invite(env)
    responses.append(failure)
    result = confirm(env, booking)
    assert result.status_code == 503 and "secret must not leak" not in result.text
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        snapshot = dict(row.meeting_request)
        assert row.meeting_status == "real_unknown" and row.confirmed_start_at is None
        assert row.application.status == ApplicationStatus.SCHEDULING
        assert len(meetings.occupied_intervals(session)) == 1
    assert confirm(env, booking).status_code == 202
    assert client.get(booking["booking_url"]).status_code == 202
    page = client.get("/interviews").text
    assert "查询并关联已有会议" in page and snapshot["subject"] in page
    assert "synthetic-meeting-token" not in page
    assert client.post(f"/api/applications/{record.id}/schedule", headers=headers).status_code == 409
    assert client.post(f"/api/interviews/{row.id}/send-invite", headers=headers).status_code == 409
    assert client.post(f"/api/interviews/{row.id}/meeting/retry", headers=headers).status_code == 409
    # 改成Mock也不能绕过已有真实尝试。
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MEETING_PROVIDER", "mock")
        assert confirm(env, booking).status_code == 202
    assert len(calls) == 1
    responses.append(httpx.Response(200, json=reply(snapshot)))
    recovered = client.post(f"/api/interviews/{row.id}/meeting/reconcile", headers=headers, data={"meeting_code": "123 456 789"})
    assert recovered.status_code == 200, recovered.text
    assert [p["name"] for p in calls] == ["schedule_meeting", "get_meeting_by_code"]
    assert client.post(f"/api/interviews/{row.id}/meeting/reconcile", headers=headers).status_code == 200
    assert len(calls) == 2
    with factory() as session:
        assert session.get(Interview, row.id).status == InterviewStatus.CONFIRMED


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout("secret"), httpx.Response(401, text="secret"),
                                   httpx.Response(200, json={"result": {"content": [{"text": '{"status_code":403}'}]}})])
def test_definite_failure_requires_hr_retry_and_preserves_attempt_audit(env, wire, failure):
    client, headers, factory = env
    calls, responses = wire
    _, booking = invite(env)
    responses.append(failure)
    assert confirm(env, booking).status_code == 503
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        original = row.meeting_request["attempt_id"]
        assert row.meeting_status == "real_failed" and not meetings.occupied_intervals(session)
    assert confirm(env, booking).status_code == 202 and len(calls) == 1
    assert "重试建会一次" in client.get("/interviews").text
    response = client.post(f"/api/interviews/{row.id}/meeting/retry", headers=headers)
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert client.post(f"/api/interviews/{row.id}/meeting/retry", headers=headers).status_code == 409
    with factory() as session:
        current = session.get(Interview, row.id)
        assert current.meeting_request["attempt_id"] != original and current.status == InterviewStatus.CONFIRMED
        audits = list(session.scalars(select(AuditLog).where(AuditLog.action == "interview.meeting_requested")))
        assert len(audits) == 2 and audits[-1].details["previous_attempt"] == original


def test_concurrent_pending_attempt_does_not_hold_sqlite_lock_or_double_book(env, wire):
    client, headers, factory = env
    calls, responses = wire
    first, booking = invite(env)
    second, other = invite(env)
    started, release = Event(), Event()
    def delayed(params):
        started.set()
        assert release.wait(8)
        return httpx.Response(200, json=reply(params["arguments"]))
    responses.append(delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(confirm, env, booking)
        try:
            assert started.wait(5)
            with factory() as session:
                row = session.get(Interview, booking["interview_id"])
                assert row.meeting_status == "real_creating" and row.confirmed_start_at is None
                session.execute(text("UPDATE interviews SET id=id WHERE id=:id"), {"id": row.id})
                session.commit()  # 外部请求等待期间可以获得写锁。
            assert confirm(env, booking).status_code == 202
            # 第二份邀请是在占用前生成，提交时仍须重新检查预留时段。
            assert confirm(env, other).status_code == 409
            assert client.post(f"/api/applications/{first.id}/schedule", headers=headers).status_code == 409
            assert len(calls) == 1
        finally:
            release.set()
        assert future.result().status_code == 200


def test_restart_and_lost_response_preserve_reservation_and_query_only(env, wire, monkeypatch):
    client, headers, factory = env
    calls, responses = wire
    _, booking = invite(env)
    original = meetings._accept_result
    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic process interruption after remote create")
    monkeypatch.setattr(meetings, "_accept_result", interrupted)
    with pytest.raises(RuntimeError):
        confirm(env, booking)
    monkeypatch.setattr(meetings, "_accept_result", original)
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        snapshot = dict(row.meeting_request)
        assert row.meeting_status == "real_creating"
        recover_inflight(session)
        session.commit()
    with factory() as session:
        assert session.get(Interview, row.id).meeting_status == "real_unknown"
        assert len(meetings.occupied_intervals(session)) == 1
    assert confirm(env, booking).status_code == 202 and len(calls) == 1
    responses.append(httpx.Response(200, json=reply(snapshot)))
    assert client.post(f"/api/interviews/{row.id}/meeting/reconcile", headers=headers, data={"meeting_code": "123456789"}).status_code == 200
    assert len(calls) == 2


def test_recovery_rejects_wrong_meeting_and_keeps_original_account(env, wire, monkeypatch):
    client, headers, factory = env
    calls, responses = wire
    _, booking = invite(env)
    responses.append(httpx.ReadTimeout("unknown"))
    confirm(env, booking)
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        snapshot = dict(row.meeting_request)
    endpoint = f"/api/interviews/{row.id}/meeting/reconcile"
    for changes in [{"subject": "other meeting"}, {"join_url": "https://evil.example.test/"},
                    {"start_time": "2000-01-01T10:00:00+08:00"}]:
        responses.append(httpx.Response(200, json=reply(snapshot, **changes)))
        assert client.post(endpoint, headers=headers, data={"meeting_code": "123456789"}).status_code == 503
        with factory() as session:
            current = session.get(Interview, row.id)
            assert current.meeting_status == "real_unknown" and current.meeting_id is None
            assert current.meeting_url is None
    count = len(calls)
    monkeypatch.setenv("TENCENT_MEETING_TOKEN", "other-synthetic-account")
    assert client.post(endpoint, headers=headers, data={"meeting_code": "123456789"}).status_code == 409
    assert len(calls) == count
    # 未登录及没有CSRF均不能触发查询/重试。
    assert client.post(endpoint, data={"meeting_code": "123456789"}).status_code == 403
    client.cookies.clear()
    assert client.post(endpoint, headers=headers, data={"meeting_code": "123456789"}).status_code in {401, 403}
    assert len(calls) == count


def test_partial_create_keeps_id_and_query_does_not_create_again(env, wire):
    client, headers, factory = env
    calls, responses = wire
    _, booking = invite(env)
    responses.append(lambda params: httpx.Response(200, json=reply(params["arguments"], join_url="")))
    assert confirm(env, booking).status_code == 503
    with factory() as session:
        row = session.get(Interview, booking["interview_id"])
        snapshot = dict(row.meeting_request)
        assert row.meeting_id == "123456789012345" and row.meeting_status == "real_unknown"
    responses.append(httpx.Response(200, json=reply(snapshot)))
    assert client.post(f"/api/interviews/{row.id}/meeting/reconcile", headers=headers).status_code == 200
    assert [p["name"] for p in calls] == ["schedule_meeting", "get_meeting"]


def test_mock_is_default_even_with_token_and_invalid_provider_fails_closed(env, wire, monkeypatch):
    calls, _ = wire
    monkeypatch.delenv("MEETING_PROVIDER")
    _, booking = invite(env)
    assert confirm(env, booking).status_code == 200 and not calls
    monkeypatch.setenv("MEETING_PROVIDER", "typo")
    _, another = invite(env)
    assert confirm(env, another).status_code == 503 and not calls


def test_migration_backs_up_old_interview_data_and_is_repeatable(tmp_path):
    from app.db import create_db_engine
    from app.migrations import prepare_upgrade
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE interviews (id INTEGER PRIMARY KEY, meeting_status VARCHAR(50), meeting_url TEXT)")
        connection.exec_driver_sql("INSERT INTO interviews VALUES (1, 'mock_created', 'https://example.test/legacy')")
    backup = prepare_upgrade(engine)
    assert backup and backup.is_file()
    prepare_upgrade(engine)
    with engine.connect() as connection:
        record = connection.exec_driver_sql("SELECT * FROM interviews").mappings().one()
        assert record["meeting_status"] == "mock_created" and record["meeting_id"] is None and record["meeting_request"] is None
    engine.dispose()


def test_commit_failure_before_send_cannot_create_remote_meeting(env, wire, monkeypatch):
    from sqlalchemy.orm import Session
    calls, _ = wire
    _, booking = invite(env)
    original = Session.commit
    def fail_claim(session):
        if any(isinstance(row, Interview) and row.meeting_status == "real_creating" for row in session.identity_map.values()):
            raise RuntimeError("synthetic commit failed")
        return original(session)
    monkeypatch.setattr(Session, "commit", fail_claim)
    with pytest.raises(RuntimeError):
        confirm(env, booking)
    assert calls == []
    with env[2]() as session:
        assert session.get(Interview, booking["interview_id"]).meeting_request is None


def test_query_failure_keeps_unknown_and_duplicate_remote_id_is_rejected(env, wire):
    client, headers, factory = env
    calls, responses = wire
    _, first = invite(env)
    assert confirm(env, first).status_code == 200
    _, second = invite(env)
    # 替身给另一份预约返回相同的服务端ID；必须拒绝关联。
    assert confirm(env, second).status_code == 503
    with factory() as session:
        row = session.get(Interview, second["interview_id"])
        assert row.meeting_status == "real_unknown" and row.meeting_id is None
        assert row.meeting_url is None
    endpoint = f"/api/interviews/{row.id}/meeting/reconcile"
    assert client.post(endpoint, headers=headers).status_code == 409
    assert len(calls) == 2
    responses.append(httpx.Response(404))
    assert client.post(endpoint, headers=headers, data={"meeting_code": "123456789"}).status_code == 503
    assert confirm(env, second).status_code == 202
    with factory() as session:
        assert session.get(Interview, row.id).meeting_status == "real_unknown"
        assert len(meetings.occupied_intervals(session)) == 2
    assert len(calls) == 3


def test_retry_checks_current_occupancy_and_expiry(env, wire):
    client, headers, factory = env
    calls, responses = wire
    _, first = invite(env)
    _, second = invite(env)
    responses.append(httpx.Response(401))
    assert confirm(env, first).status_code == 503
    assert confirm(env, second).status_code == 200
    endpoint = f"/api/interviews/{first['interview_id']}/meeting/retry"
    assert client.post(endpoint, headers=headers).status_code == 409
    with factory() as session:
        slot = session.get(InterviewSlot, first["slots"][0]["id"])
        slot.start_at = datetime(2000, 1, 1)
        session.commit()
    assert client.post(endpoint, headers=headers).status_code == 409
    assert len(calls) == 2


@pytest.mark.parametrize("key,value", [("TENCENT_MEETING_TOKEN", ""), ("TENCENT_MEETING_TOKEN", "bad token"),
                                      ("TENCENT_MEETING_TIMEOUT_SECONDS", "nan")])
def test_invalid_configuration_never_creates_or_falls_back(env, wire, monkeypatch, key, value):
    calls, _ = wire
    _, booking = invite(env)
    monkeypatch.setenv(key, value)
    assert confirm(env, booking).status_code == 503 and not calls
    with env[2]() as session:
        row = session.get(Interview, booking["interview_id"])
        assert row.meeting_url is None and row.meeting_request is None and row.confirmed_start_at is None
