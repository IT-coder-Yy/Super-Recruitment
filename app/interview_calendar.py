"""项目内面试月历；只读取已有排期，不调用外部服务。"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Application, Interview, InterviewStatus, Job
from app.workbench import appointment_time


def build_interview_calendar(
    session: Session, *, month: str | None = None, selected_date: str | None = None,
    recruitment_type: str = "all", now: datetime | None = None,
) -> dict:
    today = appointment_time(now or datetime.now(timezone.utc)).date()
    first = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    if not 2 <= first.year <= 9998:
        raise ValueError("无法显示该月份")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    previous_month = (first - timedelta(days=1)).replace(day=1)
    selected = date.fromisoformat(selected_date) if selected_date else (
        today if (today.year, today.month) == (first.year, first.month) else first
    )
    if not first <= selected < next_month:
        raise ValueError("所选日期不属于当前月份")

    # 排期在SQLite中保存北京时间钟面值，与首页今日面试保持相同口径。
    interviews = session.scalars(select(Interview).join(Interview.application).join(Application.job).options(
        joinedload(Interview.application).joinedload(Application.candidate),
        joinedload(Interview.application).joinedload(Application.job),
    ).where(
        Interview.status.in_([InterviewStatus.CONFIRMED, InterviewStatus.COMPLETED]),
        Interview.confirmed_start_at < datetime.combine(next_month, time.min),
        or_(Interview.confirmed_end_at > datetime.combine(first, time.min),
            Interview.confirmed_start_at >= datetime.combine(first, time.min)),
        True if recruitment_type == "all" else Job.recruitment_type == recruitment_type,
    ).order_by(Interview.confirmed_start_at, Interview.id)).all()
    events = defaultdict(list)
    for interview in interviews:
        start = appointment_time(interview.confirmed_start_at)
        end = appointment_time(interview.confirmed_end_at) if interview.confirmed_end_at else start
        event = {
            "id": interview.id,
            "name": interview.application.candidate.name or "待补充姓名",
            "job": interview.application.job.title,
            "round": interview.round,
            "time": start.strftime("%H:%M") + (f"—{end:%H:%M}" if end > start and end.date() == start.date() else ""),
            "period": f"{start:%m月%d日 %H:%M}—{end:%m月%d日 %H:%M}" if end.date() != start.date() else "",
            "status": "已完成" if interview.status == InterviewStatus.COMPLETED else "已确认",
            "href": f"/interviews?recruitment_type={interview.application.job.recruitment_type}#interview-{interview.id}",
        }
        day = max(first, start.date())
        last = min(next_month - timedelta(days=1), (end - timedelta(microseconds=1)).date() if end > start else start.date())
        while day <= last:
            events[day].append(event)
            day += timedelta(days=1)
    weeks = [[{
        "number": number,
        "date": first.replace(day=number).isoformat(),
        "count": len(events[first.replace(day=number)]),
        "today": first.replace(day=number) == today,
        "selected": first.replace(day=number) == selected,
    } if number else None for number in week]
        for week in calendar.Calendar(firstweekday=0).monthdayscalendar(first.year, first.month)]
    return {
        "month": first.strftime("%Y-%m"), "title": f"{first.year}年{first.month}月",
        "previous_month": previous_month.strftime("%Y-%m") if previous_month.year >= 2 else None,
        "next_month": next_month.strftime("%Y-%m") if next_month.year <= 9998 else None,
        "today": today.isoformat(), "today_month": today.strftime("%Y-%m"),
        "selected_date": selected.isoformat(),
        "selected_label": f"{selected.month}月{selected.day}日 · 周{'一二三四五六日'[selected.weekday()]}",
        "weeks": weeks, "events": events[selected], "month_count": len(interviews),
    }
