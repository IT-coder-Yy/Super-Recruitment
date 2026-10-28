"""实习与秋招共用流程的边界回归，全部使用隔离库和虚构资料。"""
import csv
from datetime import timedelta
from io import BytesIO, StringIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from tests.test_demo_contracts import env, application, post
from app.models import Application, ApplicationStatus, AuditLog, ExamDispatch, Interview, Job, Offer, OfferStatus, OnboardingTask, utc_now
from app.schemas import JobCreate
from app.services import exams
from app.services.workflow import create_application, get_or_create_job_version


def paired_applications(session):
    autumn = application(session)
    job, version, _ = get_or_create_job_version(session, JobCreate(
        platform="test", source_job_id="internship-pair", title="虚构实习岗位",
        recruitment_type="internship", jd_raw="Python FastAPI",
    ))
    internship, _ = create_application(session, {
        "candidate_id": autumn.candidate_id, "job_id": job.id, "job_version_id": version.id,
    })
    internship.status = ApplicationStatus.EVALUATION_REVIEW
    session.commit()
    return autumn, internship


def test_internship_type_is_explicit_inherited_and_locked(env):
    client, headers, factory = env
    raw = "姓名：虚构实习人\n手机号：13900000991\n邮箱：intern@example.test\n项目：Python FastAPI 后端 API"
    files = {"files": ("internship.txt", raw.encode())}
    assert client.post("/api/import/files", headers=headers, files=files).status_code == 422
    assert client.post("/api/import/files", headers=headers, data={"recruitment_type": "unknown"}, files=files).status_code == 422
    with factory() as session:
        autumn, internship = paired_applications(session)
        assert autumn.candidate_id == internship.candidate_id
        assert autumn.job.recruitment_type == "autumn"
        values = {"platform": "test", "source_job_id": "internship-pair", "title": "不能修改的名称",
                  "recruitment_type": "autumn", "jd_raw": "不同 JD"}
        with pytest.raises(ValueError):
            get_or_create_job_version(session, values)
        session.rollback()
        assert session.get(Job, internship.job_id).recruitment_type == "internship"
        assert session.get(Job, internship.job_id).title == "虚构实习岗位"
    uploaded = client.post("/api/import/files", headers=headers, data={"job_id": internship.job_id}, files=files)
    assert uploaded.status_code == 200, uploaded.text
    task_id = uploaded.json()["task"]["id"]
    post(client, headers, f"/api/import/tasks/{task_id}/parse")
    imported = post(client, headers, f"/api/import/tasks/{task_id}/confirm")
    with factory() as session:
        assert session.get(Application, imported["application_id"]).job.recruitment_type == "internship"


def test_internship_screening_skips_are_atomic_and_do_not_change_autumn(env):
    client, headers, factory = env
    with factory() as session:
        autumn, internship = paired_applications(session)
    for record, wrong in [(autumn, "interview_pending"), (internship, "assessment_pending")]:
        assert client.post(f"/api/applications/{record.id}/decision", headers=headers, data={"status": wrong}).status_code == 409
    post(client, headers, f"/api/applications/{internship.id}/decision", status="on_hold", reason="待核实")
    result = post(client, headers, f"/api/applications/{internship.id}/decision", status="interview_pending", reason="HR 初筛通过")
    assert result["status"] == "interview_pending"
    with factory() as session:
        record = session.get(Application, internship.id)
        assert record.extra_data["workflow_revision"] == 2
        assert {kind: item["result"] for kind, item in record.extra_data["exams"].items()} == {"assessment": "skipped", "written": "skipped"}
        assert all("实习" in item["note"] for item in record.extra_data["exams"].values())
        actions = session.scalars(select(AuditLog.action).where(AuditLog.entity_type == "application", AuditLog.entity_id == internship.id)).all()
        assert actions.count("exam.assessment_decided") == actions.count("exam.written_decided") == 1
        assert session.scalar(select(func.count(ExamDispatch.id))) == 0
        assert session.get(Application, autumn.id).status == ApplicationStatus.EVALUATION_REVIEW
    assert client.post(f"/api/applications/{internship.id}/decision", headers=headers, data={"status": "interview_pending"}).status_code == 409
    assert post(client, headers, f"/api/applications/{autumn.id}/decision", status="assessment_pending")["status"] == "assessment_pending"
    with factory() as session:
        assert not (session.get(Application, autumn.id).extra_data or {}).get("exams")
        assert session.get(Application, internship.id).extra_data["workflow_revision"] == 2


