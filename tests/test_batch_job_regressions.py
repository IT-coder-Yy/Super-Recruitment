"""同次上传共用岗位，但独立批次、旧任务和既有岗位互不混淆。"""
import pytest
from sqlalchemy import func, select

from app.models import Application, ImportTask, Job, JobVersion
from app.services.workflow import get_or_create_job_version
from tests.test_demo_contracts import env, post


def upload_batch(client, headers, **values):
    files = [
        ("files", (f"candidate-{index}.txt", (
            f"姓名：虚构批次{index}\n手机号：1380013100{index}\n"
            f"邮箱：batch-{index}@example.test\n项目：Python FastAPI 后端 API"
        ).encode(), "text/plain"))
        for index in [1, 2]
    ]
    files.append(("files", ("empty.txt", b"", "text/plain")))
    result = client.post("/api/import/files", headers=headers, data=values, files=files)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert len(payload["tasks"]) == 2 and len(payload["errors"]) == 1
    return [task["id"] for task in payload["tasks"]]


def confirm(client, headers, task_id):
    post(client, headers, f"/api/import/tasks/{task_id}/parse")
    return post(client, headers, f"/api/import/tasks/{task_id}/confirm")["application_id"]


@pytest.mark.parametrize("kind", ["autumn", "internship"])
def test_batch_candidates_share_job_and_autumn_exam(env, kind):
    client, headers, factory = env
    tasks = upload_batch(client, headers, recruitment_type=kind, job_title="虚构后端岗位", jd_text="要求 Python API")
    with factory() as session:
        assert session.scalar(select(func.count(Job.id))) == 0
    application_ids = [confirm(client, headers, task_id) for task_id in tasks]
    with factory() as session:
        applications = [session.get(Application, app_id) for app_id in application_ids]
        job_id = applications[0].job_id
        assert applications[1].job_id == job_id
        assert applications[0].candidate_id != applications[1].candidate_id
        assert applications[0].job.recruitment_type == kind
        assert session.scalar(select(func.count(Job.id))) == 1
        assert session.scalar(select(func.count(JobVersion.id))) == 1
        revisions = [(app.candidate.structured_data or {}).get("revision", 0) for app in applications]
    if kind == "autumn":
        resource = post(client, headers, "/api/exams/resources", kind="assessment", title="同岗位共用测评",
                        url="https://exam.example.test/batch", job_id=job_id)
        for app_id, revision in zip(application_ids, revisions):
            post(client, headers, f"/api/applications/{app_id}/evaluate")
            post(client, headers, f"/api/applications/{app_id}/decision", status="assessment_pending", reason="核实通过")
            sent = post(client, headers, f"/api/applications/{app_id}/exams/send",
                        resource_id=resource["resource_id"], candidate_revision=revision)
            assert sent["ok"] and sent["status"] == "mocked"


def test_same_title_in_separate_batches_stays_separate(env):
    client, headers, factory = env
    application_ids = []
    for _ in range(2):
        tasks = upload_batch(client, headers, recruitment_type="internship", job_title="同名实习岗位", jd_text="相同 JD")
        application_ids.append(confirm(client, headers, tasks[0]))
    with factory() as session:
        applications = [session.get(Application, app_id) for app_id in application_ids]
        assert applications[0].job_id != applications[1].job_id
        assert applications[0].candidate_id == applications[1].candidate_id
        assert session.scalar(select(func.count(Job.id))) == 2


def test_existing_job_keeps_type_and_current_jd(env):
    client, headers, factory = env
    with factory() as session:
        job, _, _ = get_or_create_job_version(session, {
            "platform": "test", "source_job_id": "existing", "title": "已有实习岗位",
            "recruitment_type": "internship", "jd_raw": "旧 JD",
        })
        _, current, _ = get_or_create_job_version(session, {
            "platform": "test", "source_job_id": "existing", "title": "已有实习岗位",
            "recruitment_type": "internship", "jd_raw": "当前 JD",
        })
        job_id, current_id = job.id, current.id
        session.commit()
    tasks = upload_batch(client, headers, job_id=job_id, recruitment_type="autumn", jd_text="上传不得覆盖")
    application_ids = [confirm(client, headers, task_id) for task_id in tasks]
    with factory() as session:
        for app_id in application_ids:
            application = session.get(Application, app_id)
            assert application.job_id == job_id and application.job_version_id == current_id
            assert application.job.recruitment_type == "internship"
        assert session.scalar(select(func.count(Job.id))) == 1
        assert session.scalar(select(func.count(JobVersion.id))) == 2


def test_later_candidate_in_batch_does_not_replace_shared_jd(env):
    client, headers, factory = env
    tasks = upload_batch(client, headers, recruitment_type="internship", job_title="共同岗位")
    first_id = confirm(client, headers, tasks[0])
    post(client, headers, f"/api/import/tasks/{tasks[1]}/parse")
    with factory() as session:
        first = session.get(Application, first_id)
        job_id, version_id = first.job_id, first.job_version_id
        original_jd = session.get(JobVersion, version_id).jd_raw
        task = session.get(ImportTask, tasks[1])
        task.preview_json = {**task.preview_json, "job": {"title": "不同解析岗位", "required_skills": ["完全不同要求"]}}
        session.commit()
    second_id = post(client, headers, f"/api/import/tasks/{tasks[1]}/confirm")["application_id"]
    with factory() as session:
        second = session.get(Application, second_id)
        assert second.job_id == job_id and second.job_version_id == version_id
        assert session.get(JobVersion, version_id).jd_raw == original_jd
        assert second.job.title == "共同岗位"
        assert session.scalar(select(func.count(JobVersion.id))) == 1


def test_legacy_tasks_without_batch_key_keep_separate_job_identity(env):
    client, headers, factory = env
    tasks = upload_batch(client, headers, recruitment_type="autumn", job_title="旧任务同名岗位")
    with factory() as session:
        for task_id in tasks:
            task = session.get(ImportTask, task_id)
            task.summary_json = {key: value for key, value in task.summary_json.items() if key != "new_job_source_id"}
        session.commit()
    application_ids = [confirm(client, headers, task_id) for task_id in tasks]
    with factory() as session:
        applications = [session.get(Application, app_id) for app_id in application_ids]
        assert applications[0].job_id != applications[1].job_id
        assert [app.job.source_job_id for app in applications] == [f"local-import-{task_id}" for task_id in tasks]
