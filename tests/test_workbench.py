from datetime import datetime, timedelta, timezone

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.db import Base, create_db_engine
from app.models import (
    Application, ApplicationStatus, Candidate, FollowUpStatus, FollowUpTask,
    Interview, InterviewStatus, Job, JobVersion, Offer, OfferStatus,
    OnboardingStatus, OnboardingTask,
)
from app.workbench import build_workbench_context
from app.services.scheduling import generate_candidate_slots


@pytest.fixture()
def session():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_application(session, status, number=1):
    job = Job(platform="test", source_job_id=f"job-{number}", title=f"岗位 {number}")
    version = JobVersion(job=job, version_number=1, version_hash=f"hash-{number}", jd_raw="JD")
    candidate = Candidate(name=f"候选人 {number}")
    application = Application(candidate=candidate, job=job, job_version=version, status=status)
    session.add(application)
    session.flush()
    return application


def row_for(application):
    return {
        "id": application.id, "candidate_id": application.candidate_id,
        "candidate_name": application.candidate.name, "job_title": application.job.title,
        "status": application.status.value, "status_label": application.status.value,
        "owner": "未分配", "updated_at": application.updated_at,
        "interview_time": None, "interview_id": None,
    }


def test_board_groups_active_applications_without_losing_processing_states(session):
    applications = [make_application(session, status, number) for number, status in enumerate([
        ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.PARSING,
        ApplicationStatus.INTERVIEW_PENDING, ApplicationStatus.FEEDBACK_PENDING,
        ApplicationStatus.OFFER_PENDING, ApplicationStatus.OFFER_ACCEPTED,
        ApplicationStatus.ONBOARDING, ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER_DECLINED, ApplicationStatus.JOINED,
    ], 1)]
    result = build_workbench_context(session, [row_for(app) for app in applications], {})["workbench"]
    assert [len(column["cards"]) for column in result["columns"]] == [2, 2, 1, 2]
    assert result["active_count"] == 7
    assert len(result["jobs"]) == 7
    assert result["columns"][0]["cards"][0]["href"].endswith(f"?application_id={applications[0].id}")
    assert not session.dirty


def test_today_schedule_uses_shanghai_day_and_excludes_cancelled(session):
    application = make_application(session, ApplicationStatus.INTERVIEW_CONFIRMED)
    # 生成器的北京时间写入 SQLite 后是 naive；事件与 now 仍使用 UTC。
    starts = [
        (datetime(2026, 10, 23, 23, 59), InterviewStatus.CONFIRMED),
        (datetime(2026, 10, 24, 0, 0), InterviewStatus.CONFIRMED),
        (datetime(2026, 10, 24, 9, 0), InterviewStatus.CANCELLED),
        (datetime(2026, 10, 24, 23, 59), InterviewStatus.COMPLETED),
        (datetime(2026, 10, 25, 0, 0), InterviewStatus.CONFIRMED),
    ]
    for number, (start, status) in enumerate(starts, 1):
        session.add(Interview(application=application, round=number, status=status,
            confirmed_start_at=start, confirmed_end_at=start + timedelta(minutes=45)))
    session.flush()
    result = build_workbench_context(session, [], {}, now=datetime(2026, 10, 23, 16, 15, tzinfo=timezone.utc))["workbench"]
    assert result["today"] == "2026-10-24"
    assert result["date_label"] == "10月24日 · 周六"
    assert result["interview_count"] == 2
    assert [item["time"] for item in result["interviews"]] == ["00:00", "23:59"]
    assert result["interviews"][0]["is_next"]
    assert result["interviews"][0]["timing_label"] == "当前时段"


def test_workbench_preserves_time_from_real_slot_generator_after_sqlite_reload(session):
    application = make_application(session, ApplicationStatus.INTERVIEW_CONFIRMED)
    slot = generate_candidate_slots(
        datetime(2026, 10, 26, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 26, 10, tzinfo=timezone.utc), limit=1,
    )[0]
    session.add(Interview(application=application, round=1, status=InterviewStatus.CONFIRMED,
        confirmed_start_at=slot["start_at"], confirmed_end_at=slot["end_at"]))
    session.commit()
    session.expire_all()
    result = build_workbench_context(session, [], {}, now=datetime(2026, 10, 26, 0, tzinfo=timezone.utc))["workbench"]
    assert result["interviews"][0]["time"] == "09:00"
    assert result["interview_count"] == 1


