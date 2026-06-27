from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR = DATA_DIR / "uploads"
ENV_FILE = ROOT_DIR / ".env"


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text(
            (ROOT_DIR / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def reload_env(*, override: bool = True) -> None:
    load_dotenv(ENV_FILE, override=override)


def get_setting(name: str, default: str = "") -> str:
    return os.getenv(name, default)


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
    values = dotenv_values(ENV_FILE)
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
    values = dotenv_values(ENV_FILE)
    webhook = values.get("WECOM_WEBHOOK_URL") or ""
    smtp_host = values.get("SMTP_HOST") or ""
    return {
        "wecom_webhook_configured": bool(webhook),
        "wecom_webhook_hint": f"••••{webhook[-6:]}" if webhook else "",
        "smtp_configured": bool(smtp_host),
        "smtp_host": smtp_host,
    }
