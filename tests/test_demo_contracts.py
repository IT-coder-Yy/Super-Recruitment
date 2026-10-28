"""隔离磁盘数据库上的新闭环、故障及恢复契约；仅使用虚构资料。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import re
import sqlite3
from threading import Event, Barrier
import uuid

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import app.main as main
from app import config
from app.ai.client import LLMClient
from app.ai.service import AIService
from app.db import create_db_engine, get_db, init_db
from app.models import *
from app.services import imports, offers
from app.services.candidates import ingest_candidate, revise_candidate
from app.services.evaluations import evaluate_application_record, matching_input
from app.services.exams import decide_exam, delete_or_disable, send_exam
from app.services.onboarding import update_onboarding
from app.services.periodic import recover_inflight, run_followups
from app.services.workflow import create_application, get_or_create_job_version


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "startup", lambda: init_db(engine))
    monkeypatch.setattr(imports, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "")
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "http://testserver")
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[get_db] = database
    with TestClient(main.app) as client:
        html = client.get("/register").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
        client.post("/register", data={"username": "demo-hr", "password": "password123", "csrf_token": token})
        html = client.get("/login").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
        response = client.post("/login", data={"username": "demo-hr", "password": "password123", "csrf_token": token})
        assert response.status_code == 200
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
        yield client, {"X-CSRF-Token": csrf}, factory
    main.app.dependency_overrides.clear()
    engine.dispose()


def application(session, status=ApplicationStatus.EVALUATION_REVIEW):
    identity = uuid.uuid4().hex[:8]
    raw = f"姓名：虚构{identity}\n手机号：13800130000\n邮箱：{identity}@example.test\n项目：Python FastAPI 后端 API"
    structured = imports.build_rule_parse(raw)
    structured.update(revision=1, confirmed=True)
    candidate, source, _ = ingest_candidate(session, candidate_data={**structured["candidate"], "structured_data": structured},
        source_data={"platform": "test", "source_application_id": identity, "raw_resume": raw, "raw_chat": []})
    job, jd, _ = get_or_create_job_version(session, {"platform": "test", "source_job_id": identity, "title": "虚构后端岗位", "jd_raw": "要求 Python FastAPI 和 API 项目"})
    record, _ = create_application(session, {"candidate_id": candidate.id, "source_id": source.id, "job_id": job.id, "job_version_id": jd.id})
    record.status = status
    session.commit()
    return record


def resource(session, kind="assessment"):
    record = ExamResource(job_id=session.scalar(select(Job.id).order_by(Job.id.desc())), kind=kind, title="虚构试卷", url="https://exam.example.test/paper")
    session.add(record)
    session.commit()
    return record


def ready_offer(session):
    record = application(session, ApplicationStatus.OFFER_PENDING)
    offer = offers.create_offer(session, record.id)
    now = utc_now()
    values = {"company_name": "虚构公司", "job_title": "后端开发", "salary": "虚构：300 元/天", "subsidy": "无",
              "office_location": "虚构办公区", "report_location": "虚构前台", "report_time": (now + timedelta(days=7)).date().isoformat(),
              "expires_at": (now + timedelta(days=3)).isoformat()}
    offers.save_offer_terms(session, offer.id, values, version=0)
    offers.approve_offer(session, offer.id, version=1)
    session.commit()
    return offer, values


def test_batch_import_correction_and_table_docx(env):
    client, headers, factory = env
    document = Document()
    for label, value in [("姓名", "虚构表格人"), ("手机号", "13800130001"), ("邮箱", "table@example.test"), ("项目", "Python 表格项目")]:
        cells = document.add_table(rows=1, cols=2).rows[0].cells
        cells[0].text, cells[1].text = label + "：", value
    docx = BytesIO(); document.save(docx)
    response = client.post("/api/import/files", headers=headers, files=[
        ("files", ("missing.txt", "姓名：虚构缺字段\n手机号：13800130002\n项目：Python".encode(), "text/plain")),
        ("files", ("table.docx", docx.getvalue(), "application/octet-stream")),
        ("files", ("empty.txt", b"", "text/plain")),
    ])
    assert len(response.json()["tasks"]) == 2 and len(response.json()["errors"]) == 1
    first, second = [t["id"] for t in response.json()["tasks"]]
    for task_id in [first, second]:
        parsed = client.post(f"/api/import/tasks/{task_id}/parse", headers=headers)
        assert parsed.json()["ok"], parsed.text
    table = client.get(f"/api/import/tasks/{second}").json()
    assert table["preview"]["candidate"]["email"] == "table@example.test"
    assert client.post(f"/api/import/tasks/{first}/confirm", headers=headers).status_code == 422
    preview = client.get(f"/api/import/tasks/{first}").json()["preview"]
    corrected = client.post(f"/api/import/tasks/{first}/supplement", headers=headers,
                            data={"version": preview["revision"], "email": "corrected@example.test", "skills": "Rust"})
    assert corrected.status_code == 200
    assert client.post(f"/api/import/tasks/{first}/supplement", headers=headers, data={"version": preview["revision"], "email": "old@example.test"}).status_code == 409
    reparsed = client.post(f"/api/import/tasks/{first}/parse", headers=headers).json()["task"]["preview"]
    assert reparsed["candidate"]["skills"] == ["Rust"]
    confirmed = client.post(f"/api/import/tasks/{first}/confirm", headers=headers, data={"version": reparsed["revision"]})
    assert confirmed.status_code == 200, confirmed.text
    assert client.post(f"/api/import/tasks/{first}/confirm", headers=headers).json()["application_id"] == confirmed.json()["application_id"]
    assert client.get("/exams").status_code == 200


@pytest.mark.parametrize("filename,content", [("blank.pdf", None), ("broken.pdf", b"not a pdf"), ("blank.txt", b"   \n")])
def test_empty_or_broken_file_never_becomes_resume(env, filename, content):
    client, headers, _ = env
    if content is None:
        pdf = PdfWriter(); pdf.add_blank_page(width=200, height=200)
        stream = BytesIO(); pdf.write(stream); content = stream.getvalue()
    result = client.post("/api/import/files", headers=headers, files={"files": (filename, content)})
    task_id = result.json()["task"]["id"]
    result = client.post(f"/api/import/tasks/{task_id}/parse", headers=headers)
    assert result.json()["task"]["status"] == "failed"
    assert client.post(f"/api/import/tasks/{task_id}/confirm", headers=headers).status_code == 422


def test_same_person_same_job_concurrent_sources_do_not_reset_progress(env):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.OFFER_PENDING)
        values = {"candidate_id": app.candidate_id, "job_id": app.job_id, "job_version_id": app.job_version_id}
    def ingest(index):
        with factory() as session:
            # 每次先申请数据库写锁再创建来源，以模拟导入确认事务。
            from app.services.workflow import lock_record
            lock_record(session, Job, values["job_id"])
            source = CandidateSource(candidate_id=values["candidate_id"], platform="concurrent", source_application_id=str(index), raw_resume="新来源")
            session.add(source); session.flush()
            record, created = create_application(session, {**values, "source_id": source.id})
            session.commit()
            return record.id, created
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(ingest, [1, 2]))
    assert results == [(app.id, False), (app.id, False)]
    with factory() as session:
        app = session.get(Application, app.id)
        assert app.status == ApplicationStatus.OFFER_PENDING and len(app.source_links) == 3
        job, jd, _ = get_or_create_job_version(session, {"platform": "test", "source_job_id": "different", "title": "同名岗位", "jd_raw": "Rust"})
        other, created = create_application(session, {**values, "job_id": job.id, "job_version_id": jd.id})
        assert created and other.id != app.id


@pytest.mark.parametrize("status", [ApplicationStatus.INTERVIEW_PENDING, ApplicationStatus.NEXT_ROUND, ApplicationStatus.OFFER_PENDING])
def test_manual_facts_evaluation_failure_retry_and_job_chat_isolation(env, monkeypatch, status):
    client, headers, factory = env
    with factory() as session:
        app = application(session, status)
        first = evaluate_application_record(session, app)
        original = session.get(Evaluation, first["evaluation_id"])
        revise_candidate(session, app.candidate, {"skills": "Rust", "chat_summary": "仅本岗位沟通"}, version=1, application_id=app.id)
        session.commit()
        assert original.is_stale
        before = app.candidate.structured_data
        job, jd, _ = get_or_create_job_version(session, {"platform": "test", "source_job_id": "other-chat", "title": "岗位二", "jd_raw": "Rust"})
        other, _ = create_application(session, {"candidate_id": app.candidate_id, "job_id": job.id, "job_version_id": jd.id})
        session.commit()
        assert "仅本岗位沟通" not in json.dumps(matching_input(other), ensure_ascii=False)
        monkeypatch.setenv("LLM_PROVIDER", "openai"); monkeypatch.setenv("LLM_API_KEY", "")
        with pytest.raises(ValueError, match="岗位匹配失败"):
            evaluate_application_record(session, app)
        assert app.status == status
        assert session.scalar(select(func.count(Evaluation.id))) == 1
        assert session.scalars(select(EvaluationAttempt.status).order_by(EvaluationAttempt.id)).all() == ["succeeded", "failed"]
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        result = evaluate_application_record(session, app)
        assert session.get(Evaluation, result["evaluation_id"]).input_snapshot["candidate"]["skills"] == ["Rust"]
        assert app.candidate.structured_data == before and app.status == status
    assert client.post(f"/api/applications/{app.id}/parse", headers=headers).status_code == 200
    with factory() as session:
        assert session.get(Candidate, app.candidate_id).structured_data == before


@pytest.mark.parametrize("failure", ["config", "timeout", "malformed"])
def test_online_failure_never_calls_mock(monkeypatch, failure):
    client = LLMClient(provider="openai", base_url="https://example.test/v1", api_key="" if failure == "config" else "test", model="test")
    def fail(messages):
        raise TimeoutError() if failure == "timeout" else ValueError("bad JSON")
    monkeypatch.setattr(client, "_request_json", fail)
    with pytest.raises(ValueError):
        client.generate_json(messages=[], fallback=lambda: pytest.fail("在线失败不能降级 Mock"))


@pytest.mark.parametrize("kind", ["assessment", "written"])
def test_exam_delivery_is_separate_from_decision_and_freezes_resource(env, kind):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING if kind == "assessment" else ApplicationStatus.WRITTEN_PENDING)
        paper = resource(session, kind)
        first = send_exam(session, app.id, paper.id, candidate_revision=1)
        second = send_exam(session, app.id, paper.id, candidate_revision=1)
        assert first.id == second.id and first.status == NotificationStatus.MOCKED
        assert app.status in {ApplicationStatus.ASSESSMENT_PENDING, ApplicationStatus.WRITTEN_PENDING}
        assert delete_or_disable(session, paper.id) == "disabled"
        session.commit()
        with pytest.raises(ValueError, match="停用"):
            send_exam(session, app.id, paper.id, candidate_revision=1)
        session.rollback()
        decided = decide_exam(session, app.id, kind=kind, result="passed", note="人工核实通过", version=0)
        session.commit()
        again = decide_exam(session, app.id, kind=kind, result="passed", note="人工核实通过", version=0)
        assert again.status == decided.status
        with pytest.raises(ValueError):
            decide_exam(session, app.id, kind=kind, result="failed", note="旧页面冲突", version=0)


def test_resource_download_auth_type_and_unused_delete(env):
    client, headers, factory = env
    with factory() as session:
        job_id = application(session).job_id
    created = client.post("/api/exams/resources", headers=headers, data={"job_id": job_id, "kind": "assessment", "title": "虚构", "url": "https://exam.example.test/paper"},
                          files={"file": ("internal.pdf", b"%PDF-1.4\ninternal-only", "application/pdf")})
    resource_id = created.json()["resource_id"]
    with TestClient(main.app) as anonymous:
        assert anonymous.get(f"/api/exams/resources/{resource_id}/file").status_code == 401
    assert client.get(f"/api/exams/resources/{resource_id}/file").content.startswith(b"%PDF")
    assert client.post(f"/api/exams/resources/{resource_id}/delete", headers=headers).json()["ok"]
    assert client.get(f"/api/exams/resources/{resource_id}/file").status_code == 404
    for url in ["javascript:alert(1)", "http://", "https://u:p@example.test", "https://exa mple.test"]:
        assert client.post("/api/exams/resources", headers=headers, data={"job_id": job_id, "kind": "assessment", "title": "虚构", "url": url}).status_code == 422


def test_offer_version_reapproval_smtp_failure_retry_and_freeze(env, monkeypatch):
    _, _, factory = env
    with factory() as session:
        offer, values = ready_offer(session)
        offers.return_offer(session, offer.id, version=1, reason="修订薪资")
        values["salary"] = "虚构：350 元/天"
        offers.save_offer_terms(session, offer.id, values, version=1)
        session.commit()
        with pytest.raises(ValueError):
            offers.approve_offer(session, offer.id, version=1)
        offers.approve_offer(session, offer.id, version=2)
        session.commit()
        monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
        monkeypatch.setattr(offers.smtplib, "SMTP", lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
        offers.send_offer(session, offer.id); session.commit()
        assert offer.status == OfferStatus.APPROVED and offer.application.status == ApplicationStatus.OFFER_PENDING
        assert session.scalar(select(Notification).where(Notification.channel == "email")).status == NotificationStatus.FAILED
        monkeypatch.setenv("SMTP_HOST", "")
        offers.send_offer(session, offer.id); session.commit()
        assert offer.status == OfferStatus.SENT and offer.application.status == ApplicationStatus.OFFER_SENT
        count = session.scalar(select(func.count(Notification.id)))
        offers.send_offer(session, offer.id); session.commit()
        assert session.scalar(select(func.count(Notification.id))) == count
        assert "http://testserver/offer/" in session.scalars(select(Notification).order_by(Notification.id.desc())).first().content
        with pytest.raises(ValueError):
            offers.save_offer_terms(session, offer.id, values, version=2)


@pytest.mark.parametrize("ssl", [True, False])
def test_smtp_transport_and_uncertain_delivery_recovery(env, monkeypatch, ssl):
    client, headers, factory = env
    calls = []
    class SMTP:
        def __init__(self, *a, **kw): calls.append("ssl" if ssl else "starttls")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): calls.append("tls")
        def login(self, *a): calls.append("login")
        def send_message(self, message):
            calls.append("send")
            raise TimeoutError("unknown after DATA")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_SSL", str(ssl).lower())
    monkeypatch.setenv("SMTP_FROM", "hr@example.test")
    monkeypatch.setattr(offers.smtplib, "SMTP_SSL" if ssl else "SMTP", SMTP)
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING); paper = resource(session)
        sent = send_exam(session, app.id, paper.id, candidate_revision=1)
        assert sent.status == NotificationStatus.UNKNOWN
        assert send_exam(session, app.id, paper.id, candidate_revision=1).id == sent.id
        assert calls.count("send") == 1 and ("tls" in calls) != ssl
    result = client.post(f"/api/notifications/{sent.id}/verify", headers=headers, data={"delivered": "true", "note": "联系收件人核实已收到"})
    assert result.status_code == 200
    with factory() as session:
        decide_exam(session, app.id, kind="assessment", result="passed", note="HR 核实结果", version=0)
        session.commit()


def test_onboarding_preparation_does_not_join_and_withdraw_blocks_old_actions(env):
    client, headers, factory = env
    with factory() as session:
        offer, _ = ready_offer(session)
        offers.send_offer(session, offer.id); session.commit()
        offers.respond_offer(session, offer.candidate_token, accepted=True); session.commit()
        app = offer.application
        tasks = session.scalars(select(OnboardingTask).where(OnboardingTask.application_id == app.id)).all()
        for task in tasks:
            offers.confirm_onboarding_task(session, task.id)
        session.commit()
        assert app.status == ApplicationStatus.ONBOARDING
        with pytest.raises(ValueError, match="登记"):
            update_onboarding(session, app.id, action="join", version=0, values={"actual_date": utc_now().date().isoformat(), "note": "到岗"})
        session.rollback()
        terms = offers.offer_snapshot(offer)
        update_onboarding(session, app.id, action="defer", version=0, values={"expected_date": (utc_now()+timedelta(days=10)).date().isoformat(), "location": "虚构前台", "note": "候选人申请延期"})
        session.commit()
        assert offers.offer_snapshot(offer) == terms
        update_onboarding(session, app.id, action="needs_supplement", version=1, values={"note": "材料需补齐"}); session.commit()
        with pytest.raises(ValueError):
            update_onboarding(session, app.id, action="join", version=2, values={"actual_date": utc_now().date().isoformat(), "note": "到岗"})
        session.rollback()
        update_onboarding(session, app.id, action="withdraw", version=2, values={"note": "本人确认放弃"}); session.commit()
        with pytest.raises(ValueError):
            offers.confirm_onboarding_task(session, tasks[0].id)


def test_recovery_and_scheduler_without_visiting_pages(env, monkeypatch):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING)
        app.status_changed_at = utc_now()-timedelta(days=3)
        session.add(Notification(channel="email", content="虚构", status=NotificationStatus.SENDING))
        session.add(EvaluationAttempt(application_id=app.id, status="running", input_snapshot={}))
        session.commit()
        recover_inflight(session); session.commit()
        assert session.scalar(select(Notification)).status == NotificationStatus.UNKNOWN
        assert session.scalar(select(EvaluationAttempt)).status == "failed"
    assert run_followups(factory) == 1
    assert run_followups(factory) == 0
    with factory() as session:
        app = session.get(Application, app.id); app.status = ApplicationStatus.REJECTED; session.commit()
    assert run_followups(factory) == 0
    with factory() as session:
        assert session.scalar(select(FollowUpTask)).status == FollowUpStatus.RESOLVED
    triggered = Event()
    monkeypatch.setenv("FOLLOWUP_ENABLED", "true")
    monkeypatch.setattr(main, "run_followups", lambda factory: triggered.set())
    with TestClient(main.app):
        assert triggered.wait(3), "应用生命周期没有实际触发调度入口"


def test_upgrade_backup_is_reentrant_and_restorable(tmp_path):
    from scripts.restore_backup import restore
    database = tmp_path / "old.db"
    # 用真实旧列定义构造包含资料、投递、评估、Offer 的旧库。
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as session:
        offer, _ = ready_offer(session)
        evaluate_application_record(session, offer.application)
        second = application(session, ApplicationStatus.NEXT_ROUND)
        session.add(Interview(application_id=second.id, round=2, status=InterviewStatus.COMPLETED))
        joined = application(session, ApplicationStatus.JOINED)
        joined.candidate.name = joined.candidate.phone = joined.candidate.email = None
        session.add(ExamResource(job_id=offer.application.job_id, kind="assessment", title="历史内部附件", url="https://exam.example.test/old",
                                 file_path=str(tmp_path/"uploads"/"resume.txt"), filename="resume.txt"))
        session.commit()
    engine.dispose()

    with sqlite3.connect(database) as connection:
        for table, column in [("evaluations", "input_snapshot"), ("evaluations", "is_stale"), ("offers", "terms"), ("offer_approvals", "snapshot")]:
            connection.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        connection.execute("DROP TABLE app_schema_versions")
        connection.execute("DELETE FROM application_sources")
    (tmp_path/"uploads").mkdir(); (tmp_path/"uploads"/"resume.txt").write_text("虚构原文", encoding="utf-8")
    (tmp_path/"runtime.env").write_text("LLM_PROVIDER=mock", encoding="utf-8")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    init_db(engine); init_db(engine)
    backups = list((tmp_path/"backups").glob("*.db"))
    assert len(backups) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count(ApplicationSource.source_id))) == 3
        assert session.scalar(select(Offer)).status == OfferStatus.APPROVED
        assert session.get(Application, second.id).status == ApplicationStatus.NEXT_ROUND
        old_joined = session.get(Application, joined.id)
        assert old_joined.status == ApplicationStatus.JOINED and old_joined.candidate.email is None
        assert not (old_joined.extra_data or {}).get("exams")
    restored = restore(backups[0], tmp_path/"restored")
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT file_path FROM exam_resources").fetchone()[0] == str(restored.parent/"uploads"/"resume.txt")
    assert (restored.parent/"uploads"/"resume.txt").read_text(encoding="utf-8") == "虚构原文"
    engine.dispose()

    # 固定迁移前基线，不能随 HEAD 漂移成“新版自动重迁移后读取成功”。
    import os
    import subprocess
    import sys
    from zipfile import ZipFile
    legacy = tmp_path/"legacy-code"
    legacy.mkdir()
    legacy_revision = "9108823d7dd9ae542e76f21620127c94b66ef640"
    archive = subprocess.check_output(["git", "archive", "--format=zip", legacy_revision, "app"])
    with ZipFile(BytesIO(archive)) as bundle:
        assert all((legacy/member).resolve().is_relative_to(legacy.resolve()) for member in bundle.namelist())
        bundle.extractall(legacy)
    (legacy/".env").write_text("LLM_PROVIDER=mock\nSMTP_HOST=\nWECOM_WEBHOOK_URL=\n", encoding="utf-8")
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{restored.as_posix()}", "APP_SECRET_KEY": "synthetic-rollback-only", "LLM_PROVIDER": "mock", "SMTP_HOST": "", "WECOM_WEBHOOK_URL": ""}
    code = '''
import json, re, socket
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError("external call"))
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    token=re.search(r'name="csrf_token" value="([^"]+)"',client.get('/register').text)[1]
    client.post('/register',data={'username':'rollback-hr','password':'password123','csrf_token':token})
    token=re.search(r'name="csrf_token" value="([^"]+)"',client.get('/login').text)[1]
    assert client.post('/login',data={'username':'rollback-hr','password':'password123','csrf_token':token}).status_code==200
    assert client.get('/candidates').status_code==200
    rows=client.get('/api/candidates').json()
    assert len(rows)==3
    assert sorted(r['status'] for r in rows)==['joined','next_round','offer_pending']
    print(json.dumps({'legacy_service':'passed','applications':len(rows)}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=legacy, env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(restored) as connection:
        assert "input_snapshot" not in {row[1] for row in connection.execute("PRAGMA table_info(evaluations)")}
    (tmp_path/"rollback-evidence.json").write_text(json.dumps({"legacy_revision": legacy_revision, "result": json.loads(result.stdout)}), encoding="utf-8")


def post(client, headers, path, **values):
    result = client.post(path, headers=headers, data=values)
    assert result.status_code == 200, result.text
    return result.json()


def enter_interview(client, headers, app_id):
    post(client, headers, f"/api/applications/{app_id}/decision", status="assessment_pending", reason="HR 核实")
    for version, kind in enumerate(["assessment", "written"]):
        post(client, headers, f"/api/applications/{app_id}/exams/decision", kind=kind, result="skipped", note="本岗位 HR 确认免试", version=version)


def test_s02_two_rounds_and_s03_interview_rejection(env):
    client, headers, factory = env
    with factory() as session:
        app = application(session)
    enter_interview(client, headers, app.id)
    rounds = []
    for action in ["next_round", "reject"]:
        schedule = post(client, headers, f"/api/applications/{app.id}/schedule")
        token = schedule["booking_url"].rsplit("/", 1)[-1]
        booked = client.post(f"/schedule/{token}/confirm", data={"slot_id": schedule["slots"][0]["id"]})
        assert booked.status_code == 200
        feedback = post(client, headers, f"/api/interviews/{schedule['interview_id']}/feedback", next_action=action, result="完成", comment="虚构面试证据")
        rounds.append(schedule["interview_id"])
    assert feedback["status"] == "rejected" and rounds[0] != rounds[1]
    replay = post(client, headers, f"/api/interviews/{rounds[0]}/feedback", next_action="next_round", result="完成", comment="虚构面试证据")
    assert replay["status"] == "rejected"
    assert client.post(f"/api/applications/{app.id}/schedule", headers=headers).status_code == 409
    assert client.post(f"/api/interviews/{rounds[0]}/send-invite", headers=headers).status_code == 409
    with factory() as session:
        assert [i.round for i in session.scalars(select(Interview).order_by(Interview.round))] == [1, 2]
        assert session.get(Interview, rounds[0]).feedback["next_action"] == "next_round"
        assert session.scalar(select(func.count(Offer.id))) == 0


def test_s04_expired_invitation_reissues_same_round_and_rejects_old_form(env):
    client, headers, factory = env
    with factory() as session:
        app = application(session)
    enter_interview(client, headers, app.id)
    first = post(client, headers, f"/api/applications/{app.id}/schedule")
    with factory() as session:
        session.get(Interview, first["interview_id"]).booking_token_expires_at = utc_now()-timedelta(seconds=1)
        session.commit()
    assert client.get(first["booking_url"]).status_code == 410
    second = post(client, headers, f"/api/applications/{app.id}/schedule")
    assert first["interview_id"] == second["interview_id"] and first["booking_url"] != second["booking_url"]
    assert client.post(first["booking_url"]+"/confirm", data={"slot_id": first["slots"][0]["id"]}).status_code == 404
    assert client.post(second["booking_url"]+"/confirm", data={"slot_id": second["slots"][0]["id"]}).status_code == 200
    assert client.post(second["booking_url"]+"/confirm", data={"slot_id": second["slots"][0]["id"]}).status_code in {404, 409}
    assert client.post(f"/api/applications/{app.id}/schedule", headers=headers).status_code == 409


@pytest.mark.parametrize("same_invitation", [True, False])
def test_s06_concurrent_booking_confirms_only_one(env, monkeypatch, same_invitation):
    client, headers, factory = env
    with factory() as session:
        a, b = application(session), application(session)
    bookings = []
    for app_id in [a.id, b.id]:
        enter_interview(client, headers, app_id)
        bookings.append(post(client, headers, f"/api/applications/{app_id}/schedule"))
    with factory() as session:
        source = session.get(InterviewSlot, bookings[0]["slots"][0]["id"])
        other = session.get(InterviewSlot, bookings[1]["slots"][0]["id"])
        other.start_at, other.end_at = source.start_at, source.end_at
        session.commit()
    count = []
    original = main.create_mock_tencent_meeting
    def meeting(**kwargs):
        count.append(kwargs["idempotency_key"])
        return original(**kwargs)
    monkeypatch.setattr(main, "create_mock_tencent_meeting", meeting)
    barrier = Barrier(2)
    def confirm(index):
        booking = bookings[0 if same_invitation else index]
        with TestClient(main.app) as anonymous:
            barrier.wait(timeout=5)
            return anonymous.post(booking["booking_url"]+"/confirm", data={"slot_id": booking["slots"][0]["id"]}).status_code
    with ThreadPoolExecutor(2) as executor:
        statuses = sorted(executor.map(confirm, [0, 1]))
    assert statuses[0] == 200 and statuses[1] in {404, 409}
    assert len(count) == 1
    with factory() as session:
        assert session.scalar(select(func.count(Interview.id)).where(Interview.status == InterviewStatus.CONFIRMED)) == 1


@pytest.mark.parametrize("kind,result", [(k, r) for k in ["assessment", "written"] for r in ["skipped", "failed"]])
def test_exam_branches_and_schedule_gate(env, kind, result):
    client, headers, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING if kind == "assessment" else ApplicationStatus.WRITTEN_PENDING)
        paper = resource(session, kind)
        if result == "failed":
            send_exam(session, app.id, paper.id, candidate_revision=1)
    assert client.post(f"/api/applications/{app.id}/schedule", headers=headers).status_code == 409
    response = post(client, headers, f"/api/applications/{app.id}/exams/decision", kind=kind, result=result, note="HR 核实业务结论", version=0)
    assert response["status"] == ("rejected" if result == "failed" else "written_pending" if kind == "assessment" else "interview_pending")


def test_exam_wrong_job_and_email_revision_retry_are_rejected(env, monkeypatch):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING); paper = resource(session)
        other = application(session, ApplicationStatus.ASSESSMENT_PENDING)
        with pytest.raises(ValueError, match="岗位"):
            send_exam(session, other.id, paper.id, candidate_revision=1)
        session.rollback()
        monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
        monkeypatch.setattr(offers.smtplib, "SMTP", lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
        send_exam(session, app.id, paper.id, candidate_revision=1)
        revise_candidate(session, app.candidate, {"email": "corrected@example.test"}, version=1)
        session.commit()
        with pytest.raises(ValueError, match="快照"):
            send_exam(session, app.id, paper.id, candidate_revision=2)
        session.rollback()
        assert session.scalar(select(func.count(Notification.id))) == 1


def test_offer_never_sends_after_application_rejected(env):
    client, headers, factory = env
    with factory() as session:
        offer, _ = ready_offer(session)
        app_id = offer.application_id
    assert client.post(f"/api/applications/{app_id}/decision", headers=headers, data={"status": "rejected"}).status_code == 409
    with factory() as session:
        offer = session.get(Offer, offer.id)
        offer.application.status = ApplicationStatus.REJECTED
        session.commit()
        with pytest.raises(ValueError, match="禁止发信"):
            offers.send_offer(session, offer.id)
        assert session.scalar(select(func.count(Notification.id))) == 0


@pytest.mark.parametrize("outcome", ["accepted", "declined", "expired"])
def test_offer_public_reply_branches(env, outcome):
    client, _, factory = env
    with factory() as session:
        offer, _ = ready_offer(session)
        offers.send_offer(session, offer.id); session.commit()
        if outcome == "expired":
            offer.expires_at = utc_now()-timedelta(seconds=1); session.commit()
    accepted = "false" if outcome == "declined" else "true"
    response = client.post(f"/offer/{offer.candidate_token}/respond", data={"accepted": accepted}, follow_redirects=False)
    assert response.status_code == 303
    with factory() as session:
        stored = session.get(Offer, offer.id)
        assert stored.status.value == outcome
        count = session.scalar(select(func.count(OnboardingTask.id)))
        assert bool(count) == (outcome == "accepted")
    client.post(f"/offer/{offer.candidate_token}/respond", data={"accepted": accepted})
    with factory() as session:
        assert session.scalar(select(func.count(OnboardingTask.id))) == count
    if outcome == "declined":
        assert client.post(f"/offer/{offer.candidate_token}/respond", data={"accepted": "true"}).status_code == 422


def test_ended_interview_reminder_and_legacy_tasks_close(env):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.INTERVIEW_CONFIRMED)
        end = datetime.now().replace(tzinfo=None)-timedelta(days=2)
        session.add(Interview(application_id=app.id, round=1, status=InterviewStatus.CONFIRMED, confirmed_start_at=end-timedelta(hours=1), confirmed_end_at=end))
        offer, _ = ready_offer(session)
        offers.send_offer(session, offer.id); session.commit()
        offers.respond_offer(session, offer.candidate_token, accepted=True); session.commit()
        task = session.scalar(select(OnboardingTask).where(OnboardingTask.offer_id == offer.id))
        session.add(FollowUpTask(target_type="onboarding", target_id=task.id, rule_code="onboarding_pending", window_key="legacy"))
        session.commit()
        update_onboarding(session, offer.application_id, action="withdraw", version=0, values={"note": "虚构放弃"}); session.commit()
        assert session.scalar(select(FollowUpTask).where(FollowUpTask.window_key == "legacy")).status != FollowUpStatus.OPEN
    assert run_followups(factory) == 1
    with factory() as session:
        assert session.scalar(select(FollowUpTask).where(FollowUpTask.rule_code == "interview_feedback_pending"))


def test_historical_funnel_and_created_today_not_updated_today(env):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.REJECTED)
        app.created_at = utc_now()-timedelta(days=2)
        app.updated_at = utc_now()
        session.add(AuditLog(actor="历史HR", action="application.status_changed", entity_type="application", entity_id=app.id,
                            before_data={"status": "feedback_pending"}, after_data={"status": "offer_pending"}))
        session.commit()
        summary = main._dashboard_data(session)
        assert summary["new_today"] == 0
        assert summary["funnel"]["offers"] == 1 and summary["funnel"]["interviews"] == 1


def test_csrf_and_two_users_real_audit_actors(env):
    client, headers, factory = env
    with factory() as session:
        app = application(session)
    assert client.post(f"/api/applications/{app.id}/decision", data={"status": "on_hold"}).status_code == 403
    assert client.post(f"/api/applications/{app.id}/decision", headers={"X-CSRF-Token":"fake"}, data={"status":"on_hold"}).status_code == 403
    post(client, headers, f"/api/applications/{app.id}/decision", status="on_hold", reason="第一位HR")
    with TestClient(main.app) as second:
        token = re.search(r'name="csrf_token" value="([^"]+)"', second.get("/register").text)[1]
        second.post("/register", data={"username":"second-hr", "password":"password123", "csrf_token":token})
        token = re.search(r'name="csrf_token" value="([^"]+)"', second.get("/login").text)[1]
        second.post("/login", data={"username":"second-hr", "password":"password123", "csrf_token":token})
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', second.get("/").text)[1]
        post(second, {"X-CSRF-Token":csrf}, f"/api/applications/{app.id}/decision", status="assessment_pending", reason="第二位HR")
    with factory() as session:
        actors = session.scalars(select(AuditLog.actor).where(AuditLog.action=="application.status_changed").order_by(AuditLog.id)).all()
        assert actors[0].endswith(":demo-hr") and actors[1].endswith(":second-hr")


def test_concurrent_offer_edit_and_send_preserves_approved_snapshot(env):
    _, _, factory = env
    with factory() as session:
        offer, values = ready_offer(session)
    barrier = Barrier(2)
    def operate(action):
        with factory() as session:
            barrier.wait()
            try:
                if action == "edit":
                    offers.save_offer_terms(session, offer.id, {**values, "salary":"虚构：400 元/天"}, version=1)
                else:
                    offers.send_offer(session, offer.id)
                session.commit()
                return "ok"
            except ValueError:
                session.rollback()
                return "conflict"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(operate, ["edit", "send"]))
    assert sorted(results) == ["conflict", "ok"]
    with factory() as session:
        saved = session.get(Offer, offer.id)
        notifications = session.scalars(select(Notification).where(Notification.target_type == "offer")).all()
        if saved.status == OfferStatus.SENT:
            assert saved.terms["salary"] == values["salary"] and len(notifications) == 1
        else:
            assert saved.status == OfferStatus.PENDING_APPROVAL and not notifications


def test_concurrent_resource_delete_and_send_preserves_references(env):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING)
        paper = resource(session)
    barrier = Barrier(2)
    def operate(action):
        with factory() as session:
            barrier.wait()
            try:
                result = delete_or_disable(session, paper.id) if action == "delete" else send_exam(session, app.id, paper.id, candidate_revision=1)
                session.commit()
                return "ok"
            except ValueError:
                session.rollback()
                return "conflict"
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(operate, ["delete", "send"]))
    with factory() as session:
        saved = session.get(ExamResource, paper.id)
        dispatch = session.scalar(select(ExamDispatch))
        if dispatch:
            assert saved is not None and saved.active is False
            assert session.scalar(select(Notification)).status == NotificationStatus.MOCKED
        else:
            assert saved is None


def test_resource_submission_idempotency_conflict_and_file_limits(env, monkeypatch):
    client, headers, factory = env
    with factory() as session:
        app = application(session)
    values = {"job_id":app.job_id,"kind":"written","title":"虚构卷","url":"https://exam.example.test/idempotent","request_key":"fixed-request"}
    first = post(client, headers, "/api/exams/resources", **values)
    assert post(client, headers, "/api/exams/resources", **values)["resource_id"] == first["resource_id"]
    assert client.post("/api/exams/resources",headers=headers,data={**values,"title":"冲突标题"}).status_code == 422
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "20")
    for filename, content in [("empty.pdf",b""),("fake.pdf",b"not PDF"),("big.pdf",b"%PDF-"+b"X"*20),("fake.docx",b"not zip"),("bad.exe",b"executable")]:
        response = client.post("/api/exams/resources",headers=headers,data={**values,"request_key":filename},files={"file":(filename,content)})
        assert response.status_code == 422, response.text
    with factory() as session:
        assert session.scalar(select(func.count(ExamResource.id))) == 1


def test_configuration_persists_across_a_fresh_process(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    for name in config.EDITABLE_KEYS:
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path/"runtime.env")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path/"exports")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path/"uploads")
    config.save_llm_settings(provider="mock", base_url="https://model.example.test/v1", model="synthetic-model", api_key="synthetic-only-key")
    environment={**os.environ,"APP_DATA_DIR":str(tmp_path),"APP_RUNTIME_ENV":str(tmp_path/"runtime.env"),"LLM_MODEL":"stale-environment"}
    code="from app.config import reload_env,get_setting; reload_env(); print(get_setting('LLM_MODEL'))"
    result=subprocess.check_output([sys.executable,"-B","-c",code],env=environment,text=True)
    assert result.strip() == "synthetic-model"
    # 恢复父进程配置，避免合成密钥影响其他测试。
    for name in config.EDITABLE_KEYS:
        monkeypatch.setenv(name, "mock" if name == "LLM_PROVIDER" else "")


def test_exam_metrics_separate_delivery_decision_and_legacy(env):
    _, _, factory = env
    with factory() as session:
        app = application(session, ApplicationStatus.ASSESSMENT_PENDING)
        paper = resource(session)
        send_exam(session, app.id, paper.id, candidate_revision=1)
        summary = main._dashboard_data(session)["exam_counts"]["assessment"]
        assert summary["sent"] == 1 and summary["passed"] == 0
        decide_exam(session, app.id, kind="assessment", result="passed", note="已核实", version=0)
        decide_exam(session, app.id, kind="written", result="skipped", note="HR 免试", version=1)
        application(session, ApplicationStatus.JOINED)
        summary = main._dashboard_data(session)["exam_counts"]
        assert summary["assessment"]["passed"] == 1 and summary["written"]["skipped"] == 1
        assert summary["assessment"]["historical_unrecorded"] == summary["written"]["historical_unrecorded"] == 1


@pytest.mark.parametrize("invalid", [None, "missing", "unknown", "invented"])
def test_online_evidence_references_are_validated_and_persisted(env, monkeypatch, invalid):
    from app.ai.service import _mock_evaluation
    _, _, factory = env
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://synthetic.example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "synthetic-model")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-only")
    with factory() as session:
        app = application(session, ApplicationStatus.OFFER_PENDING)
        snapshot = matching_input(app)
        evidence_id, quote = next((key,value) for key,value in snapshot["evidence_catalog"].items() if "项目：" in value)
        payload = _mock_evaluation(snapshot)
        for dimension in payload["dimensions"]:
            dimension["score"], dimension["references"] = 0, []
        payload["dimensions"][0].update(score=75, references=[{"id":evidence_id,"quote":quote}])
        if invalid == "missing": payload["dimensions"][0]["references"] = []
        if invalid == "unknown": payload["dimensions"][0]["references"][0]["id"] = "source:missing"
        if invalid == "invented": payload["dimensions"][0]["references"][0]["quote"] = "不存在的事实"
        def synthetic_request(self, messages):
            text = json.dumps(messages,ensure_ascii=False)
            assert app.candidate.phone not in text and app.candidate.email not in text
            return payload
        monkeypatch.setattr(LLMClient,"_request_json",synthetic_request)
        if invalid:
            with pytest.raises(ValueError, match="失败"):
                evaluate_application_record(session, app)
            assert session.scalar(select(EvaluationAttempt)).status == "failed"
            assert session.scalar(select(func.count(Evaluation.id))) == 0
        else:
            result = evaluate_application_record(session, app)
            evaluation = session.get(Evaluation, result["evaluation_id"])
            assert evaluation.scores[0]["references"] == [{"id":evidence_id,"quote":quote}]
        assert app.status == ApplicationStatus.OFFER_PENDING


def test_unknown_offer_delivery_verified_after_expiry_never_resends(env, monkeypatch):
    client, headers, factory = env
    with factory() as session:
        offer, _ = ready_offer(session)
        offers.send_offer(session,offer.id); session.commit()
        notification=session.scalar(select(Notification).where(Notification.target_type=="offer",Notification.channel=="email"))
        # 模拟 DATA 后进程中断：发送事实未明，业务仍在批准状态。
        notification.status=NotificationStatus.UNKNOWN
        offer.status=OfferStatus.APPROVED; offer.application.status=ApplicationStatus.OFFER_PENDING
        session.commit()
    monkeypatch.setattr(offers,"utc_now",lambda:utc_now()+timedelta(days=10))
    post(client,headers,f"/api/notifications/{notification.id}/verify",delivered="true",note="HR 在邮件服务日志核实原尝试已送达")
    with factory() as session:
        assert session.get(Offer,offer.id).status==OfferStatus.EXPIRED
        assert session.get(Application,offer.application_id).status==ApplicationStatus.OFFER_SENT
        assert session.scalar(select(func.count(Notification.id)).where(Notification.channel=="email"))==1


def test_fresh_process_restart_preserves_business_records_and_can_continue(env, tmp_path):
    import os
    import subprocess
    import sys
    _, _, factory = env
    with factory() as session:
        assess=application(session,ApplicationStatus.ASSESSMENT_PENDING)
        paper=resource(session)
        send_exam(session,assess.id,paper.id,candidate_revision=1)
        pending=imports.store_upload_bytes(original_filename="pending.txt",content="姓名：虚构重启\n手机：13900008888\n邮箱：restart@example.test\n项目：Python API".encode(),content_type="text/plain")
        task=imports.create_file_import_task(session,uploads=[pending],operator_id=None,job_id=assess.job_id)
        session.commit(); imports.parse_import_task(session,task.id); session.commit()
        interview_app=application(session,ApplicationStatus.INTERVIEW_CONFIRMED)
        session.add(Interview(application_id=interview_app.id,round=1,status=InterviewStatus.CONFIRMED,
                              confirmed_start_at=utc_now()+timedelta(days=1),confirmed_end_at=utc_now()+timedelta(days=1,hours=1),meeting_url="https://meeting.example.test/mock"))
        offer,_=ready_offer(session); offers.send_offer(session,offer.id); session.commit()
        accepted,_=ready_offer(session); offers.send_offer(session,accepted.id); session.commit()
        offers.respond_offer(session,accepted.candidate_token,accepted=True); session.commit()
        update_onboarding(session,accepted.application_id,action="update",version=0,values={"expected_date":(utc_now()+timedelta(days=2)).date().isoformat(),"location":"虚构前台"}); session.commit()
    run_followups(factory)
    database=factory.kw["bind"].url.database
    def snapshot():
        with sqlite3.connect(database) as connection:
            return {table:connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall() for table in
                ["candidates","applications","candidate_sources","import_tasks","import_files","resume_parse_runs","exam_resources","exam_dispatches","interviews","offers","offer_approvals","onboarding_tasks","notifications","audit_logs","follow_up_tasks"]}
    before=snapshot()
    file_before=pending.file_path.read_bytes()
    factory.kw["bind"].dispose()
    environment={**os.environ,"APP_DATA_DIR":str(tmp_path),"APP_RUNTIME_ENV":str(tmp_path/"runtime.env"),"DATABASE_URL":f"sqlite:///{database}","LLM_PROVIDER":"mock","SMTP_HOST":"","WECOM_WEBHOOK_URL":"","FOLLOWUP_ENABLED":"false"}
    subprocess.run([sys.executable,"-B","-c","from app.main import startup; startup()"],env=environment,check=True,capture_output=True)
    assert snapshot()==before and pending.file_path.read_bytes()==file_before
    with factory() as session:
        decide_exam(session,assess.id,kind="assessment",result="passed",note="重启后 HR 核实",version=0)
        confirmed=imports.confirm_import_task(session,task.id); session.commit()
        assert session.get(Application,assess.id).status==ApplicationStatus.WRITTEN_PENDING
        assert confirmed.candidate.email=="restart@example.test"
