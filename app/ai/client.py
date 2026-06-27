"""Mock 与 OpenAI-compatible 双模式 JSON 客户端。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx


JsonObject = dict[str, Any]
FallbackFactory = Callable[[], JsonObject]
Validator = Callable[[JsonObject], JsonObject]

ONLINE_PROVIDERS = {
    "openai",
    "openai-compatible",
    "openai_compatible",
    "compatible",
}


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def get_llm_settings() -> dict[str, str]:
    """读取 LLM_* 配置，并兼容无前缀变量。"""

    return {
        "provider": _first_env("LLM_PROVIDER", default="mock").lower(),
        "base_url": _first_env("LLM_BASE_URL", "BASE_URL"),
        "api_key": _first_env("LLM_API_KEY", "API_KEY"),
        "model": _first_env("LLM_MODEL", "MODEL"),
    }


def _strict_json_object(text: str) -> JsonObject:
    def reject_constant(value: str) -> None:
        raise ValueError(f"JSON 不允许常量 {value}")

    parsed = json.loads(text, parse_constant=reject_constant)
    if not isinstance(parsed, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return parsed


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    return message[:240] or error.__class__.__name__


class LLMClient:
    """同步模型客户端；任何在线调用失败均自动降级到本地 Mock。"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_llm_settings()
        self.provider = (provider or settings["provider"] or "mock").lower()
        self.base_url = (base_url if base_url is not None else settings["base_url"]).strip()
        self.api_key = api_key if api_key is not None else settings["api_key"]
        self.model = (model if model is not None else settings["model"]).strip()
        self.timeout = timeout

    def generate_json(
        self,
        *,
        messages: list[dict[str, str]],
        fallback: FallbackFactory,
        validator: Validator | None = None,
    ) -> dict[str, Any]:
        """返回包含 content 和调用元数据的普通 dict。"""

        validate = validator or self._identity_validator
        if self.provider == "mock":
            return {
                "content": validate(fallback()),
                "meta": {
                    "requested_provider": "mock",
                    "provider": "mock",
                    "model": "mock",
                    "fallback": False,
                    "fallback_reason": None,
                },
            }

        config_error = self._configuration_error()
        if config_error:
            return self._fallback_result(fallback, validate, config_error)

        try:
            content = validate(self._request_json(messages))
            return {
                "content": content,
                "meta": {
                    "requested_provider": self.provider,
                    "provider": "openai_compatible",
                    "model": self.model,
                    "fallback": False,
                    "fallback_reason": None,
                },
            }
        except Exception as error:
            return self._fallback_result(fallback, validate, _safe_error(error))

    def _configuration_error(self) -> str | None:
        if self.provider not in ONLINE_PROVIDERS:
            return f"不支持的 LLM_PROVIDER: {self.provider}"
        if not self.api_key:
            return "未配置 API Key"
        if not self.base_url:
            return "未配置 Base URL"
        if not self.model:
            return "未配置模型名称"
        return None

    def _request_json(self, messages: list[dict[str, str]]) -> JsonObject:
        endpoint = self._chat_completions_url()
        response = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        envelope = _strict_json_object(response.text)
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("模型响应缺少 choices")
        first_choice = choices[0]
        if not isinstance(first_choice, Mapping):
            raise ValueError("模型响应 choices[0] 格式错误")
        message = first_choice.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("模型响应缺少 message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型响应 content 为空")
        return _strict_json_object(content)

    def _chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _fallback_result(
        self,
        fallback: FallbackFactory,
        validator: Validator,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "content": validator(fallback()),
            "meta": {
                "requested_provider": self.provider,
                "provider": "mock",
                "model": "mock",
                "fallback": True,
                "fallback_reason": reason,
            },
        }

    @staticmethod
    def _identity_validator(value: JsonObject) -> JsonObject:
        if not isinstance(value, dict):
            raise ValueError("结果必须是 dict")
        return value


def create_llm_client(**overrides: Any) -> LLMClient:
    return LLMClient(**overrides)
