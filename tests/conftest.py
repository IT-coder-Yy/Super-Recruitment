"""回归只使用虚构配置、内存库和独立目录；禁止真实网络连接。"""
from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest


TEST_ROOT = Path(os.environ["TALENTFLOW_TEST_ROOT"]) if os.environ.get("TALENTFLOW_TEST_ROOT") else (
    Path(__file__).resolve().parents[1] / ".test-runs" / f"checks-{uuid.uuid4().hex}"
)
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "APP_DATA_DIR": str(TEST_ROOT / "data"),
    "APP_RUNTIME_ENV": str(TEST_ROOT / "data" / "runtime.env"),
    "DATABASE_URL": "sqlite:///:memory:",
    "LLM_PROVIDER": "mock", "LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": "",
    "SMTP_HOST": "", "SMTP_PASSWORD": "", "WECOM_WEBHOOK_URL": "",
    "APP_SECRET_KEY": "isolated-test-secret-not-for-deployment",
    "APP_PUBLIC_BASE_URL": "http://testserver", "FOLLOWUP_ENABLED": "false",
})


@pytest.fixture(autouse=True)
def deny_real_network(monkeypatch):
    import socket
    from ipaddress import ip_address
    original_connect = socket.socket.connect
    def denied(*args, **kwargs):
        raise AssertionError("验收禁止真实外呼，请注入适配器替身")
    def connect(sock, address):
        # Windows asyncio 用回环 socketpair 建立内部唤醒管道。
        if isinstance(address, tuple):
            try:
                if ip_address(address[0]).is_loopback:
                    return original_connect(sock, address)
            except ValueError:
                pass
        return denied()
    monkeypatch.setattr("socket.socket.connect", connect)
    monkeypatch.setattr("socket.create_connection", denied)
