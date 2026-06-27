"""发送到在线模型前使用的本地脱敏工具。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-])([A-Z0-9._%+-]*)(@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d)[-\s]?(\d{4})[-\s]?(\d{4})(?!\d)"
)
ID_CARD_PATTERN = re.compile(
    r"(?<![0-9A-Z])(\d{6})(\d{8})(\d{3}[0-9X])(?![0-9A-Z])",
    re.IGNORECASE,
)
LEGACY_ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{6})(\d{5})(\d{4})(?!\d)")


def extract_contact_info(text: str | None) -> dict[str, str | None]:
    """在本地提取联系方式，供入库使用；提取结果不会发送给在线模型。"""

    source = text or ""
    phone_match = PHONE_PATTERN.search(source)
    email_match = EMAIL_PATTERN.search(source)
    id_match = ID_CARD_PATTERN.search(source) or LEGACY_ID_CARD_PATTERN.search(source)

    phone = None
    if phone_match:
        phone = "".join(phone_match.groups())

    email = email_match.group(0) if email_match else None
    id_card = id_match.group(0) if id_match else None
    return {"phone": phone, "email": email, "id_card": id_card}


def redact_text(text: str | None) -> str:
    """脱敏文本中的邮箱、身份证号和中国大陆手机号。"""

    if not text:
        return ""

    redacted = EMAIL_PATTERN.sub(
        lambda match: f"{match.group(1)}***{match.group(3)}",
        text,
    )
    redacted = ID_CARD_PATTERN.sub(
        lambda match: f"{match.group(1)}********{match.group(3)[-4:]}",
        redacted,
    )
    redacted = LEGACY_ID_CARD_PATTERN.sub(
        lambda match: f"{match.group(1)}*****{match.group(3)}",
        redacted,
    )
    redacted = PHONE_PATTERN.sub(
        lambda match: f"{match.group(1)}****{match.group(3)}",
        redacted,
    )
    return redacted


def redact_data(value: Any) -> Any:
    """递归脱敏可 JSON 序列化的数据，同时保持原容器结构。"""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_data(item) for item in value]
    return value
