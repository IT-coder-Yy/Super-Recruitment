from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class FollowUpRule:
    code: str
    statuses: tuple[str, ...]
    timestamp_fields: tuple[str, ...]
    threshold: timedelta
    reason: str
    suggested_action: str
    message_template: str
    mode: str = "elapsed"
    repeat_interval: timedelta = timedelta(days=1)
    next_check_interval: timedelta = timedelta(hours=4)


DEFAULT_FOLLOWUP_RULES: tuple[FollowUpRule, ...] = (
    FollowUpRule(
        code="new_resume_pending",
        statuses=("captured", "received", "needs_review"),
        timestamp_fields=("applied_at", "captured_at", "created_at"),
        threshold=timedelta(hours=4),
        reason="新简历超过 4 小时未处理",
        suggested_action="提醒负责人完成简历初筛",
        message_template="候选人 {candidate_name} 的新简历已超过 4 小时未处理，请及时查看。",
    ),
    FollowUpRule(
        code="evaluation_review_pending",
        statuses=("evaluation_review", "evaluated", "evaluation_pending_review"),
        timestamp_fields=(
            "evaluation_completed_at",
            "evaluated_at",
            "status_changed_at",
            "updated_at",
        ),
        threshold=timedelta(hours=24),
        reason="AI 评估超过 24 小时未审核",
        suggested_action="提醒 HR 或面试官审核 AI 评估",
        message_template="候选人 {candidate_name} 的 AI 评估已等待审核超过 24 小时。",
    ),
    FollowUpRule(
        code="interview_invitation_unconfirmed",
        statuses=("scheduling", "interview_invited", "invitation_sent"),
        timestamp_fields=("invitation_sent_at", "status_changed_at", "updated_at"),
        threshold=timedelta(hours=24),
        reason="面试邀请超过 24 小时未确认",
        suggested_action="生成候选人跟进邮件草稿并请负责人确认",
        message_template="候选人 {candidate_name} 尚未确认面试邀请，建议检查送达状态并跟进。",
    ),
    FollowUpRule(
        code="interview_feedback_pending",
        statuses=("interviewed", "feedback_pending"),
        timestamp_fields=(
            "interview_end_at",
            "completed_at",
            "status_changed_at",
            "updated_at",
        ),
        threshold=timedelta(hours=12),
        reason="面试结束超过 12 小时仍无反馈",
        suggested_action="提醒面试官填写面试反馈",
        message_template="候选人 {candidate_name} 的面试反馈已逾期，请面试官尽快填写。",
    ),
    FollowUpRule(
        code="offer_confirmation_pending",
        statuses=("offer_sent", "offer_pending_confirmation"),
        timestamp_fields=("offer_sent_at", "status_changed_at", "updated_at"),
        threshold=timedelta(hours=48),
        reason="Offer 发出超过 48 小时未确认",
        suggested_action="生成 Offer 跟进建议并由 HR 确认",
        message_template="候选人 {candidate_name} 的 Offer 已超过 48 小时未确认，建议及时跟进。",
    ),
    FollowUpRule(
        code="onboarding_approaching",
        statuses=("offer_accepted", "onboarding"),
        timestamp_fields=("onboarding_date", "join_date", "start_date"),
        threshold=timedelta(days=3),
        reason="候选人将在 3 天内入职",
        suggested_action="提醒 HR 准备入职事项",
        message_template="候选人 {candidate_name} 将在 3 天内入职，请确认入职材料与账号准备情况。",
        mode="upcoming",
        repeat_interval=timedelta(days=1),
        next_check_interval=timedelta(days=1),
    ),
)


def scan_followups(
    records_or_session: Any,
    *,
    rules: Sequence[FollowUpRule | Mapping[str, Any]] = DEFAULT_FOLLOWUP_RULES,
    now: datetime | str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    existing_keys: Iterable[str] | Mapping[str, Any] | None = None,
    model: Any = None,
    statement: Any = None,
    scalars: bool = True,
    target_type: str = "application",
    target_id_fields: Sequence[str] = ("application_id", "id"),
) -> list[dict[str, Any]]:
    """扫描普通记录或 SQLAlchemy Session，返回应创建的超时跟进任务。

    本函数不写数据库。调用方应对返回的 ``dedupe_key`` 建唯一索引，
    以保证同一对象、规则和时间窗口只创建一条任务。
    """

    tz = ZoneInfo(timezone_name)
    current = (
        _coerce_datetime(now, tz)
        if now is not None
        else datetime.now(tz)
    )
    records = _load_records(
        records_or_session, model=model, statement=statement, scalars=scalars
    )
    known_keys = _normalize_existing_keys(existing_keys)
    normalized_rules = tuple(_normalize_rule(rule) for rule in rules)
    tasks: list[dict[str, Any]] = []
    generated_keys: set[str] = set()

    for record in records:
        status = str(_get_value(record, "status", "")).strip().lower()
        target_id = _first_present_value(record, target_id_fields)
        if target_id is None:
            continue
        for rule in normalized_rules:
            if rule.statuses and status not in {
                value.lower() for value in rule.statuses
            }:
                continue
            anchor = _first_datetime(record, rule.timestamp_fields, tz)
            if anchor is None:
                continue
            due_at, overdue_seconds = _evaluate_rule(rule, anchor, current)
            if due_at is None:
                continue
            window_start = _window_start(rule, due_at, current)
            dedupe_key = (
                f"{target_type}:{target_id}:{rule.code}:"
                f"{window_start.astimezone(timezone.utc).isoformat()}"
            )
            if dedupe_key in known_keys or dedupe_key in generated_keys:
                continue

            candidate_name = _candidate_name(record)
            owner = _first_present_value(
                record,
                ("owner_name", "owner", "owner_id", "interviewer_name", "interviewer_id"),
            )
            task = {
                "target_type": target_type,
                "target_id": target_id,
                "rule_code": rule.code,
                "status": "pending",
                "owner": owner,
                "reason": rule.reason,
                "suggested_action": rule.suggested_action,
                "message_draft": _render_message(
                    rule.message_template,
                    candidate_name=candidate_name,
                    owner=owner,
                    target_id=target_id,
                ),
                "source_status": status,
                "source_timestamp": anchor,
                "due_at": due_at,
                "triggered_at": current,
                "overdue_seconds": overdue_seconds,
                "next_check_at": current + rule.next_check_interval,
                "dedupe_key": dedupe_key,
            }
            tasks.append(task)
            generated_keys.add(dedupe_key)

    return sorted(
        tasks,
        key=lambda item: (
            item["due_at"],
            str(item["target_type"]),
            str(item["target_id"]),
            str(item["rule_code"]),
        ),
    )


