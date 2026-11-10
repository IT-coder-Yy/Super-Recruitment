"""个人腾讯会议 MCP：复用已核对的 v1.1.0 协议，每次操作只发一个请求。"""
from __future__ import annotations

import hashlib
import json
import math
import platform
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from app.config import get_setting


ENDPOINT = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
VERSION = "v1.1.0"


class MeetingError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = True):
        super().__init__(message)
        self.uncertain = uncertain


class TencentMeetingAdapter:
    def __init__(self):
        self.token = get_setting("TENCENT_MEETING_TOKEN")
        if get_setting("MEETING_PROVIDER", "mock") != "tencent_mcp":
            raise MeetingError("真实会议尚未启用，请联系管理员", uncertain=False)
        if not self.token or not self.token.isascii() or any(c.isspace() for c in self.token):
            raise MeetingError("腾讯会议 Token 未配置或格式无效", uncertain=False)
        try:
            self.timeout = float(get_setting("TENCENT_MEETING_TIMEOUT_SECONDS", "30"))
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError
        except ValueError:
            raise MeetingError("腾讯会议超时配置必须为正数", uncertain=False) from None
        self.account_fingerprint = hashlib.sha256(self.token.encode()).hexdigest()

    def _call(self, name: str, arguments: dict) -> dict:
        arguments = {**arguments, "_client_info": {
            "os": platform.system(), "agent": "TalentFlow", "model": "none",
        }}
        try:
            with httpx.Client(transport=httpx.HTTPTransport(retries=0),
                              timeout=httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                              follow_redirects=False, trust_env=False) as client:
                response = client.post(ENDPOINT, headers={
                    "X-Tencent-Meeting-Token": self.token, "X-Skill-Version": VERSION,
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise MeetingError("未能连接腾讯会议，可由 HR 重试", uncertain=False) from None
        except httpx.HTTPError:
            raise MeetingError("腾讯会议请求结果未知，请先核实，勿重复建会") from None
        # 只把明确拒绝归为可重试；5xx/超时/异常格式可能发生在服务端已经建会之后。
        rejected = {400, 401, 403, 404, 422}
        if response.status_code != 200:
            raise MeetingError("腾讯会议请求未成功，请检查配置或核实结果",
                               uncertain=response.status_code not in rejected)
        try:
            envelope = response.json()
            if envelope.get("error") or envelope["result"].get("isError"):
                raise ValueError
            wrapped = json.loads(envelope["result"]["content"][0]["text"])
            if wrapped["status_code"] != 200:
                raise MeetingError("腾讯会议业务请求未成功，请检查配置或核实结果",
                                   uncertain=wrapped["status_code"] not in rejected)
            body = wrapped["body"]
            if name == "cancel_meeting" and body in (None, "", "{}", {}):
                return {"cancelled": True}
            body = json.loads(body) if isinstance(body, str) else body
            meetings = body["meeting_info_list"]
            if len(meetings) != 1 or not isinstance(meetings[0], dict):
                raise ValueError
            # 不返回或持久化 host_key、password、参与人等无关字段。
            result = {key: meetings[0].get(key) for key in (
                "meeting_id", "meeting_code", "join_url", "subject", "start_time", "end_time", "status",
            )}
            # 个人MCP实际返回中文状态（2026-11-07真实查询证据）；REST枚举保持兼容。
            result["status"] = {"待开始": "MEETING_STATE_INIT", "已取消": "MEETING_STATE_CANCELLED"}.get(result["status"], result["status"])
            return result
        except MeetingError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise MeetingError("腾讯会议返回无法确认，请由 HR 核实已有会议") from None

    def create_meeting(self, snapshot: dict) -> dict:
        return self._call("schedule_meeting", {
            **{key: snapshot[key] for key in ("subject", "start_time", "end_time")},
            "time_zone": "Asia/Shanghai", "meeting_type": 0,
            "auto_record_type": "none", "auto_asr": False,
        })

    def get_meeting(self, *, meeting_id: str | None = None, meeting_code: str = "") -> dict:
        value = meeting_id or meeting_code.replace(" ", "")
        if not value or not value.isascii() or not value.isdigit() or len(value) > 20:
            raise MeetingError("请填写正确的会议号", uncertain=False)
        if meeting_id:
            return self._call("get_meeting", {"meeting_id": value})
        return self._call("get_meeting_by_code", {"meeting_code": value})

    def update_meeting(self, meeting_id: str, snapshot: dict) -> dict:
        return self._call("update_meeting", {
            "meeting_id": meeting_id, "time_zone": "Asia/Shanghai",
            **{key: snapshot[key] for key in ("start_time", "end_time")},
        })

    def cancel_meeting(self, meeting_id: str) -> dict:
        return self._call("cancel_meeting", {"meeting_id": meeting_id})


def verified_meeting(result: dict, snapshot: dict, known_id: str | None = None) -> tuple[str, str]:
    """不以 HTTP 成功代替建会成功；绑定本次独有主题、时间与官方入会地址。"""
    try:
        meeting_id, url = str(result["meeting_id"] or ""), result["join_url"]
        parsed = urlsplit(url)
        if (not meeting_id.isascii() or not meeting_id.isdigit() or len(meeting_id) > 20
                or (known_id and meeting_id != known_id)
                or parsed.scheme != "https" or parsed.hostname != "meeting.tencent.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path.startswith("/dm/") or parsed.path.startswith("/dm/mock-")
                or not parsed.path.removeprefix("/dm/") or any(c.isspace() for c in url)
                or result["subject"] != snapshot["subject"]
                or result.get("status") in {"MEETING_STATE_CANCELLED", "MEETING_STATE_ENDED", "MEETING_STATE_RECYCLED"}):
            raise ValueError
        for field in ("start_time", "end_time"):
            actual = datetime.fromisoformat(result[field].replace("Z", "+00:00"))
            if actual.tzinfo is None or actual != datetime.fromisoformat(snapshot[field]):
                raise ValueError
        return meeting_id, url
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MeetingError("会议与本次预约不一致或链接无效，请核实后再关联") from None
