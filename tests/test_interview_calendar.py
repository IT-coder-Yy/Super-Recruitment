from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import Base, create_db_engine
from app.interview_calendar import build_interview_calendar
from app.models import Application, ApplicationStatus, Candidate, Interview, InterviewStatus, Job, JobVersion


@pytest.fixture()
def session():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def add_interview(session, start, *, minutes=45, status=InterviewStatus.CONFIRMED, kind="autumn", name="测试候选人"):
    job = Job(platform="test", source_job_id=f"calendar-{start.isoformat()}-{kind}-{status.value}", title="测试岗位", recruitment_type=kind)
    version = JobVersion(job=job, version_number=1, version_hash=f"{start}-{name}", jd_raw="JD")
    application = Application(candidate=Candidate(name=name), job=job, job_version=version,
                              status=ApplicationStatus.INTERVIEW_CONFIRMED)
    interview = Interview(application=application, status=status, confirmed_start_at=start,
                          confirmed_end_at=start + timedelta(minutes=minutes), round=1)
    session.add(interview)
    session.flush()
    return interview


def test_shanghai_today_filters_status_and_recruitment_type(session):
    start = datetime(2026, 11, 1, 0, 10)
    first = add_interview(session, start)
    add_interview(session, start + timedelta(hours=1), kind="internship")
    add_interview(session, start + timedelta(hours=2), status=InterviewStatus.COMPLETED)
    add_interview(session, start + timedelta(hours=3), status=InterviewStatus.CANCELLED)
    add_interview(session, start + timedelta(hours=4), status=InterviewStatus.INVITED)
    result = build_interview_calendar(session, now=datetime(2026, 10, 31, 16, 5, tzinfo=timezone.utc))
    assert result["today"] == result["selected_date"] == "2026-11-01"
    assert result["month_count"] == len(result["events"]) == 3
    assert result["events"][0]["time"] == "00:10—00:55"
    assert result["events"][0]["href"].endswith(f"#interview-{first.id}")
    filtered = build_interview_calendar(session, month="2026-11", recruitment_type="internship")
    assert filtered["month_count"] == 1
    assert build_interview_calendar(session, month="2026-11", selected_date="2026-11-02")["events"] == []
    assert not session.dirty


def test_cross_midnight_month_boundary_and_exclusive_end(session):
    crossing = add_interview(session, datetime(2026, 10, 31, 23, 30), minutes=90)
    add_interview(session, datetime(2026, 10, 31, 23), minutes=60)
    add_interview(session, datetime(2026, 11, 1, 23, 30), minutes=60)
    first = build_interview_calendar(session, month="2026-11", selected_date="2026-11-01")
    second = build_interview_calendar(session, month="2026-11", selected_date="2026-11-02")
    assert first["month_count"] == 2
    assert len(first["events"]) == 2 and len(second["events"]) == 1
    assert first["events"][0]["id"] == crossing.id
    assert first["events"][0]["period"] == "10月31日 23:30—11月01日 01:00"


def test_leap_month_and_year_navigation(session):
    february = build_interview_calendar(session, month="2028-02")
    assert sum(day is not None for week in february["weeks"] for day in week) == 29
    december = build_interview_calendar(session, month="2026-12", selected_date="2026-12-31")
    assert december["next_month"] == "2027-01"
    assert build_interview_calendar(session, month="2027-01")["previous_month"] == "2026-12"


@pytest.mark.parametrize("query", [
    "calendar_month=2026-13", "calendar_month=not-a-month",
    "calendar_month=2026-11&calendar_date=2026-12-01", "calendar_month=2026-02&calendar_date=2026-02-30",
])
def test_invalid_calendar_query_has_clear_400(session, query):
    from app.main import _calendar_data
    request = Request({"type": "http", "path": "/api/dashboard/summary", "headers": [], "session": {}, "query_string": query.encode()})
    with pytest.raises(HTTPException) as error:
        _calendar_data(request, session)
    assert error.value.status_code == 400


def test_template_and_refresh_escape_names_and_preserve_scope(session):
    from app.main import dashboard_summary, templates
    add_interview(session, datetime(2026, 11, 1, 10), kind="internship", name='<img src=x onerror="alert(1)">')
    calendar = build_interview_calendar(session, month="2026-11", selected_date="2026-11-01", recruitment_type="internship")
    html = templates.env.get_template("interview_calendar.html").render(calendar=calendar, recruitment_type="internship")
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert 'recruitment_type=internship&amp;calendar_month=2026-12' in html
    request = Request({"type": "http", "path": "/api/dashboard/summary", "headers": [], "session": {}, "query_string": b"recruitment_type=internship&calendar_month=2026-11&calendar_date=2026-11-01"})
    response = dashboard_summary(request, session)
    assert response["calendar_html"] == html
