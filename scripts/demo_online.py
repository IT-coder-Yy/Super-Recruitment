"""24组真实模型回放的专用配置与请求留痕；不读取项目生产配置。"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from dotenv import dotenv_values
import httpx

from app.ai.client import LLMClient, DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT


def online_client(path: Path) -> LLMClient:
    if not path.is_file():
        raise ValueError("专用测试配置文件不存在")
    values = dotenv_values(path, interpolate=False)
    names = ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
    if any(not (values.get(name) or "").strip() for name in names):
        raise ValueError("专用测试配置必须填写 LLM_PROVIDER、LLM_BASE_URL、LLM_MODEL、LLM_API_KEY")
    client = LLMClient(**{key: values[name] for key, name in zip(("provider", "base_url", "model", "api_key"), names)},
                       connect_timeout=values.get("LLM_CONNECT_TIMEOUT_SECONDS") or DEFAULT_CONNECT_TIMEOUT,
                       read_timeout=values.get("LLM_READ_TIMEOUT_SECONDS") or DEFAULT_READ_TIMEOUT)
    if client._configuration_error():
        raise ValueError("专用测试配置必须使用支持的在线模型模式")
    url = urlsplit(client.base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("测试模型地址须为无账号、查询参数或片段的 HTTPS 地址")
    return client


class RequestRecorder:
    """每组最多一次请求，只向指定端点发送；不记录 Authorization。"""

    def __init__(self, client: LLMClient, output: Path):
        self.client, self.output = client, output
        self.endpoint = client._chat_completions_url()
        self.send = httpx.post
        self.pair = None
        self.requests = []

    def redact(self, value):
        if isinstance(value, str):
            for secret in (self.client.api_key, json.dumps(self.client.api_key, ensure_ascii=False)[1:-1],
                           json.dumps(self.client.api_key, ensure_ascii=True)[1:-1]):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        return value

    def post(self, url, **kwargs):
        if (url != self.endpoint or not self.pair or len(self.requests) >= 24
                or any(row["pair"] == self.pair for row in self.requests)
                or kwargs.get("json", {}).get("model") != self.client.model):
            raise ValueError("请求超出24组、每组一次或指定模型端点范围")
        record = {"pair": self.pair, "client_request_id": uuid.uuid4().hex,
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "request": kwargs["json"], "status": "started"}
        self.requests.append(record)
        self.save()
        try:
            # 禁止重定向和环境代理，密钥仅交给显式指定的 HTTPS 端点。
            kwargs.update(follow_redirects=False, trust_env=False)
            response = self.send(url, **kwargs)
            record.update(http_status=response.status_code, status="responded",
                          service_request_id=response.headers.get("x-request-id") or response.headers.get("request-id"),
                          response_body=response.text)
            try:
                envelope = response.json()
                record["response_body"] = envelope
                if isinstance(envelope, dict):
                    record.update(completion_id=envelope.get("id"), response_model=envelope.get("model"),
                                  usage=envelope.get("usage"))
            except ValueError:
                pass  # 保留原响应；合法性仍由真实客户端和业务校验器判断。
            if self.redact(record) != record:
                raise ValueError("服务端回显密钥，已脱敏记录并拒绝进入业务结果")
            return response
        except Exception as error:
            record.update(status="request_failed", error_type=type(error).__name__)
            raise
        finally:
            self.save()

    def save(self):
        self.output.write_text(json.dumps(self.redact(self.requests), ensure_ascii=False, indent=2), encoding="utf-8")
