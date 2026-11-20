"""自由注册默认关闭，本机受控创建与 Demo 注册分别验证。"""
import re
import sys
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func,select
from sqlalchemy.orm import sessionmaker
import app.main as main
import app.accounts as accounts
from app.db import create_db_engine,get_db,init_db
from app.models import User
from app.services.auth import verify_password


def csrf(client,path="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"',client.get(path).text)[1]


@pytest.fixture
def registration_env(tmp_path,monkeypatch):
    engine=create_db_engine(f"sqlite:///{(tmp_path / 'registration.db').as_posix()}")
    init_db(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setattr(main,"SessionLocal",factory)
    monkeypatch.setattr(main,"startup",lambda:init_db(engine))
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[get_db]=database
    with TestClient(main.app) as client:
        yield client,factory,engine
    main.app.dependency_overrides.clear()
    engine.dispose()


def test_production_register_get_and_post_are_closed(registration_env):
    client,factory,engine=registration_env
    token=csrf(client)
    assert 'href="/register"' not in client.get("/login").text
    assert client.get("/register").status_code==403
    assert client.post("/register",data={"username":"outsider","password":"synthetic-password-123","csrf_token":token}).status_code==403
    with factory() as session:
        assert session.scalar(select(func.count(User.id)))==0


def test_demo_registration_requires_explicit_mode(registration_env,monkeypatch):
    client,factory,engine=registration_env
    monkeypatch.setenv("APP_ENV","demo")
    assert 'href="/register"' in client.get("/login").text
    response=client.post("/register",data={"username":"demo-employee","password":"synthetic-password-123","csrf_token":csrf(client,"/register")},follow_redirects=False)
    assert response.status_code==303
    with factory() as session:
        assert session.scalar(select(func.count(User.id)))==1


def test_controlled_create_validates_identity_and_password(registration_env):
    client,factory,engine=registration_env
    with factory.begin() as session:
        user=accounts.create_account(session," HR-one ","synthetic-password-123")
        assert user.username=="HR-one" and verify_password("synthetic-password-123",user.password_hash)
    for username,password in [("hr-ONE","synthetic-password-123"),("xx","synthetic-password-123"),("another","short")]:
        with pytest.raises(ValueError),factory.begin() as session:
            accounts.create_account(session,username,password)
    assert client.post("/login",data={"username":"hr-ONE","password":"synthetic-password-123","csrf_token":csrf(client)},follow_redirects=False).status_code==303


def test_local_create_command_backs_up_without_echoing_password(registration_env,monkeypatch,capsys):
    client,factory,engine=registration_env
    monkeypatch.setattr(accounts,"SessionLocal",factory)
    monkeypatch.setattr(accounts,"engine",engine)
    monkeypatch.setattr(accounts,"init_db",lambda:init_db(engine))
    monkeypatch.setattr(accounts,"getpass",lambda prompt:"synthetic-command-password-123")
    monkeypatch.setattr(sys,"argv",["app.accounts","create","local-employee"])
    accounts.main()
    assert "synthetic-command-password-123" not in capsys.readouterr().out
    from pathlib import Path
    assert list((Path(engine.url.database).parent/'backups').glob('*.db'))
    with factory() as session:
        user=session.scalar(select(User).where(User.normalized_username=="local-employee"))
        assert user and verify_password("synthetic-command-password-123",user.password_hash)
