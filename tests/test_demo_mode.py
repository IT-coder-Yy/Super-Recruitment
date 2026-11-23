"""正式空库、显式演示和旧默认账号恢复的隔离回归。"""
import os
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
import app.main as main
from app.accounts import reset_password
from app.db import create_db_engine, get_db, init_db
from app.models import Candidate, User
from app.services.auth import hash_password


def csrf(client, path="/login"):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text) or re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    return match[1]


@pytest.fixture
def production_app(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'demo-policy.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(User(username="employee", normalized_username="employee", password_hash=hash_password("synthetic-password-123")))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "init_db", lambda: init_db(engine))
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[get_db] = database
    with TestClient(main.app) as client:
        response = client.post("/login", data={"username":"employee","password":"synthetic-password-123","csrf_token":csrf(client)}, follow_redirects=False)
        assert response.status_code == 303
        yield client, factory
    main.app.dependency_overrides.clear()
    engine.dispose()


def test_production_rejects_mock_capture_and_hides_its_entry(production_app):
    client, factory = production_app
    headers = {"X-CSRF-Token": csrf(client, "/")}
    for path in ("/api/import/mock", "/api/connectors/mock/capture"):
        assert client.post(path, headers=headers).status_code == 403
    assert client.get("/mock-platform").status_code == 403
    assert 'id="mockImportButton"' not in client.get("/import").text
    with factory() as session:
        assert session.scalar(select(func.count(Candidate.id))) == 0
        assert session.scalar(select(func.count(User.id))) == 1


def test_default_account_disabled_and_password_reset_revokes_session(production_app):
    client, factory = production_app
    with factory.begin() as session:
        session.add(User(username="demo-admin", normalized_username="demo-admin", password_hash=hash_password("demo123456")))
    main.startup()
    with factory() as session:
        assert session.scalar(select(User).where(User.normalized_username == "demo-admin")).is_active is False
    with TestClient(main.app) as public:
        assert public.post("/login", data={"username":"demo-admin","password":"demo123456","csrf_token":csrf(public)}, follow_redirects=False).status_code == 401
    with factory.begin() as session:
        reset_password(session, "employee", "synthetic-new-password-456")
        reset_password(session, "demo-admin", "synthetic-new-demo-password-456")
    assert client.get("/api/candidates").status_code == 401
    with TestClient(main.app) as public:
        assert public.post("/login", data={"username":"demo-admin","password":"synthetic-new-demo-password-456","csrf_token":csrf(public)}, follow_redirects=False).status_code == 303
        assert public.get("/api/candidates").status_code == 200


def test_production_restart_and_seed_cli_never_inject_demo_data(tmp_path):
    environment = {**os.environ, "APP_ENV":"production", "APP_DATA_DIR":str(tmp_path), "APP_RUNTIME_ENV":str(tmp_path / "runtime.env"),
                   "DATABASE_URL":f"sqlite:///{(tmp_path / 'boot.db').as_posix()}", "FOLLOWUP_ENABLED":"false"}
    code = "from app.main import startup; from app.db import SessionLocal; from app.models import User,Candidate; from sqlalchemy import select,func; startup(); startup(); s=SessionLocal(); assert s.scalar(select(func.count(User.id)))==0; assert s.scalar(select(func.count(Candidate.id)))==0"
    result = subprocess.run([sys.executable,"-X","utf8","-B","-c",code],env=environment,capture_output=True,text=True,encoding="utf-8",timeout=45)
    assert result.returncode == 0, result.stderr
    seed = subprocess.run([sys.executable,"-X","utf8","-B","-m","app.seed"],env=environment,capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert seed.returncode != 0 and "APP_ENV=demo" in seed.stderr


def test_explicit_demo_boot_and_capture_do_not_recreate_deleted_account(tmp_path):
    environment = {**os.environ, "APP_ENV":"demo", "APP_DATA_DIR":str(tmp_path), "APP_RUNTIME_ENV":str(tmp_path / "runtime.env"),
                   "DATABASE_URL":f"sqlite:///{(tmp_path / 'demo.db').as_posix()}", "FOLLOWUP_ENABLED":"false"}
    code = """
from app.main import startup,_capture_mock_recruitment_data
from app.db import SessionLocal
from app.models import User,Candidate
from sqlalchemy import select, func
from app.accounts import assign_data, create_account
from app.ownership import account_scope
startup()
with SessionLocal.begin() as session:
    demo = session.scalars(select(User)).one()
    assert demo.normalized_username == "demo-admin" and demo.is_admin
    assert session.scalar(select(func.count(Candidate.id)).where(Candidate.owner_user_id == demo.id)) == 7
    recipient = create_account(session, "successor", "synthetic-review-password")
    recipient_id = recipient.id
    assign_data(session, recipient, source=demo)
    session.delete(demo)
with account_scope(recipient_id), SessionLocal() as session:
    _capture_mock_recruitment_data(session)
    assert session.scalars(select(User.id)).all() == [recipient_id]
    assert session.scalar(select(func.count(Candidate.id))) == 7
"""
    result = subprocess.run([sys.executable,"-X","utf8","-B","-c",code],env=environment,capture_output=True,text=True,encoding="utf-8",timeout=45)
    assert result.returncode == 0, result.stderr