def test_internship_filters_views_statistics_and_exports(env):
    client, headers, factory = env
    with factory() as session:
        autumn, internship = paired_applications(session)
    review = client.get(f"/evaluations?recruitment_type=internship&application_id={internship.id}")
    assert review.status_code == 200
    assert 'value="interview_pending"' in review.text and '>进入面试</button>' in review.text
    assert 'value="assessment_pending"' not in review.text
    post(client, headers, f"/api/applications/{internship.id}/decision", status="interview_pending")
    rows = client.get("/api/candidates?recruitment_type=internship").json()
    assert [row["id"] for row in rows] == [internship.id]
    assert rows[0]["recruitment_type"] == "internship"
    page = client.get("/?recruitment_type=internship")
    assert 'data-workbench-column="assessment"' not in page.text
    assert 'data-workbench-column="written"' not in page.text
    assert 'data-workbench-column="interview"' in page.text
    detail = client.get(f"/candidates/{internship.candidate_id}?application_id={internship.id}")
    assert "跳过" in detail.text
    summary = client.get("/api/dashboard/summary?recruitment_type=internship").json()
    assert summary["applications"] == summary["total_candidates"] == 1
    assert summary["exam_counts"]["assessment"]["skipped"] == summary["exam_counts"]["written"]["skipped"] == 1
    combined = client.get("/api/dashboard/summary?recruitment_type=all").json()
    assert combined["applications"] == 2 and combined["total_candidates"] == 1
    assert set(combined["by_recruitment_type"]) == {"autumn", "internship"}
    for kind in ("autumn", "internship"):
        assert combined["by_recruitment_type"][kind]["applications"] == 1
    exported = client.get("/api/exports/recruitment.csv?recruitment_type=internship")
    data = list(csv.DictReader(StringIO(exported.content.decode("utf-8-sig"))))
    assert len(data) == 1 and data[0]["招聘类型"] == "实习"
    assert data[0]["测评结论"] == data[0]["笔试结论"] == "skipped"
    workbook = load_workbook(BytesIO(client.get("/api/exports/recruitment.xlsx?recruitment_type=autumn").content))
    rows = list(workbook.active.values)
    assert len(rows) == 2 and rows[1][rows[0].index("招聘类型")] == "秋招"


def test_internship_partial_skip_failure_rolls_back_screening(env, monkeypatch):
    client, headers, factory = env
    with factory() as session:
        _, internship = paired_applications(session)
    original = exams.decide_exam
    def fail_written(*args, **kwargs):
        if kwargs["kind"] == "written":
            raise ValueError("虚构笔试记录故障")
        return original(*args, **kwargs)
    monkeypatch.setattr(exams, "decide_exam", fail_written)
    response = client.post(f"/api/applications/{internship.id}/decision", headers=headers, data={"status": "interview_pending"})
    assert response.status_code == 409
    with factory() as session:
        record = session.get(Application, internship.id)
        assert record.status == ApplicationStatus.EVALUATION_REVIEW
        assert not (record.extra_data or {}).get("exams")
        assert session.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.entity_type == "application", AuditLog.entity_id == internship.id,
            AuditLog.action.in_(["exam.assessment_decided", "exam.written_decided"]),
        )) == 0


def test_internship_page_filter_survives_api_polling_and_blocks_other_exam_details(env):
    client, headers, factory = env
    with factory() as session:
        autumn, internship = paired_applications(session)
    post(client, headers, f"/api/applications/{autumn.id}/decision", status="assessment_pending")
    paper = post(client, headers, "/api/exams/resources", job_id=autumn.job_id, kind="assessment",
        title="秋招专属试卷", url="https://exam.example.test/autumn", request_key="internship-scope")
    resource_id = paper["resource_id"]
    post(client, headers, f"/api/applications/{autumn.id}/exams/send", resource_id=resource_id, candidate_revision=1)
    assert client.get("/candidates?recruitment_type=internship").status_code == 200
    assert client.get("/api/dashboard/summary?recruitment_type=autumn").json()["applications"] == 1
    page = client.get("/candidates")
    assert "虚构实习岗位" in page.text and "虚构后端岗位" not in page.text
    for suffix in ("csv", "xlsx"):
        assert f'href="/api/exports/recruitment.{suffix}?recruitment_type=internship"' in page.text
    dashboard = client.get("/dashboard")
    assert 'href="/api/exports/recruitment.xlsx?recruitment_type=internship"' in dashboard.text
    exams_page = client.get(f"/exams?recruitment_type=internship&application_id={autumn.id}&resource_id={resource_id}")
    assert exams_page.status_code == 200
    assert "虚构后端岗位" not in exams_page.text and "秋招专属试卷" not in exams_page.text
    assert f'action="/api/applications/{autumn.id}/exams/decision"' not in exams_page.text
    assert f'action="/api/applications/{autumn.id}/exams/send"' not in exams_page.text
    assert "暂无发放记录" in exams_page.text


