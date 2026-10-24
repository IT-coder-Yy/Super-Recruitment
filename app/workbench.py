"""Read-only presentation data for the recruitment workbench."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Application, FollowUpStatus, FollowUpTask, Interview, InterviewStatus,
    Offer, OfferStatus, OnboardingStatus, OnboardingTask,
)
from app.services.scheduling import DEFAULT_TIMEZONE


STAGES = (
    ("review", "待复核", "初筛、资料确认与评估审核", {
        "captured", "parsing", "needs_review", "parsed", "deduplicated",
        "evaluating", "evaluation_review", "on_hold",
    }),
    ("interview", "面试", "排期、面试与反馈", {
        "interview_pending", "scheduling", "interview_confirmed", "interviewed",
        "feedback_pending", "next_round",
    }),
    ("offer", "Offer", "方案审批与候选人确认", {"offer_pending", "offer_sent"}),
    ("onboarding", "入职", "入职前的资料与事项", {"offer_accepted", "onboarding"}),
)

OFFER_LABELS = {
    "draft": "Offer 草稿", "pending_approval": "待人工审批", "approved": "已审批，待发放",
    "sent": "等待候选人确认", "accepted": "Offer 已接受", "declined": "Offer 已拒绝",
    "expired": "Offer 已过期",
}


def _utc(value: datetime) -> datetime:
    # SQLite drops timezone information from the project's UTC timestamps.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def appointment_time(value: datetime) -> datetime:
    # 排期生成器写入北京时间；SQLite 去掉时区但保留这个本地钟面时间。
    zone = ZoneInfo(DEFAULT_TIMEZONE)
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def build_workbench_context(
    session: Session,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _utc(now or datetime.now(timezone.utc))
    local_zone = ZoneInfo(DEFAULT_TIMEZONE)
    today = now.astimezone(local_zone).date()
    day_start = datetime.combine(today, time.min, tzinfo=local_zone)
    day_end = day_start + timedelta(days=1)

    job_ids = dict(session.execute(select(Application.id, Application.job_id)).all())
    offers = session.scalars(select(Offer).order_by(Offer.created_at.desc(), Offer.id.desc())).all()
    latest_offers: dict[int, Offer] = {}
    for offer in offers:
        latest_offers.setdefault(offer.application_id, offer)
    onboarding = Counter(session.scalars(
        select(OnboardingTask.application_id).where(OnboardingTask.status == OnboardingStatus.OPEN)
    ).all())
    overdue_count = session.scalar(select(func.count(FollowUpTask.id)).where(
        FollowUpTask.status == FollowUpStatus.OPEN,
        FollowUpTask.due_at <= now,
    )) or 0

    columns = []
    jobs: dict[int, str] = {}
    for stage_key, label, description, statuses in STAGES:
        cards = []
        for row in rows:
            if row["status"] not in statuses:
                continue
            card = dict(row)
            card["job_id"] = job_ids[row["id"]]
            card["stage"] = stage_key
            card["href"] = f"/candidates/{row['candidate_id']}?application_id={row['id']}"
            card["action"] = "查看资料"
            card["action_href"] = card["href"]
            card["tone"] = "neutral"
            card["note"] = ""
            card["facts"] = []
            card["updated_label"] = (
                _utc(row["updated_at"]).astimezone(local_zone).strftime("%m月%d日")
                if row.get("updated_at") else ""
            )
            if stage_key == "review":
                card["note"] = row.get("evaluation_reason") or {
                    "captured": "简历已采集，等待解析与评估。",
                    "parsing": "简历正在解析，请稍后查看结果。",
                    "needs_review": "资料需要人工确认，请检查原始证据。",
                    "parsed": "简历已解析，可继续检查资料并评估。",
                    "deduplicated": "已完成查重，可继续评估。",
                    "evaluating": "正在生成辅助评估，请稍后查看结果。",
                    "evaluation_review": "辅助评估已完成，等待人工复核。",
                    "on_hold": "当前投递已暂缓，可查看已有决策记录。",
                }[row["status"]]
                if row["status"] in {"needs_review", "evaluation_review"}:
                    card["tone"] = "warning"
                    card["action"] = "复核证据"
                if row.get("missing_information"):
                    card["facts"].append(("待确认", "；".join(row["missing_information"])))
            elif stage_key == "interview":
                card["tone"] = "info"
                if row.get("interview_id"):
                    card["action_href"] = f"/interviews#interview-{row['interview_id']}"
                    card["action"] = "补充反馈" if row["status"] == "feedback_pending" else "查看面试"
                else:
                    card["action"] = "安排面试"
                if row.get("interview_time"):
                    card["facts"].append(("面试时间", appointment_time(row["interview_time"]).strftime("%m月%d日 %H:%M")))
                card["facts"].append(("负责人", row.get("owner") or "未分配"))
                card["note"] = {
                    "interview_pending": "等待安排面试时段。", "scheduling": "正在协调面试时间。",
                    "interview_confirmed": "面试时间已确认。", "interviewed": "面试已结束，请查看记录。",
                    "feedback_pending": "等待面试官补充面试结论。", "next_round": "等待安排下一轮面试。",
                }[row["status"]]
            elif stage_key == "offer":
                offer = latest_offers.get(row["id"])
                if offer:
                    card["status_label"] = OFFER_LABELS[offer.status.value]
                    card["action_href"] = f"/offers?offer_id={offer.id}"
                    card["facts"].append(("预计报到", offer.report_time or "待确认"))
                    card["note"] = "方案经人工审批后发放。" if offer.status in {OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL} else "查看 Offer 内容与候选人回复。"
                else:
                    card["note"] = "待准备 Offer 方案，可查看候选人的面试结论。"
                card["action"] = "查看方案" if offer else "查看候选人"
                card["tone"] = "warning"
            else:
                card["note"] = f"{onboarding[row['id']]} 项入职事项待完成。" if onboarding[row["id"]] else "当前没有未完成的入职事项。"
                card["action"] = "入职跟进"
                card["action_href"] = "/onboarding"
                card["facts"].append(("负责人", row.get("owner") or "未分配"))
                offer = latest_offers.get(row["id"])
                if offer and offer.report_time:
                    card["facts"].append(("预计报到", offer.report_time))
            jobs[card["job_id"]] = row["job_title"]
            cards.append(card)
        columns.append({"key": stage_key, "label": label, "description": description, "cards": cards})

    today_interviews = session.scalars(select(Interview).options(
        joinedload(Interview.application).joinedload(Application.candidate),
        joinedload(Interview.application).joinedload(Application.job),
    ).where(
        Interview.status.in_([InterviewStatus.CONFIRMED, InterviewStatus.COMPLETED]),
        Interview.confirmed_start_at >= day_start,
        Interview.confirmed_start_at < day_end,
    ).order_by(Interview.confirmed_start_at, Interview.id)).all()
    next_interview = next((item for item in today_interviews
        if item.status == InterviewStatus.CONFIRMED and
        appointment_time(item.confirmed_end_at or item.confirmed_start_at) >= now), None)
    # Keep the next appointment visible even after the first three have passed.
    start_index = today_interviews.index(next_interview) if next_interview else 0
    visible_interviews = today_interviews[start_index:start_index + 3]
    if len(visible_interviews) < 3 and start_index:
        visible_interviews = today_interviews[max(0, len(today_interviews) - 3):]
    interviews = [{
        "id": item.id,
        "time": appointment_time(item.confirmed_start_at).strftime("%H:%M"),
        "datetime": appointment_time(item.confirmed_start_at).isoformat(),
        "name": item.application.candidate.name or "待补充姓名",
        "job": item.application.job.title,
        "round": item.round,
        "is_next": item is next_interview,
        "timing_label": "当前时段" if appointment_time(item.confirmed_start_at) <= now else "下一场",
        "is_completed": item.status == InterviewStatus.COMPLETED,
        "href": f"/interviews#interview-{item.id}",
    } for item in visible_interviews]

    flow = [{"key": key, "label": label, "icon": icon, "count": summary.get("funnel", summary).get(key, 0), "href": href}
        for key, label, icon, href in [
            ("applications", "简历", "file", "/candidates"),
            ("screened", "初筛", "checklist", "/evaluations"),
            ("interviews", "面试", "calendar", "/interviews"),
            ("offers", "Offer", "audit", "/offers"),
            ("onboarded", "入职", "users", "/onboarding"),
        ]]
    return {"workbench": {
        "today": today.isoformat(),
        "date_label": f"{today.month}月{today.day}日 · 周{'一二三四五六日'[today.weekday()]}",
        "timezone": DEFAULT_TIMEZONE,
        "flow": flow,
        "columns": columns,
        "active_count": sum(len(column["cards"]) for column in columns),
        "jobs": [{"id": key, "title": title} for key, title in sorted(jobs.items(), key=lambda item: item[1])],
        "interviews": interviews,
        "interview_count": len(today_interviews),
        "overdue_count": overdue_count,
        "reminders": [
            {"label": "待复核", "description": "查看资料与评估证据", "count": summary.get("pending_review", 0), "href": "/candidates", "icon": "file"},
            {"label": "待反馈", "description": "补充面试结论", "count": summary.get("pending_feedback", 0), "href": "/interviews", "icon": "calendar"},
            {"label": "待审批", "description": "查看 Offer 方案", "count": sum(offer.status == OfferStatus.PENDING_APPROVAL for offer in offers), "href": "/offers", "icon": "audit"},
        ],
    }}
