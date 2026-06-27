from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


DEFAULT_TIMEOUT_SECONDS = 5.0
WECOM_WEBHOOK_ENV = "WECOM_WEBHOOK_URL"


class NotificationError(RuntimeError):
    """外部通知失败。result 保留可审计的结构化结果。"""

    def __init__(self, message: str, *, result: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.result = dict(result)


class WeComWebhookNotifier:
    """企业微信群机器人通知；未配置时自动进入 Mock 模式。"""

    def __init__(
        self,
        webhook_url: str | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.webhook_url = (
            webhook_url
            if webhook_url is not None
            else os.getenv(WECOM_WEBHOOK_ENV, "")
        ).strip()
        self.timeout_seconds = timeout_seconds
        self.client = client

    @property
    def is_mock(self) -> bool:
        return not bool(self.webhook_url)

    def send(
        self,
        content: str,
        *,
        msgtype: str = "text",
        mentioned_list: Sequence[str] | None = None,
        mentioned_mobile_list: Sequence[str] | None = None,
        raise_on_error: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload = build_wecom_payload(
            content,
            msgtype=msgtype,
            mentioned_list=mentioned_list,
            mentioned_mobile_list=mentioned_mobile_list,
        )
        resolved_request_id = request_id or _payload_fingerprint(payload)

        if self.is_mock:
            return {
                "ok": True,
                "channel": "wecom",
                "mode": "mock",
                "is_mock": True,
                "status": "mocked",
                "request_id": resolved_request_id,
                "payload": payload,
                "response": {"errcode": 0, "errmsg": "mocked: webhook not configured"},
                "error": None,
            }

        try:
            if self.client is None:
                response = httpx.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            else:
                response = self.client.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            response_data = response.json()
            if not isinstance(response_data, Mapping):
                raise ValueError("企业微信响应不是 JSON 对象")
            errcode = response_data.get("errcode")
            if errcode not in (0, "0"):
                result = _failure_result(
                    payload=payload,
                    request_id=resolved_request_id,
                    error_type="wecom_api_error",
                    error_message=str(response_data.get("errmsg") or "企业微信返回失败"),
                    response=dict(response_data),
                )
                return _raise_or_return(result, raise_on_error)
            return {
                "ok": True,
                "channel": "wecom",
                "mode": "webhook",
                "is_mock": False,
                "status": "sent",
                "request_id": resolved_request_id,
                "payload": payload,
                "response": dict(response_data),
                "error": None,
            }
        except httpx.TimeoutException as exc:
            result = _failure_result(
                payload=payload,
                request_id=resolved_request_id,
                error_type="timeout",
                error_message=str(exc) or "企业微信调用超时",
            )
        except httpx.HTTPStatusError as exc:
            result = _failure_result(
                payload=payload,
                request_id=resolved_request_id,
                error_type="http_error",
                error_message=str(exc),
                response={
                    "status_code": exc.response.status_code,
                    "body": exc.response.text[:1000],
                },
            )
        except (httpx.RequestError, ValueError, json.JSONDecodeError) as exc:
            result = _failure_result(
                payload=payload,
                request_id=resolved_request_id,
                error_type="request_error",
                error_message=str(exc),
            )
        except Exception as exc:  # 注入的兼容客户端可能抛出非 httpx 异常
            result = _failure_result(
                payload=payload,
                request_id=resolved_request_id,
                error_type="unexpected_error",
                error_message=str(exc),
            )
        return _raise_or_return(result, raise_on_error)


def build_wecom_payload(
    content: str,
    *,
    msgtype: str = "text",
    mentioned_list: Sequence[str] | None = None,
    mentioned_mobile_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """构造企业微信群机器人 text 或 markdown 消息。"""

    text = str(content).strip()
    if not text:
        raise ValueError("通知内容不能为空")
    normalized_type = msgtype.strip().lower()
    if normalized_type == "text":
        return {
            "msgtype": "text",
            "text": {
                "content": text,
                "mentioned_list": list(mentioned_list or ()),
                "mentioned_mobile_list": list(mentioned_mobile_list or ()),
            },
        }
    if normalized_type == "markdown":
        return {"msgtype": "markdown", "markdown": {"content": text}}
    raise ValueError("msgtype 仅支持 text 或 markdown")


def send_wecom_notification(
    content: str,
    *,
    webhook_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """函数式企业微信通知入口。"""

    notifier = WeComWebhookNotifier(
        webhook_url,
        timeout_seconds=timeout_seconds,
        client=client,
    )
    return notifier.send(content, **kwargs)


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _failure_result(
    *,
    payload: Mapping[str, Any],
    request_id: str,
    error_type: str,
    error_message: str,
    response: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "channel": "wecom",
        "mode": "webhook",
        "is_mock": False,
        "status": "failed",
        "request_id": request_id,
        "payload": dict(payload),
        "response": dict(response or {}),
        "error": {"type": error_type, "message": error_message},
    }


def _raise_or_return(
    result: dict[str, Any], raise_on_error: bool
) -> dict[str, Any]:
    if raise_on_error:
        message = str(result.get("error", {}).get("message") or "企业微信通知失败")
        raise NotificationError(message, result=result)
    return result