def test_internship_demo_reimport_preserves_progress_and_legacy_type(env):
    client, headers, factory = env
    post(client, headers, "/api/import/mock")
    rows = client.get("/api/candidates?recruitment_type=internship").json()
    assert len(rows) == 1
    post(client, headers, f"/api/applications/{rows[0]['id']}/decision", status="interview_pending")
    post(client, headers, "/api/import/mock")
    with factory() as session:
        assert session.scalar(select(func.count(Job.id)).where(Job.recruitment_type == "internship")) == 1
        assert session.get(Application, rows[0]["id"]).status == ApplicationStatus.INTERVIEW_PENDING
        old_jobs = session.scalars(select(Job).where(Job.source_job_id != "mock-internship-backend-001")).all()
        assert old_jobs and all(job.recruitment_type == "autumn" for job in old_jobs)


def test_internship_two_interviews_offer_approval_and_onboarding(env):
    client, headers, factory = env
    uploaded = client.post("/api/import/files", headers=headers,
        data={"job_title": "虚构实习工程师", "jd_text": "Python FastAPI", "recruitment_type": "internship"},
        files={"files": ("intern.txt", "姓名：虚构流程人\n手机号：13900000992\n邮箱：flow@example.test\n项目：Python FastAPI API".encode())})
    assert uploaded.status_code == 200, uploaded.text
    task_id = uploaded.json()["task"]["id"]
    post(client, headers, f"/api/import/tasks/{task_id}/parse")
    imported = post(client, headers, f"/api/import/tasks/{task_id}/confirm", start_evaluation="true")
    app_id = imported["application_id"]
    assert imported["evaluation"] and not imported["evaluation_error"]
    post(client, headers, f"/api/applications/{app_id}/decision", status="interview_pending", reason="HR 通过")
    for action in ("next_round", "offer"):
        booking = post(client, headers, f"/api/applications/{app_id}/schedule")
        assert client.post(booking["booking_url"] + "/confirm", data={"slot_id": booking["slots"][0]["id"]}).status_code == 200
        post(client, headers, f"/api/interviews/{booking['interview_id']}/feedback", next_action=action, result="通过", comment="虚构面试证据")
    with factory() as session:
        assert session.scalars(select(Interview.round).where(Interview.application_id == app_id).order_by(Interview.round)).all() == [1, 2]
        offer = session.scalar(select(Offer).where(Offer.application_id == app_id))
        assert offer.status == OfferStatus.PENDING_APPROVAL
        offer_id = offer.id
    assert client.post(f"/api/offers/{offer_id}/send", headers=headers).status_code == 422
    post(client, headers, f"/api/offers/{offer_id}/terms", version=0, company_name="虚构公司", job_title="虚构实习工程师",
        salary="300 元/天", subsidy="无", office_location="虚构办公区", report_location="虚构前台",
        report_time=utc_now().date().isoformat(), expires_at=(utc_now() + timedelta(days=3)).isoformat())
    post(client, headers, f"/api/offers/{offer_id}/approve", version=1)
    post(client, headers, f"/api/offers/{offer_id}/send")
    with factory() as session:
        token = session.get(Offer, offer_id).candidate_token
    assert client.post(f"/offer/{token}/respond", data={"accepted": "true"}).status_code == 200
    post(client, headers, f"/api/applications/{app_id}/onboarding", action="update", version=0,
         expected_date=utc_now().date().isoformat(), location="虚构前台")
    with factory() as session:
        task_ids = session.scalars(select(OnboardingTask.id).where(OnboardingTask.application_id == app_id)).all()
    for task in task_ids:
        post(client, headers, f"/api/onboarding/{task}/complete")
    post(client, headers, f"/api/applications/{app_id}/onboarding", action="join", version=1,
         actual_date=utc_now().date().isoformat(), note="HR 已确认到岗")
    with factory() as session:
        assert session.get(Application, app_id).status == ApplicationStatus.JOINED
    assert len(client.get("/api/offers?recruitment_type=internship").json()) == 1
    assert client.get("/api/offers?recruitment_type=autumn").json() == []
