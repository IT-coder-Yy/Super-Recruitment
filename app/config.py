from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR = DATA_DIR / "uploads"
ENV_FILE = Path(os.getenv("APP_RUNTIME_ENV", str(DATA_DIR / "runtime.env")))
EDITABLE_KEYS = (
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
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
            value = os.getenv(name, legacy.get(name) or ("mock" if name == "LLM_PROVIDER" else ""))
            set_key(str(ENV_FILE), name, value)


def reload_env(*, override: bool = True) -> None:
    if not os.getenv("APP_DATA_DIR"):
        load_dotenv(ROOT_DIR / ".env", override=False)
    for name, value in dotenv_values(ENV_FILE).items():
        if name in EDITABLE_KEYS:
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


def public_integration_settings() -> dict[str, str | bool]:
    ensure_runtime_dirs()
    values = {name: get_setting(name) for name in EDITABLE_KEYS}
    webhook = values.get("WECOM_WEBHOOK_URL") or ""
    smtp_host = values.get("SMTP_HOST") or ""
    return {
        "wecom_webhook_configured": bool(webhook),
        "wecom_webhook_hint": f"••••{webhook[-6:]}" if webhook else "",
        "smtp_configured": bool(smtp_host),
        "smtp_host": smtp_host,
    }
