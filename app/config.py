from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR = DATA_DIR / "uploads"
ENV_FILE = Path(os.getenv("APP_RUNTIME_ENV", str(DATA_DIR / "runtime.env")))
SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_SSL", "SMTP_USE_SSL", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM")
SMTP_DEFAULTS = {"SMTP_HOST": "", "SMTP_PORT": "465", "SMTP_SSL": "true", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SMTP_FROM": ""}
EDITABLE_KEYS = (
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
    "LLM_CONNECT_TIMEOUT_SECONDS", "LLM_READ_TIMEOUT_SECONDS",
    "SMTP_HOST", "SMTP_PORT", "SMTP_SSL", "SMTP_USE_SSL", "SMTP_USERNAME", "SMTP_PASSWORD",
    "SMTP_FROM", "WECOM_WEBHOOK_URL", "APP_PUBLIC_BASE_URL",
)


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        # 环境变量用于首启，之后统一使用持久化的运行配置。
        legacy = {} if os.getenv("APP_DATA_DIR") else dotenv_values(ROOT_DIR / ".env")
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.touch()
        for name in EDITABLE_KEYS:
            if name in SMTP_KEYS:
                continue
            value = os.getenv(name, legacy.get(name) or ("mock" if name == "LLM_PROVIDER" else ""))
            set_key(str(ENV_FILE), name, value)


def reload_env(*, override: bool = True) -> None:
    if not os.getenv("APP_DATA_DIR"):
        load_dotenv(ROOT_DIR / ".env", override=False)
    legacy_smtp = not any(name in os.environ for name in SMTP_KEYS)
    for name, value in dotenv_values(ENV_FILE).items():
        # SMTP 整组取部署配置，避免混入旧授权码；完全未声明时兼容旧运行文件。
        if name in EDITABLE_KEYS and (name not in SMTP_KEYS or legacy_smtp):
            os.environ[name] = value or ""


def get_setting(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def public_url(path: str) -> str:
    from urllib.parse import urlsplit
    base = get_setting("APP_PUBLIC_BASE_URL") or "http://127.0.0.1:8000"
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("APP_PUBLIC_BASE_URL 必须是完整的 http(s) 地址")
    return base.rstrip("/") + "/" + path.lstrip("/")


def save_llm_settings(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None,
) -> None:
    ensure_runtime_dirs()
    set_key(str(ENV_FILE), "LLM_PROVIDER", provider)
    set_key(str(ENV_FILE), "LLM_BASE_URL", base_url)
    set_key(str(ENV_FILE), "LLM_MODEL", model)
    if api_key:
        set_key(str(ENV_FILE), "LLM_API_KEY", api_key)
    reload_env()


def clear_llm_api_key() -> None:
    ensure_runtime_dirs()
    set_key(str(ENV_FILE), "LLM_API_KEY", "")
    set_key(str(ENV_FILE), "LLM_PROVIDER", "mock")
    reload_env()


def save_app_setting(name: str, value: str | None) -> None:
    ensure_runtime_dirs()
    set_key(str(ENV_FILE), name, value or "")
    reload_env()


def user_smtp_file(user_id: int) -> Path:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("邮箱配置必须关联有效的 HR 账号")
    return DATA_DIR / "users" / str(user_id) / "smtp.runtime.env"


def smtp_settings(user_id: int | None = None) -> dict[str, str]:
    path = user_smtp_file(user_id) if user_id is not None else None
    personal = path is not None and path.is_file()
    raw = dotenv_values(path, interpolate=False) if personal else {key: get_setting(key) for key in SMTP_KEYS}
    values = {key: raw.get(key) or default for key, default in SMTP_DEFAULTS.items()}
    values["SMTP_SSL"] = raw.get("SMTP_SSL") or raw.get("SMTP_USE_SSL") or SMTP_DEFAULTS["SMTP_SSL"]
    values["SMTP_PORT"] = raw.get("SMTP_PORT") or ("465" if values["SMTP_SSL"].lower() in {"true", "1", "yes"} else "587")
    values["SMTP_FROM"] = values["SMTP_FROM"] or values["SMTP_USERNAME"]
    values["source"] = "personal" if personal else "default"
    return values


def save_smtp_settings(
    *, user_id: int, host: str, port: int, use_ssl: bool, username: str, password: str, from_address: str,
) -> None:
    path = user_smtp_file(user_id)
    previous = dotenv_values(path, interpolate=False) if path.is_file() else {}
    host, username, from_address = host.strip(), username.strip(), from_address.strip()
    if host and (len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)):
        raise ValueError("SMTP 服务器只填写主机名，例如 smtp.qq.com，不含协议、端口或路径")
    if not 1 <= port <= 65535:
        raise ValueError("SMTP 端口必须在 1–65535 之间")
    if any(char in username + password + from_address for char in "\r\n"):
        raise ValueError("邮箱账号、授权码和发件地址不能包含换行")
    from_address = from_address or username
    if host and (len(from_address) > 254 or not re.fullmatch(r"[^\s@<>]+@[^\s@.<>]+(?:\.[^\s@.<>]+)+", from_address)):
        raise ValueError("请填写有效的发件邮箱地址")
    if host and username and not password:
        if not previous.get("SMTP_PASSWORD"):
            raise ValueError("首次配置请填写 SMTP 授权码或密码；QQ 邮箱请使用授权码")
        if host != previous.get("SMTP_HOST") or username != previous.get("SMTP_USERNAME"):
            raise ValueError("更换 SMTP 服务器或邮箱账号时，请重新填写授权码或密码")
    values = {
        "SMTP_HOST": host, "SMTP_PORT": str(port), "SMTP_SSL": str(use_ssl).lower(),
        "SMTP_USERNAME": username, "SMTP_FROM": from_address,
        "SMTP_PASSWORD": password or previous.get("SMTP_PASSWORD") or "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # 一次替换整份个人配置，发送请求不会读到只写了一半的账号或授权码。
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".runtime.env", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        for name, value in values.items():
            set_key(str(temporary), name, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def public_llm_settings() -> dict[str, str | bool]:
    ensure_runtime_dirs()
    values = {name: get_setting(name) for name in EDITABLE_KEYS}
    key = values.get("LLM_API_KEY") or ""
    return {
        "provider": values.get("LLM_PROVIDER") or "mock",
        "base_url": values.get("LLM_BASE_URL") or "",
        "model": values.get("LLM_MODEL") or "",
        "api_key_configured": bool(key),
        "api_key_hint": f"••••{key[-4:]}" if key else "",
    }


def public_integration_settings(user_id: int | None = None) -> dict[str, str | bool]:
    ensure_runtime_dirs()
    values = {name: get_setting(name) for name in EDITABLE_KEYS}
    webhook = values.get("WECOM_WEBHOOK_URL") or ""
    smtp = smtp_settings(user_id)
    personal = smtp["source"] == "personal"
    return {
        "wecom_webhook_configured": bool(webhook),
        "meeting_provider": get_setting("MEETING_PROVIDER", "mock"),
        "meeting_token_configured": bool(get_setting("TENCENT_MEETING_TOKEN")),
        "wecom_webhook_hint": f"••••{webhook[-6:]}" if webhook else "",
        "smtp_configured": bool(smtp["SMTP_HOST"]),
        "smtp_source": smtp["source"],
        "smtp_effective_from": smtp["SMTP_FROM"],
        "smtp_host": smtp["SMTP_HOST"],
        "smtp_port": smtp["SMTP_PORT"],
        "smtp_ssl": smtp["SMTP_SSL"].lower() in {"true", "1", "yes"},
        "smtp_username": smtp["SMTP_USERNAME"] if personal else "",
        "smtp_from": smtp["SMTP_FROM"] if personal else "",
        "smtp_password_configured": personal and bool(smtp["SMTP_PASSWORD"]),
    }
