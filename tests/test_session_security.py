"""会话密钥启动边界与伪造 Cookie 回归，全部使用隔离数据。"""
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import pytest


def test_parallel_first_start_keeps_a_single_complete_config(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("需要 PowerShell 来运行首次初始化脚本")
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "initialize_env.ps1"
    shutil.copyfile(root / "scripts" / script.name, script)
    shutil.copyfile(root / ".env.example", tmp_path / ".env.example")
    worker = """
$ErrorActionPreference = 'Stop'
[Console]::WriteLine('ready')
$null = [Console]::ReadLine()
. ./scripts/initialize_env.ps1
$saved = [IO.File]::ReadAllText((Join-Path $PWD '.env'))
if ($SessionSecret -and -not $saved.Contains("APP_SECRET_KEY=$SessionSecret")) { exit 2 }
$sha = [Security.Cryptography.SHA256]::Create()
try { [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($saved))).Replace('-', '') }
finally { $sha.Dispose() }
"""
    args = [shell, "-NoProfile", "-NonInteractive", "-Command", worker]
    workers = [subprocess.Popen(args, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        assert all(process.stdout.readline().strip() == "ready" for process in workers)
        for process in workers:
            process.stdin.write("\n")
            process.stdin.flush()
        observed = [process.communicate(timeout=30)[0].strip() for process in workers]
        assert all(process.returncode == 0 for process in workers)
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    config = (tmp_path / ".env").read_bytes()
    assert observed == [hashlib.sha256(config).hexdigest().upper()] * len(workers)
    key = re.search(rb"(?m)^APP_SECRET_KEY=(\S+)", config)[1]
    assert len(base64.b64decode(key, validate=True)) == 32
    assert config.replace(key, b"") == (tmp_path / ".env.example").read_bytes()
    subprocess.run([shell, "-NoProfile", "-File", str(script)], cwd=tmp_path, check=True)
    assert hashlib.sha256((tmp_path / ".env").read_bytes()).hexdigest().upper() == observed[0]


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