def test_every_nonterminal_application_status_has_exactly_one_board_stage(session):
    applications = [make_application(session, status, index) for index, status in enumerate(ApplicationStatus, 1)]
    result = build_workbench_context(session, [row_for(app) for app in applications], {})["workbench"]
    visible_ids = [card["id"] for column in result["columns"] for card in column["cards"]]
    terminal = {ApplicationStatus.REJECTED, ApplicationStatus.OFFER_DECLINED, ApplicationStatus.JOINED}
    expected_ids = [app.id for app in applications if app.status not in terminal]
    assert sorted(visible_ids) == expected_ids
    assert len(visible_ids) == len(set(visible_ids))


def test_compact_schedule_keeps_next_interview_visible(session):
    application = make_application(session, ApplicationStatus.INTERVIEW_CONFIRMED)
    for number, hour in enumerate([9, 10, 11, 12, 13, 14], 1):
        start = datetime(2026, 10, 24, hour)
        session.add(Interview(application=application, round=number,
            status=InterviewStatus.CONFIRMED, confirmed_start_at=start,
            confirmed_end_at=start + timedelta(minutes=30)))
    session.flush()
    result = build_workbench_context(session, [], {}, now=datetime(2026, 10, 24, 4, 45, tzinfo=timezone.utc))["workbench"]
    assert result["interview_count"] == 6
    assert len(result["interviews"]) == 3
    assert [item["time"] for item in result["interviews"]] == ["12:00", "13:00", "14:00"]
    assert next(item for item in result["interviews"] if item["is_next"])["time"] == "13:00"


def test_overdue_only_counts_open_tasks_past_their_deadline(session):
    now = datetime(2026, 10, 24, 1, tzinfo=timezone.utc)
    for number, (status, due) in enumerate([
        (FollowUpStatus.OPEN, now - timedelta(hours=1)),
        (FollowUpStatus.OPEN, now + timedelta(hours=1)),
        (FollowUpStatus.OPEN, None),
        (FollowUpStatus.RESOLVED, now - timedelta(days=1)),
    ], 1):
        session.add(FollowUpTask(target_type="application", target_id=number,
            rule_code="test", window_key=str(number), status=status, due_at=due))
    session.flush()
    result = build_workbench_context(session, [], {}, now=now)["workbench"]
    assert result["overdue_count"] == 1


def test_offer_and_onboarding_cards_use_actual_records(session):
    offer_app = make_application(session, ApplicationStatus.OFFER_PENDING, 1)
    onboarding_app = make_application(session, ApplicationStatus.ONBOARDING, 2)
    offers = [Offer(application=app, candidate_name=app.candidate.name, job_title=app.job.title,
        content="真实条款", status=status, report_time="2026-11-02 09:00") for app, status in [
            (offer_app, OfferStatus.PENDING_APPROVAL), (onboarding_app, OfferStatus.ACCEPTED)]]
    session.add_all(offers)
    session.flush()
    session.add_all([OnboardingTask(offer=offers[1], application_id=onboarding_app.id,
        title="资料确认", status=status) for status in [OnboardingStatus.OPEN, OnboardingStatus.DONE]])
    session.flush()
    result = build_workbench_context(session, [row_for(offer_app), row_for(onboarding_app)], {})["workbench"]
    offer_card = result["columns"][2]["cards"][0]
    assert offer_card["status_label"] == "待人工审批"
    assert offer_card["action_href"] == f"/offers?offer_id={offers[0].id}"
    assert result["reminders"][2]["count"] == 1
    assert result["columns"][3]["cards"][0]["note"] == "1 项入职事项待完成。"


def test_empty_workbench_renders_without_demo_counts(session):
    context = build_workbench_context(session, [], {}, now=datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert context["workbench"]["active_count"] == 0
    assert context["workbench"]["interviews"] == []
    assert all(step["count"] == 0 for step in context["workbench"]["flow"])
    environment = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    html = environment.get_template("workbench.html").render(**context, current_user={"username": "测试用户"}, csrf_token="test")
    assert "今日暂无已确认面试" in html
    assert "暂无进行中的候选人" in html
    assert "我的候选人" not in html