def followup_dedupe_keys(tasks: Iterable[Any]) -> set[str]:
    """从已有任务记录提取去重键。"""

    keys = set()
    for task in tasks:
        value = _get_value(task, "dedupe_key")
        if value:
            keys.add(str(value))
    return keys


def _evaluate_rule(
    rule: FollowUpRule, anchor: datetime, current: datetime
) -> tuple[datetime | None, int]:
    if rule.mode == "elapsed":
        due_at = anchor + rule.threshold
        if current < due_at:
            return None, 0
        return due_at, max(0, int((current - due_at).total_seconds()))
    if rule.mode == "upcoming":
        if anchor < current or anchor > current + rule.threshold:
            return None, 0
        return anchor, 0
    raise ValueError(f"未知跟进规则模式：{rule.mode}")


def _window_start(
    rule: FollowUpRule, due_at: datetime, current: datetime
) -> datetime:
    interval_seconds = int(rule.repeat_interval.total_seconds())
    if interval_seconds <= 0:
        return due_at
    if rule.mode == "upcoming":
        elapsed = max(0, int((current - (due_at - rule.threshold)).total_seconds()))
        base = due_at - rule.threshold
    else:
        elapsed = max(0, int((current - due_at).total_seconds()))
        base = due_at
    return base + timedelta(seconds=(elapsed // interval_seconds) * interval_seconds)


def _normalize_rule(rule: FollowUpRule | Mapping[str, Any]) -> FollowUpRule:
    if isinstance(rule, FollowUpRule):
        return rule
    if not isinstance(rule, Mapping):
        raise TypeError("跟进规则必须是 FollowUpRule 或映射")
    values = dict(rule)
    for field in ("threshold", "repeat_interval", "next_check_interval"):
        if field in values and isinstance(values[field], (int, float)):
            values[field] = timedelta(seconds=float(values[field]))
    values["statuses"] = tuple(values.get("statuses", ()))
    values["timestamp_fields"] = tuple(values.get("timestamp_fields", ()))
    return FollowUpRule(**values)


def _coerce_datetime(value: datetime | str, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{value}") from exc
    else:
        raise TypeError("时间必须是 datetime 或 ISO 8601 字符串")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _first_datetime(
    record: Any, fields: Sequence[str], tz: ZoneInfo
) -> datetime | None:
    for field in fields:
        value = _get_path_value(record, field)
        if value is not None and value != "":
            return _coerce_datetime(value, tz)
    return None


def _candidate_name(record: Any) -> str:
    value = _first_present_value(
        record,
        ("candidate_name", "candidate.name", "name"),
    )
    return str(value) if value not in (None, "") else "待确认候选人"


def _render_message(template: str, **values: Any) -> str:
    safe_values = {
        key: ("" if value is None else str(value)) for key, value in values.items()
    }
    try:
        return template.format_map(_SafeFormatDict(safe_values))
    except (ValueError, KeyError):
        return template


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _first_present_value(record: Any, fields: Sequence[str]) -> Any:
    for field in fields:
        value = _get_path_value(record, field)
        if value is not None and value != "":
            return value
    return None


def _get_path_value(record: Any, path: str, default: Any = None) -> Any:
    current = record
    for part in path.split("."):
        current = _get_value(current, part, default)
        if current is default:
            return default
    return current


def _get_value(record: Any, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    mapping = getattr(record, "_mapping", None)
    if mapping is not None:
        return mapping.get(field, default)
    return getattr(record, field, default)


def _normalize_existing_keys(
    values: Iterable[str] | Mapping[str, Any] | None,
) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, Mapping):
        return {str(key) for key, enabled in values.items() if enabled}
    return {str(value) for value in values}


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
