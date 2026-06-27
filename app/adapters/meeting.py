from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


MOCK_PROVIDER = "tencent_meeting_mock"
MOCK_JOIN_URL_PREFIX = "https://meeting.tencent.com/dm/mock-"


class MockTencentMeetingAdapter:
    """无凭证也可稳定演示的腾讯会议 Mock 适配器。"""

    provider = MOCK_PROVIDER
    is_mock = True

    def create_meeting(
        self,
        *,
        title: str,
        start_at: datetime | str,
        end_at: datetime | str,
        participants: Sequence[Any] | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(title).strip():
            raise ValueError("会议标题不能为空")
        start_text = _datetime_text(start_at)
        end_text = _datetime_text(end_at)
        if _parse_datetime(start_text) >= _parse_datetime(end_text):
            raise ValueError("会议结束时间必须晚于开始时间")

        participant_list = [_participant_value(item) for item in participants or ()]
        seed = idempotency_key or _canonical_json(
            {
                "title": str(title).strip(),
                "start_at": start_text,
                "end_at": end_text,
                "participants": participant_list,
                "metadata": dict(metadata or {}),
            }
        )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        meeting_id = f"mock-tm-{digest[:20]}"
        meeting_code = _numeric_code(digest)

        return {
            "ok": True,
            "provider": self.provider,
            "mode": "mock",
            "is_mock": True,
            "status": "created",
            "meeting_id": meeting_id,
            "meeting_code": meeting_code,
            "join_url": f"{MOCK_JOIN_URL_PREFIX}{meeting_code}",
            "title": str(title).strip(),
            "start_at": start_text,
            "end_at": end_text,
            "participants": participant_list,
            "idempotency_key": idempotency_key,
            "metadata": dict(metadata or {}),
        }

    def cancel_meeting(
        self, meeting_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        if not meeting_id:
            raise ValueError("meeting_id 不能为空")
        return {
            "ok": True,
            "provider": self.provider,
            "mode": "mock",
            "is_mock": True,
            "status": "cancelled",
            "meeting_id": meeting_id,
            "reason": reason,
        }


def create_mock_tencent_meeting(**kwargs: Any) -> dict[str, Any]:
    """函数式 Mock 会议创建入口。"""

    return MockTencentMeetingAdapter().create_meeting(**kwargs)


def create_mock_meeting_link(
    idempotency_key: str,
    *,
    title: str = "候选人面试",
    start_at: datetime | str = "2000-01-01T09:00:00+08:00",
    end_at: datetime | str = "2000-01-01T10:00:00+08:00",
) -> str:
    """仅需稳定链接时使用；相同幂等键始终得到相同链接。"""

    result = create_mock_tencent_meeting(
        title=title,
        start_at=start_at,
        end_at=end_at,
        idempotency_key=idempotency_key,
    )
    return str(result["join_url"])


def _datetime_text(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        parsed = _parse_datetime(value)
        return parsed.isoformat()
    raise TypeError("时间必须是 datetime 或 ISO 8601 字符串")


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"无法解析时间：{value}") from exc


def _participant_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item)
            for key, item in sorted(vars(value).items())
            if not key.startswith("_")
        }
    return _json_safe(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _numeric_code(digest: str) -> str:
    digits = str(int(digest[:16], 16)).zfill(10)[-10:]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
