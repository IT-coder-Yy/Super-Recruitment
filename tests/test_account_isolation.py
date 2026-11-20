"""正式环境与账号边界：真实 HTTP 路由、虚构资料，禁止外部服务。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.main as main
from app import config
from app.accounts import assign_data, create_account
from app.db import Base, create_db_engine, get_db, init_db
from app.models import Application, ApplicationStatus, AuditLog, Candidate, ImportTask, Interview, Job, Notification, NotificationStatus, Offer, User, utc_now
from app.ownership import AccountAccessDenied, account_scope
from app.seed import seed_database
from app.services import imports, offers


def csrf(client, path="/login"):
    response = client.get(path)
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text) or re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match
    return match[1]


def login(client, username):
    response = client.post("/login", data={"username": username, "password": "synthetic-password-123", "csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return {"X-CSRF-Token": csrf(client, "/")}


@pytest.fixture
def secure_env(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'secure.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "startup", lambda: init_db(engine))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "runtime.env")
    monkeypatch.setattr(imports, "UPLOAD_DIR", tmp_path / "uploads")
    with factory.begin() as session:
        a = create_account(session, "owner-a", "synthetic-password-123", admin=True)
        b = create_account(session, "owner-b", "synthetic-password-123")
        ids = (a.id, b.id)
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[get_db] = database
    with TestClient(main.app) as a, TestClient(main.app) as b:
        ha, hb = login(a, "owner-a"), login(b, "owner-b")
        yield a, b, ha, hb, factory, ids
    main.app.dependency_overrides.clear()
    engine.dispose()


def upload(client, headers, *, name="隔离测试人", phone="13900008881"):
    raw = f"姓名：{name}\n手机号：{phone}\n邮箱：synthetic@example.test\n项目：Python FastAPI 后端\n教育：虚构大学 计算机 本科"
    response = client.post("/api/import/files", headers=headers,
        data={"job_title": "隔离测试岗位", "jd_text": "Python FastAPI", "recruitment_type": "autumn", "owner_user_id": 999},
        files={"files": ("synthetic.txt", raw.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    task_id = response.json()["task"]["id"]
    assert client.post(f"/api/import/tasks/{task_id}/parse", headers=headers).status_code == 200
    confirmed = client.post(f"/api/import/tasks/{task_id}/confirm", headers=headers, data={"start_evaluation": "false"})
    assert confirmed.status_code == 200, confirmed.text
    return {**confirmed.json(), "task_id": task_id}





def test_each_account_has_independent_import_dedup_and_exports(secure_env):
    a, b, ha, hb, factory, (aid, bid) = secure_env
    first = upload(a, ha)
    assert b.get("/api/candidates").json() == []
    assert b.get("/api/import/tasks").json() == []
    second = upload(b, hb)
    assert first["candidate_id"] != second["candidate_id"]
    repeated = upload(a, ha)
    assert repeated["candidate_id"] == first["candidate_id"]
    for client, own, other in [(a, first, second), (b, second, first)]:
        rows = client.get("/api/candidates").json()
        assert {row["candidate_id"] for row in rows} == {own["candidate_id"]}
        assert client.get(f"/api/candidates/{other['candidate_id']}").status_code == 403
        assert client.get(f"/candidates/{other['candidate_id']}").status_code == 403
        workbook = load_workbook(BytesIO(client.get("/api/exports/recruitment.xlsx").content), read_only=True)
        assert workbook.active.max_row - 1 == len(rows)
        workbook.close()
        assert client.get("/api/dashboard/summary").json()["funnel"]["applications"] == len(rows)
        assert client.get("/").status_code == 200
    with factory() as session:
        assert session.get(Candidate, first["candidate_id"]).owner_user_id == aid
        assert session.get(Candidate, second["candidate_id"]).owner_user_id == bid
        assert session.get(ImportTask, first["task_id"]).owner_user_id == aid


@pytest.mark.parametrize("path", [
    "/api/candidates/{candidate_id}/delete", "/api/candidates/{candidate_id}/supplement",
    "/api/candidates/{candidate_id}/profile", "/api/applications/{application_id}/parse",
    "/api/applications/{application_id}/evaluate", "/api/applications/{application_id}/decision",
    "/api/applications/{application_id}/schedule", "/api/applications/{application_id}/offer",
    "/api/applications/{application_id}/onboarding", "/api/import/tasks/{task_id}/parse",
    "/api/import/tasks/{task_id}/supplement", "/api/import/tasks/{task_id}/confirm",
    "/api/import/tasks/{task_id}/reject",
])
def test_cross_account_mutations_are_rejected_before_side_effects(secure_env, path):
    a, b, ha, hb, factory, ids = secure_env
    record = upload(a, ha)
    assert b.post(path.format(**record), headers=hb, data={"confirm": "DELETE", "status": "on_hold"}).status_code == 403
    assert b.delete(f"/api/candidates/{record['candidate_id']}", headers=hb).status_code == 403
    with factory() as session:
        assert session.get(Candidate, record["candidate_id"]) is not None
        assert session.get(Application, record["application_id"]).status == ApplicationStatus.CAPTURED


@pytest.mark.parametrize("id_format", ["{id}", "+{id}", "%2B{id}", "{id}.0"])
def test_settings_and_offer_approval_need_explicit_permissions(secure_env, id_format):
    a, b, ha, hb, factory, (aid, bid) = secure_env
    assert b.get("/api/settings/llm").status_code == 403
    assert 'action="/settings"' not in b.get("/settings").text
    values = {"provider": "mock", "model": "synthetic-permission-check"}
    assert a.post("/api/settings/llm", headers=ha, data=values).status_code == 200
    for path in ("/settings", "/settings/wecom", "/settings/llm/clear", "/api/settings/llm", "/api/settings/llm/clear", "/api/settings/llm/clear-key"):
        assert b.post(path, headers=hb, data=values).status_code == 403
    assert a.get("/api/settings/llm").json()["model"] == values["model"]
    record = upload(b, hb)
    with account_scope(bid), factory.begin() as session:
        session.get(Application, record["application_id"]).status = ApplicationStatus.OFFER_PENDING
        offer = offers.create_offer(session, record["application_id"])
        now = utc_now()
        offers.save_offer_terms(session, offer.id, {"company_name": "虚构公司", "job_title": "虚构岗位", "salary": "虚构 300 元/天", "subsidy": "无", "office_location": "虚构办公区", "report_location": "虚构前台", "report_time": (now + timedelta(days=7)).date().isoformat(), "expires_at": (now + timedelta(days=3)).isoformat()}, version=0)
        oid = offer.id
    path = f"/api/offers/{id_format.format(id=oid)}/approve"
    button = f'action="/api/offers/{oid}/approve"'
    assert button not in b.get(f"/offers?offer_id={oid}").text
    assert b.post(path, headers=hb, data={"version": 1}).status_code == 403
    assert b.post(path.removesuffix("approve") + "return", headers=hb, data={"version": 1, "reason": "无权退回"}).status_code == 403
    with factory() as session:
        assert session.get(Offer, oid).status.value == "pending_approval"
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.entity_type == "offer", AuditLog.entity_id == oid,
            AuditLog.action.in_(["offer.approved", "offer.returned"]))) == 0
    assert a.post(f"/api/offers/{oid}/approve", headers=ha, data={"version": 1}).status_code == 403
    assert a.get(f"/offers?offer_id={oid}").status_code == 403
    with factory.begin() as session:
        session.get(User, bid).can_approve_offers = True
    assert b.get(f"/offers?offer_id={oid}").text.count(button) == 2
    assert b.post(path, headers=hb, data={"version": 1}).json()["status"] == "approved"
    with factory.begin() as session:
        session.get(User, bid).is_active = False
    assert b.get("/").url.path == "/login"
    assert b.get("/api/candidates").status_code == 401


@pytest.mark.parametrize("created_admin, explicit", [(True, False), (False, False), (True, True)])
def test_demotion_keeps_only_explicit_offer_permission(secure_env, monkeypatch, created_admin, explicit):
    from app import accounts
    _, _, _, _, factory, _ = secure_env
    engine = factory.kw["bind"]
    with factory.begin() as session:
        user = create_account(session, "review-role", "synthetic-password-123", admin=created_admin, approve_offers=explicit)
        uid = user.id
    monkeypatch.setattr(accounts, "engine", engine)
    monkeypatch.setattr(accounts, "init_db", lambda: init_db(engine))
    monkeypatch.setattr(accounts, "SessionLocal", factory)
    with TestClient(main.app) as client:
        login(client, "review-role")
        for value in ("yes", "no"):
            monkeypatch.setattr(sys, "argv", ["accounts", "update", "review-role", "--admin", value])
            accounts.main()
            assert client.get("/offers").context["current_user"]["can_approve_offers"] is (value == "yes" or explicit)
    with factory() as session:
        user = session.get(User, uid)
        assert not user.is_admin and user.can_approve_offers is explicit
        created = session.scalar(select(AuditLog).where(AuditLog.action == "account.created", AuditLog.entity_id == uid))
        assert created.after_data["can_approve_offers"] is explicit


def test_admin_cannot_access_foreign_workflow_records_or_files(secure_env):
    from app.models import ExamResource
    a, b, ha, hb, factory, (aid, bid) = secure_env
    with factory.begin() as session:
        session.get(User, bid).is_admin = True
    with account_scope(aid), factory.begin() as session:
        seed_database(session)
        iid = session.scalar(select(Interview.id))
        application = session.scalar(select(Application))
        application.status = ApplicationStatus.OFFER_PENDING
        session.flush()
        oid = offers.create_offer(session, application.id).id
        job_id = session.scalar(select(Job.id))
        resource = ExamResource(job_id=job_id, kind="assessment", title="甲的私有试题", url="https://exam.example.test/private")
        notification = Notification(channel="email", status=NotificationStatus.FAILED, content="虚构通知", target_type="offer", target_id=oid)
        session.add_all([resource, notification])
        session.flush()
        rid, nid = resource.id, notification.id
        assert all(isinstance(value, int) for value in (iid, oid, nid, rid))
    for path in ("/api/candidates", "/api/offers", "/api/onboarding"):
        assert b.get(path).json() == []
    for path in (f"/api/exams/resources/{rid}/file", f"/exams?resource_id={rid}", f"/exams?job_id={job_id}"):
        assert b.get(path).status_code == 403
    for path in (
        f"/api/interviews/{iid}/feedback", f"/api/interviews/{iid}/send-invite",
        f"/api/interviews/{iid}/meeting/retry", f"/api/interviews/{iid}/meeting/change",
        f"/api/interviews/{iid}/meeting/reconcile", f"/api/notifications/{nid}/retry",
        f"/api/notifications/{nid}/verify", f"/api/exams/resources/{rid}/delete",
        f"/api/offers/{oid}/terms", f"/api/offers/{oid}/generate-draft",
        f"/api/offers/{oid}/approve", f"/api/offers/{oid}/return", f"/api/offers/{oid}/send",
    ):
        assert b.post(path, headers=hb).status_code == 403, path
    with account_scope(aid), factory() as session:
        assert session.get(ExamResource, rid) is not None
        assert session.get(Offer, oid).owner_user_id == aid


def test_orphaned_data_are_not_visible(secure_env):
    a, b, ha, hb, factory, (aid, bid) = secure_env
    with factory.begin() as session:
        orphan = Candidate(name="尚未分配的历史资料", normalized_name="legacy", phone="13900008882", normalized_phone="13900008882")
        session.add(orphan)
    assert a.get("/api/candidates").json() == []
    assert b.get(f"/api/candidates/{orphan.id}").status_code == 403
    with factory.begin() as session:
        assign_data(session, session.get(User, aid))
    assert a.get(f"/api/candidates/{orphan.id}").status_code == 200
    assert b.get(f"/api/candidates/{orphan.id}").status_code == 403


def test_parallel_requests_do_not_mix_account_context(secure_env):
    a, b, ha, hb, factory, (aid, bid) = secure_env
    one, two = upload(a, ha, name="仅甲可见"), upload(b, hb, name="仅乙可见")
    def read(client):
        return {row["candidate_id"] for row in client.get("/api/candidates").json()}
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(read, [a, b] * 5))
    assert results == [{one["candidate_id"]}, {two["candidate_id"]}] * 5


def test_foreign_relationships_and_bulk_queries_remain_scoped(secure_env):
    from sqlalchemy import update
    a, b, ha, hb, factory, (aid, bid) = secure_env
    record = upload(a, ha)
    with account_scope(bid), factory() as session:
        assert session.get(Candidate, record["candidate_id"]) is None
        changed = session.execute(update(Candidate).where(Candidate.id == record["candidate_id"]).values(name="不能修改"))
        assert changed.rowcount == 0
        session.add(Interview(application_id=record["application_id"], round=1))
        with pytest.raises(AccountAccessDenied):
            session.flush()
        session.rollback()


def test_public_offer_capability_preserves_owner_without_logging_in(secure_env):
    import secrets
    from app.models import OfferStatus, OnboardingTask
    a, b, ha, hb, factory, (aid, bid) = secure_env
    record = upload(a, ha, name="仅公开链接对应的候选人")
    with account_scope(aid), factory.begin() as session:
        application = session.get(Application, record["application_id"])
        application.status = ApplicationStatus.OFFER_PENDING
        offer = offers.create_offer(session, application.id)
        offer.status, offer.candidate_token = OfferStatus.SENT, secrets.token_urlsafe(32)
        application.status = ApplicationStatus.OFFER_SENT
        token = offer.candidate_token
    with TestClient(main.app) as public:
        page = public.get(f"/offer/{token}")
        assert page.status_code == 200 and "仅公开链接对应的候选人" in page.text
        response = public.post(f"/offer/{token}/respond", data={"accepted": "true"}, follow_redirects=False)
        assert response.status_code == 303, response.text
        assert public.get("/api/candidates").status_code == 401
        with factory() as session:
            tasks = session.scalars(select(OnboardingTask)).all()
            assert tasks and {task.owner_user_id for task in tasks} == {aid}
        assert b.get("/api/onboarding").json() == []
        with factory.begin() as session:
            session.get(User, aid).is_active = False
        assert public.get(f"/offer/{token}").status_code != 200


def test_background_followups_are_owned_and_hidden_from_other_accounts(secure_env):
    from app.models import FollowUpTask
    from app.services.periodic import run_account_followups
    a, b, ha, hb, factory, (aid, bid) = secure_env
    records = [upload(a, ha, name="后台甲"), upload(b, hb, name="后台乙")]
    for owner, record in zip((aid, bid), records):
        with account_scope(owner), factory.begin() as session:
            application = session.get(Application, record["application_id"])
            application.status = ApplicationStatus.ASSESSMENT_PENDING
            application.status_changed_at = utc_now() - timedelta(days=3)
    assert run_account_followups(factory) == 2
    assert run_account_followups(factory) == 0
    with factory() as session:
        assert set(session.scalars(select(FollowUpTask.owner_user_id))) == {aid, bid}
        assert set(session.scalars(select(Notification.owner_user_id))) == {aid, bid}
    one = a.get("/api/follow-ups").json()
    two = b.get("/api/follow-ups").json()
    assert len(one) == len(two) == 1 and one[0]["id"] != two[0]["id"]
    assert b.post(f"/api/follow-ups/{one[0]['id']}/resolve", headers=hb).status_code == 403
