from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_WORKING_HOURS = ("09:00", "18:00")
DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)
DEFAULT_DURATION_MINUTES = 60
DEFAULT_STEP_MINUTES = 30
DEFAULT_BUFFER_MINUTES = 15
DEFAULT_TOKEN_MAX_AGE_SECONDS = 48 * 60 * 60
DEFAULT_TOKEN_SALT = "interview-booking-v1"
IGNORED_BOOKING_STATUSES = frozenset(
    {"cancelled", "canceled", "expired", "rejected", "declined"}
)

_START_FIELDS = ("start_at", "start", "start_time", "begin_at", "begin")
_END_FIELDS = ("end_at", "end", "end_time", "finish_at", "finish")


class SchedulingError(ValueError):
    """排期输入不合法。"""


class BookingTokenError(ValueError):
    """预约 Token 校验失败，code 可供 API 层映射错误码。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def generate_candidate_slots(
    window_start: datetime | str,
    window_end: datetime | str,
    busy_intervals: Any = (),
    *,
    working_hours: Any = DEFAULT_WORKING_HOURS,
    weekdays: Sequence[int] = DEFAULT_WEEKDAYS,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    step_minutes: int = DEFAULT_STEP_MINUTES,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    limit: int = 5,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | str | None = None,
    minimum_notice_minutes: int = 0,
) -> list[dict[str, Any]]:
    """在工作时间内生成稳定、有序且无冲突的候选面试时段。

    ``busy_intervals`` 可为区间字典/对象列表，也可为
    ``{"interviewer": [...], "hr": [...]}`` 形式的多参与人忙碌表。
    区间采用半开语义 ``[start, end)``，相邻区间本身不冲突。
    """

    if duration_minutes <= 0 or step_minutes <= 0:
        raise SchedulingError("面试时长和步进必须大于 0")
    if buffer_minutes < 0 or minimum_notice_minutes < 0:
        raise SchedulingError("缓冲时间和最小提前量不能小于 0")
    if limit < 0:
        raise SchedulingError("候选时段数量不能小于 0")
    if limit == 0:
        return []

    tz = ZoneInfo(timezone_name)
    start = _coerce_datetime(window_start, tz)
    end = _coerce_datetime(window_end, tz)
    if start >= end:
        raise SchedulingError("排期窗口的结束时间必须晚于开始时间")

    effective_start = start
    if now is not None:
        notice_start = _coerce_datetime(now, tz) + timedelta(
            minutes=minimum_notice_minutes
        )
        effective_start = max(effective_start, notice_start)
    if effective_start >= end:
        return []

    valid_weekdays = tuple(dict.fromkeys(int(day) for day in weekdays))
    if any(day < 0 or day > 6 for day in valid_weekdays):
        raise SchedulingError("weekdays 只能包含 0 到 6")

    expanded_busy = []
    buffer_delta = timedelta(minutes=buffer_minutes)
    for record in _flatten_interval_records(busy_intervals):
        if not _is_active_record(record):
            continue
        busy_start, busy_end = _extract_interval(record, tz)
        if busy_start >= busy_end:
            raise SchedulingError("忙碌区间的结束时间必须晚于开始时间")
        expanded_busy.append((busy_start - buffer_delta, busy_end + buffer_delta))
    merged_busy = _merge_intervals(expanded_busy)

    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    slots: list[dict[str, Any]] = []
    current_date = effective_start.date()
    last_date = end.date()

    while current_date <= last_date and len(slots) < limit:
        if current_date.weekday() in valid_weekdays:
            for work_start, work_end in _working_windows_for_date(
                current_date, working_hours, tz
            ):
                allowed_start = max(work_start, effective_start)
                allowed_end = min(work_end, end)
                cursor = _ceil_to_step(allowed_start, work_start, step)

                while cursor + duration <= allowed_end and len(slots) < limit:
                    slot_end = cursor + duration
                    if not _has_conflict(cursor, slot_end, merged_busy):
                        slots.append(
                            {
                                "start_at": cursor,
                                "end_at": slot_end,
                                "timezone": timezone_name,
                                "duration_minutes": duration_minutes,
                            }
                        )
                    cursor += step
        current_date += timedelta(days=1)

    return slots


def intervals_conflict(
    start_at: datetime | str,
    end_at: datetime | str,
    other_start_at: datetime | str,
    other_end_at: datetime | str,
    *,
    buffer_minutes: int = 0,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> bool:
    """判断两个半开时间区间是否冲突，可在第二个区间两侧增加缓冲。"""

    if buffer_minutes < 0:
        raise SchedulingError("缓冲时间不能小于 0")
    tz = ZoneInfo(timezone_name)
    start = _coerce_datetime(start_at, tz)
    end = _coerce_datetime(end_at, tz)
    other_start = _coerce_datetime(other_start_at, tz)
    other_end = _coerce_datetime(other_end_at, tz)
    if start >= end or other_start >= other_end:
        raise SchedulingError("时间区间的结束时间必须晚于开始时间")
    buffer_delta = timedelta(minutes=buffer_minutes)
    return start < other_end + buffer_delta and end > other_start - buffer_delta


def find_slot_conflicts(
    start_at: datetime | str,
    end_at: datetime | str,
    records_or_session: Any,
    *,
    model: Any = None,
    statement: Any = None,
    scalars: bool = True,
    exclude_id: Any = None,
    id_field: str = "id",
    ignored_statuses: Iterable[str] = IGNORED_BOOKING_STATUSES,
    buffer_minutes: int = 0,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[Any]:
    """从普通记录或 SQLAlchemy Session 中找出与目标时段冲突的记录。

    Session 模式须传 ``model`` 或 ``statement``。该函数只读取数据，
    最终预约仍应由调用方在事务中再次检查并写入唯一约束保护。
    """

    records = _load_records(
        records_or_session, model=model, statement=statement, scalars=scalars
    )
    ignored = {str(status).strip().lower() for status in ignored_statuses}
    conflicts = []
    for record in records:
        if exclude_id is not None and _get_value(record, id_field) == exclude_id:
            continue
        status = _get_value(record, "status")
        if status is not None and str(status).strip().lower() in ignored:
            continue
        try:
            other_start, other_end = _extract_interval(
                record, ZoneInfo(timezone_name)
            )
        except (KeyError, TypeError):
            continue
        if intervals_conflict(
            start_at,
            end_at,
            other_start,
            other_end,
            buffer_minutes=buffer_minutes,
            timezone_name=timezone_name,
        ):
            conflicts.append(record)
    return conflicts


def is_slot_available(
    start_at: datetime | str,
    end_at: datetime | str,
    records_or_session: Any,
    **kwargs: Any,
) -> bool:
    """``find_slot_conflicts`` 的布尔便捷接口。"""

    return not find_slot_conflicts(
        start_at, end_at, records_or_session, **kwargs
    )


def create_booking_token(
    payload: Mapping[str, Any],
    secret_key: str,
    *,
    salt: str = DEFAULT_TOKEN_SALT,
    token_id: str | None = None,
    issued_at: datetime | str | None = None,
) -> str:
    """创建带签名预约 Token。

    Token 默认带随机 ``jti``，调用方可保存其哈希并在成功预约后标记已使用。
    """

    if not secret_key:
        raise SchedulingError("secret_key 不能为空")
    if not isinstance(payload, Mapping):
        raise SchedulingError("payload 必须是映射")

    data = dict(payload)
    data.setdefault("jti", token_id or secrets.token_urlsafe(18))
    timestamp = (
        _coerce_datetime(issued_at, timezone.utc)
        if issued_at is not None
        else datetime.now(timezone.utc)
    )
    data.setdefault("iat", timestamp.isoformat())
    serializer = URLSafeTimedSerializer(secret_key=secret_key, salt=salt)
    return serializer.dumps(data)


def verify_booking_token(
    token: str,
    secret_key: str,
    *,
    max_age_seconds: int = DEFAULT_TOKEN_MAX_AGE_SECONDS,
    salt: str = DEFAULT_TOKEN_SALT,
    used_token_ids: Any = None,
) -> dict[str, Any]:
    """校验 Token 的签名、48 小时有效期及可选的一次性消费状态。"""

    if not token or not secret_key:
        raise BookingTokenError("Token 或密钥不能为空", code="invalid_token")
    if max_age_seconds <= 0:
        raise SchedulingError("Token 有效期必须大于 0")

    serializer = URLSafeTimedSerializer(secret_key=secret_key, salt=salt)
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except SignatureExpired as exc:
        raise BookingTokenError("预约 Token 已过期", code="token_expired") from exc
    except BadSignature as exc:
        raise BookingTokenError("预约 Token 无效", code="invalid_token") from exc

    if not isinstance(payload, dict):
        raise BookingTokenError("预约 Token 内容无效", code="invalid_payload")
    token_id = payload.get("jti")
    if not token_id:
        raise BookingTokenError("预约 Token 缺少 jti", code="invalid_payload")
    if _token_was_used(str(token_id), used_token_ids):
        raise BookingTokenError("预约 Token 已使用", code="token_used")
    return payload


def validate_booking_token(
    token: str,
    secret_key: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """不抛业务异常的 Token 校验接口，适合直接供路由层使用。"""

    try:
        payload = verify_booking_token(token, secret_key, **kwargs)
        return {"ok": True, "code": "valid", "payload": payload}
    except BookingTokenError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc), "payload": None}


def booking_token_fingerprint(token_or_id: str) -> str:
    """生成可入库的一次性 Token 指纹，避免保存明文 Token。"""

    return hashlib.sha256(token_or_id.encode("utf-8")).hexdigest()


def _coerce_datetime(value: datetime | str, tz: Any) -> datetime:
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise SchedulingError(f"无法解析时间：{value}") from exc
    else:
        raise TypeError("时间必须是 datetime 或 ISO 8601 字符串")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _coerce_time(value: time | str) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip())
        except ValueError as exc:
            raise SchedulingError(f"无法解析工作时间：{value}") from exc
    raise TypeError("工作时间必须是 time 或 HH:MM 字符串")


def _working_windows_for_date(
    day: date, working_hours: Any, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    spec = working_hours
    if isinstance(working_hours, Mapping):
        spec = working_hours.get(day.weekday(), working_hours.get(str(day.weekday())))
        if spec is None:
            return []

    if _looks_like_time_pair(spec):
        windows = [spec]
    elif isinstance(spec, Iterable) and not isinstance(spec, (str, bytes)):
        windows = list(spec)
    else:
        raise SchedulingError("working_hours 格式无效")

    result = []
    for window in windows:
        if not _looks_like_time_pair(window):
            raise SchedulingError("每个工作时间窗口必须包含开始和结束时间")
        start_time = _coerce_time(window[0])
        end_time = _coerce_time(window[1])
        start = datetime.combine(day, start_time, tzinfo=tz)
        end = datetime.combine(day, end_time, tzinfo=tz)
        if start >= end:
            raise SchedulingError("工作时间的结束时间必须晚于开始时间")
        result.append((start, end))
    return sorted(result)


def _looks_like_time_pair(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
        and all(isinstance(item, (str, time)) for item in value)
    )


def _flatten_interval_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if _is_interval_record(value):
        return [value]
    if isinstance(value, Mapping):
        flattened = []
        for nested in value.values():
            flattened.extend(_flatten_interval_records(nested))
        return flattened
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        flattened = []
        for nested in value:
            flattened.extend(_flatten_interval_records(nested))
        return flattened
    raise TypeError("busy_intervals 必须是区间记录、列表或映射")


def _is_interval_record(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(field in value for field in _START_FIELDS) and any(
            field in value for field in _END_FIELDS
        )
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) >= 2
    ):
        return isinstance(value[0], (datetime, str)) and isinstance(
            value[1], (datetime, str)
        )
    return any(hasattr(value, field) for field in _START_FIELDS) and any(
        hasattr(value, field) for field in _END_FIELDS
    )


def _extract_interval(record: Any, tz: ZoneInfo) -> tuple[datetime, datetime]:
    if (
        isinstance(record, Sequence)
        and not isinstance(record, (str, bytes, Mapping))
        and len(record) >= 2
    ):
        start_value, end_value = record[0], record[1]
    else:
        start_value = _first_value(record, _START_FIELDS)
        end_value = _first_value(record, _END_FIELDS)
    return _coerce_datetime(start_value, tz), _coerce_datetime(end_value, tz)


def _first_value(record: Any, fields: Sequence[str]) -> Any:
    for field in fields:
        value = _get_value(record, field)
        if value is not None:
            return value
    raise KeyError(f"记录缺少字段：{', '.join(fields)}")


def _get_value(record: Any, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    mapping = getattr(record, "_mapping", None)
    if mapping is not None:
        return mapping.get(field, default)
    return getattr(record, field, default)


def _is_active_record(record: Any) -> bool:
    status = _get_value(record, "status")
    return status is None or str(status).strip().lower() not in IGNORED_BOOKING_STATUSES


def _merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _has_conflict(
    start: datetime,
    end: datetime,
    intervals: Sequence[tuple[datetime, datetime]],
) -> bool:
    for busy_start, busy_end in intervals:
        if busy_start >= end:
            return False
        if start < busy_end and end > busy_start:
            return True
    return False


def _ceil_to_step(
    value: datetime, anchor: datetime, step: timedelta
) -> datetime:
    if value <= anchor:
        return anchor
    elapsed = value - anchor
    steps, remainder = divmod(elapsed, step)
    return anchor + step * (steps + (1 if remainder else 0))


def _load_records(
    source: Any,
    *,
    model: Any = None,
    statement: Any = None,
    scalars: bool = True,
) -> list[Any]:
    if source is None:
        return []
    if statement is not None:
        if not hasattr(source, "execute"):
            raise TypeError("传入 statement 时 source 必须是 SQLAlchemy Session")
        result = source.execute(statement)
        return list(result.scalars().all() if scalars else result.mappings().all())
    if model is not None:
        if not hasattr(source, "query"):
            raise TypeError("传入 model 时 source 必须是 SQLAlchemy Session")
        return list(source.query(model).all())
    if hasattr(source, "all") and callable(source.all):
        return list(source.all())
    if isinstance(source, Iterable) and not isinstance(source, (str, bytes, Mapping)):
        return list(source)
    raise TypeError("请传入记录列表，或 Session + model/statement")


def _token_was_used(token_id: str, used_token_ids: Any) -> bool:
    if used_token_ids is None:
        return False
    fingerprint = booking_token_fingerprint(token_id)
    if callable(used_token_ids):
        checker: Callable[[str], Any] = used_token_ids
        return bool(checker(token_id) or checker(fingerprint))
    if isinstance(used_token_ids, Mapping):
        return bool(
            used_token_ids.get(token_id) or used_token_ids.get(fingerprint)
        )
    try:
        return token_id in used_token_ids or fingerprint in used_token_ids
    except TypeError as exc:
        raise TypeError(
            "used_token_ids 必须是集合、映射或查询函数"
        ) from exc
