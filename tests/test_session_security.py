"""会话密钥启动边界与伪造 Cookie 回归，全部使用隔离数据。"""
import os
import subprocess
import sys
import pytest


@pytest.mark.parametrize("key", [None, "", "change-this-local-secret", "change-this-local-secret          ", "short"])
def test_production_refuses_unsafe_session_keys(tmp_path, key):
    environment = {**os.environ, "APP_ENV": "production", "APP_DATA_DIR": str(tmp_path), "APP_RUNTIME_ENV": str(tmp_path / "runtime.env"), "DATABASE_URL": "sqlite:///:memory:"}
    if key is None:
        environment.pop("APP_SECRET_KEY", None)
    else:
        environment["APP_SECRET_KEY"] = key
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", "import app.main"], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0
    assert "APP_SECRET_KEY" in result.stderr



def test_strong_key_allows_login_but_rejects_example_signed_cookie(tmp_path):
    environment = {**os.environ, "APP_ENV": "production", "APP_DATA_DIR": str(tmp_path), "APP_RUNTIME_ENV": str(tmp_path / "runtime.env"),
                   "DATABASE_URL": f"sqlite:///{(tmp_path / 'auth.db').as_posix()}", "APP_SECRET_KEY": "synthetic-strong-key-for-session-regression", "FOLLOWUP_ENABLED": "false"}
    code = """
import base64,json,re
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from app.main import app
with TestClient(app) as client:
    token=re.search(r'name="csrf_token" value="([^"]+)"',client.get('/login').text)[1]
    assert client.post('/login',data={'username':'demo-admin','password':'demo123456','csrf_token':token},follow_redirects=False).status_code==303
    assert client.get('/api/candidates').status_code==200
with TestClient(app) as forged:
    payload=base64.b64encode(json.dumps({'user_id':1}).encode())
    forged.cookies.set('talentflow_session',TimestampSigner('change-this-local-secret').sign(payload).decode())
    assert forged.get('/api/candidates').status_code==401
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stderr
