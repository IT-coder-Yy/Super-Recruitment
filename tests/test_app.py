from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from dotenv import set_key
from fastapi.testclient import TestClient


os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_recruitment.db")

from app import config as app_config  # noqa: E402
from app import db as app_db  # noqa: E402
from app.main import app  # noqa: E402


def _csrf_token(client: TestClient, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    if not match:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match, f"CSRF token not found on {path}"
    return match.group(1)


def _csrf_headers(client: TestClient, path: str = "/") -> dict[str, str]:
    return {"X-CSRF-Token": _csrf_token(client, path)}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def authenticated_client(client: TestClient):
    client.cookies.clear()
    client.post(
        "/register",
        data={
            "username": "existing-test-user",
            "password": "password123",
            "csrf_token": _csrf_token(client, "/register"),
        },
    )
    response = client.post(
        "/login",
        data={
            "username": "EXISTING-TEST-USER",
            "password": "password123",
            "csrf_token": _csrf_token(client, "/login"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    yield client
    client.cookies.clear()


def _application_row(client: TestClient, application_id: int) -> dict:
    response = client.get("/api/candidates")
    assert response.status_code == 200
    for row in response.json():
        row_application_id = row.get("application_id", row.get("id"))
        if row_application_id == application_id:
            return row
    pytest.fail(f"application {application_id} not found")


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unauthenticated_page_redirects_to_login(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_api_is_rejected(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录"


def test_authenticated_api_requires_csrf(authenticated_client: TestClient) -> None:
    response = authenticated_client.post("/api/import/mock")
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF 校验失败，请刷新页面后重试"


def test_candidate_booking_route_remains_public(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/schedule/invalid-token")
    assert response.status_code == 400
    assert response.url.path == "/schedule/invalid-token"


def test_registration_does_not_log_user_in(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(
        "/register",
        data={
            "username": "NewUser",
            "password": "secret123",
            "csrf_token": _csrf_token(client, "/register"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?registered=1"

    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


def test_registration_validates_lengths(client: TestClient) -> None:
    client.cookies.clear()
    short_username = client.post(
        "/register",
        data={
            "username": "ab",
            "password": "secret123",
            "csrf_token": _csrf_token(client, "/register"),
        },
    )
    short_password = client.post(
        "/register",
        data={
            "username": "valid-user",
            "password": "12345",
            "csrf_token": _csrf_token(client, "/register"),
        },
    )
    assert short_username.status_code == 422
    assert "用户名至少需要 3 个字符" in short_username.text
    assert short_password.status_code == 422
    assert "密码至少需要 6 个字符" in short_password.text


def test_username_is_case_insensitive_and_password_must_match(
    client: TestClient,
) -> None:
    client.cookies.clear()
    client.post(
        "/register",
        data={
            "username": "CaseUser",
            "password": "correct-password",
            "csrf_token": _csrf_token(client, "/register"),
        },
    )
    duplicate = client.post(
        "/register",
        data={
            "username": "caseuser",
            "password": "another-password",
            "csrf_token": _csrf_token(client, "/register"),
        },
    )
    wrong_password = client.post(
        "/login",
        data={
            "username": "CASEUSER",
            "password": "wrong-password",
            "csrf_token": _csrf_token(client, "/login"),
        },
    )
    assert duplicate.status_code == 409
    assert "该用户名已被注册" in duplicate.text
    assert wrong_password.status_code == 401
    assert "用户名或密码错误" in wrong_password.text

    success = client.post(
        "/login",
        data={
            "username": "caseuser",
            "password": "correct-password",
            "csrf_token": _csrf_token(client, "/login"),
        },
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_logout_invalidates_session(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        "/logout",
        data={"csrf_token": _csrf_token(authenticated_client, "/")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    protected = authenticated_client.get("/", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


def test_mock_import_is_idempotent(authenticated_client: TestClient) -> None:
    first = authenticated_client.post("/api/import/mock", headers=_csrf_headers(authenticated_client))
    second = authenticated_client.post("/api/import/mock", headers=_csrf_headers(authenticated_client))
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["candidate_count"] == first.json()["candidate_count"]


def test_local_resume_upload_parse_preview_and_confirm(
    authenticated_client: TestClient,
) -> None:
    resume = (
        "姓名：李明\n"
        "手机号：13800138099\n"
        "邮箱：liming@example.test\n"
        "项目：使用 Python、FastAPI 和 RAG 开发招聘助手平台\n"
        "教育：虚构大学 计算机 本科\n"
    ).encode("utf-8")
    upload = authenticated_client.post(
        "/api/import/files",
        headers=_csrf_headers(authenticated_client),
        data={
            "job_title": "AI 应用开发实习生",
            "jd_text": "负责 AI 应用开发，要求 Python、FastAPI、RAG。",
            "owner": "测试 HR",
        },
        files={"files": ("resume.txt", resume, "text/plain")},
    )
    assert upload.status_code == 200
    task_id = upload.json()["task"]["id"]
    assert upload.json()["task"]["status"] == "uploaded"

    parsed = authenticated_client.post(
        f"/api/import/tasks/{task_id}/parse",
        headers=_csrf_headers(authenticated_client),
    )
    assert parsed.status_code == 200
    task = parsed.json()["task"]
    assert task["status"] == "needs_review"
    assert task["preview"]["candidate"]["name"] == "李明"
    assert task["preview"]["candidate"]["phone"] == "13800138099"
    assert task["summary"]["human_summary"]["headline"]

    confirmed = authenticated_client.post(
        f"/api/import/tasks/{task_id}/confirm",
        headers=_csrf_headers(authenticated_client),
        data={"start_evaluation": "false"},
    )
    assert confirmed.status_code == 200
    candidate_id = confirmed.json()["candidate_id"]

    detail = authenticated_client.get(f"/candidates/{candidate_id}")
    assert detail.status_code == 200
    assert "简历摘要" in detail.text
    assert "经历与技能" in detail.text
    assert "技术详情" in detail.text
    assert "结构化 JSON、解析器输出和调试数据" in detail.text


def test_candidate_supplement_and_delete(
    authenticated_client: TestClient,
) -> None:
    resume = "姓名：补充候选人\n手机号：13800138991\n项目：后端 API".encode("utf-8")
    upload = authenticated_client.post(
        "/api/import/files",
        headers=_csrf_headers(authenticated_client),
        data={"job_title": "后端开发实习生", "jd_text": "负责后端 API。"},
        files={"files": ("supplement.txt", resume, "text/plain")},
    )
    assert upload.status_code == 200
    task_id = upload.json()["task"]["id"]
    assert authenticated_client.post(
        f"/api/import/tasks/{task_id}/parse",
        headers=_csrf_headers(authenticated_client),
    ).status_code == 200
    confirmed = authenticated_client.post(
        f"/api/import/tasks/{task_id}/confirm",
        headers=_csrf_headers(authenticated_client),
        data={"start_evaluation": "false"},
    )
    assert confirmed.status_code == 200
    candidate_id = confirmed.json()["candidate_id"]

    supplemented = authenticated_client.post(
        f"/api/candidates/{candidate_id}/supplement",
        headers=_csrf_headers(authenticated_client),
        data={
            "name": "补充候选人",
            "phone": "13800138991",
            "email": "supplement@example.test",
            "skills": "Python\nFastAPI",
            "experience": "补充项目：招聘自动化 Demo",
            "chat_summary": "可尽快到岗",
            "note": "内部备注",
        },
    )
    assert supplemented.status_code == 200
    detail = authenticated_client.get(f"/candidates/{candidate_id}")
    assert "supplement@example.test" in detail.text
    assert "FastAPI" in detail.text
    assert "可尽快到岗" in detail.text

    deleted = authenticated_client.post(
        f"/api/candidates/{candidate_id}/delete",
        headers=_csrf_headers(authenticated_client),
        data={"confirm": "DELETE"},
    )
    assert deleted.status_code == 200
    assert authenticated_client.get(f"/api/candidates/{candidate_id}").status_code == 404


def test_local_resume_confirm_can_start_evaluation(
    authenticated_client: TestClient,
) -> None:
    upload = authenticated_client.post(
        "/api/import/files",
        headers=_csrf_headers(authenticated_client),
        data={
            "job_title": "AI 应用开发实习生",
            "jd_text": "负责 AI 应用开发，要求 Python、FastAPI、RAG。",
        },
        files={
            "files": (
                "resume.md",
                "姓名：评估候选人\n手机号：13800138188\n项目：Python FastAPI RAG 招聘助手".encode(
                    "utf-8"
                ),
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 200
    task_id = upload.json()["task"]["id"]
    assert authenticated_client.post(
        f"/api/import/tasks/{task_id}/parse",
        headers=_csrf_headers(authenticated_client),
    ).status_code == 200

    confirmed = authenticated_client.post(
        f"/api/import/tasks/{task_id}/confirm",
        headers=_csrf_headers(authenticated_client),
        data={"start_evaluation": "true"},
    )
    assert confirmed.status_code == 200
    payload = confirmed.json()
    assert payload["evaluation"]["evaluation_id"]
    assert payload["evaluation"]["total_score"] >= 0

    rows = authenticated_client.get("/api/candidates").json()
    assert any(
        row["application_id"] == payload["application_id"]
        if "application_id" in row
        else row["id"] == payload["application_id"] and row["status"] == "evaluation_review"
        for row in rows
    )


def test_dashboard_summary_has_core_metrics(
    authenticated_client: TestClient,
) -> None:
    authenticated_client.post("/api/import/mock", headers=_csrf_headers(authenticated_client))
    response = authenticated_client.get("/api/dashboard/summary")
    assert response.status_code == 200
    payload = response.json()
    assert "total_candidates" in payload
    assert "funnel" in payload
    assert "status_counts" in payload


def test_all_management_pages_render(
    authenticated_client: TestClient,
) -> None:
    authenticated_client.post("/api/import/mock", headers=_csrf_headers(authenticated_client))
    paths = [
        "/",
        "/dashboard",
        "/candidates",
        "/candidates/1",
        "/import",
        "/mock-platform",
        "/evaluations",
        "/interviews",
        "/offers",
        "/onboarding",
        "/notifications",
        "/follow-ups",
        "/audit",
        "/settings",
    ]
    for path in paths:
        response = authenticated_client.get(path)
        assert response.status_code == 200, path
        assert "TalentFlow" in response.text


def test_offer_and_onboarding_full_loop(
    authenticated_client: TestClient,
) -> None:
    resume = (
        "姓名：Offer候选人\n"
        "手机号：13800138266\n"
        "邮箱：offer-candidate@example.test\n"
        "项目：使用 Python、FastAPI、SQLAlchemy 构建招聘自动化 Demo\n"
        "意向岗位：后端开发实习生\n"
    ).encode("utf-8")
    upload = authenticated_client.post(
        "/api/import/files",
        headers=_csrf_headers(authenticated_client),
        data={
            "job_title": "后端开发实习生",
            "jd_text": "负责后端 API、自动化工作流和数据看板开发。",
            "owner": "测试 HR",
        },
        files={"files": ("offer-candidate.txt", resume, "text/plain")},
    )
    assert upload.status_code == 200
    task_id = upload.json()["task"]["id"]
    assert authenticated_client.post(
        f"/api/import/tasks/{task_id}/parse",
        headers=_csrf_headers(authenticated_client),
    ).status_code == 200

    confirmed = authenticated_client.post(
        f"/api/import/tasks/{task_id}/confirm",
        headers=_csrf_headers(authenticated_client),
        data={"start_evaluation": "true"},
    )
    assert confirmed.status_code == 200
    application_id = confirmed.json()["application_id"]

    decision = authenticated_client.post(
        f"/api/applications/{application_id}/decision",
        headers=_csrf_headers(authenticated_client),
        data={"status": "interview_pending", "reason": "测试流程进入面试"},
    )
    assert decision.status_code == 200

    scheduled = authenticated_client.post(
        f"/api/applications/{application_id}/schedule",
        headers=_csrf_headers(authenticated_client),
    )
    assert scheduled.status_code == 200
    schedule_payload = scheduled.json()
    interview_id = schedule_payload["interview_id"]
    slot_id = schedule_payload["slots"][0]["id"]
    token = urlparse(schedule_payload["booking_url"]).path.rsplit("/", 1)[-1]

    invite = authenticated_client.post(
        f"/api/interviews/{interview_id}/send-invite",
        headers=_csrf_headers(authenticated_client),
    )
    assert invite.status_code == 200
    assert invite.json()["status"] in {"mocked", "sent"}

    booking = authenticated_client.post(
        f"/schedule/{token}/confirm",
        data={"slot_id": slot_id},
    )
    assert booking.status_code == 200
    assert "Mock" in booking.text

    feedback = authenticated_client.post(
        f"/api/interviews/{interview_id}/feedback",
        headers=_csrf_headers(authenticated_client),
        data={
            "result": "通过",
            "comment": "技术面通过，进入 Offer",
            "next_action": "offer",
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["status"] == "offer_pending"

    offers = authenticated_client.get("/api/offers")
    assert offers.status_code == 200
    offer = next(item for item in offers.json() if item["application_id"] == application_id)
    assert offer["status"] == "pending_approval"

    draft = authenticated_client.post(
        f"/api/offers/{offer['id']}/generate-draft",
        headers=_csrf_headers(authenticated_client),
    )
    assert draft.status_code == 200
    assert "待 HR 填写" in draft.json()["message"]

    approved = authenticated_client.post(
        f"/api/offers/{offer['id']}/approve",
        headers=_csrf_headers(authenticated_client),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    sent = authenticated_client.post(
        f"/api/offers/{offer['id']}/send",
        headers=_csrf_headers(authenticated_client),
    )
    assert sent.status_code == 200
    sent_payload = sent.json()
    assert sent_payload["status"] == "sent"
    assert sent_payload["candidate_email"] == "offer-candidate@example.test"

    public_offer = authenticated_client.get(sent_payload["offer_url"])
    assert public_offer.status_code == 200
    assert "欢迎你加入 Testin" in public_offer.text
    assert "Offer候选人" in public_offer.text
    assert "后端开发实习生" in public_offer.text

    responded = authenticated_client.post(
        f"{sent_payload['offer_url']}/respond",
        data={"accepted": "true", "note": "确认接受"},
        follow_redirects=False,
    )
    assert responded.status_code == 303

    onboarding_tasks = authenticated_client.get("/api/onboarding")
    assert onboarding_tasks.status_code == 200
    tasks = [
        item
        for item in onboarding_tasks.json()
        if item["application_id"] == application_id and item["status"] == "open"
    ]
    assert tasks
    for task in tasks:
        done = authenticated_client.post(
            f"/api/onboarding/{task['id']}/complete",
            headers=_csrf_headers(authenticated_client),
        )
        assert done.status_code == 200
        assert done.json()["status"] == "done"

    row = _application_row(authenticated_client, application_id)
    assert row["status"] == "joined"

    notifications = authenticated_client.get("/notifications")
    assert notifications.status_code == 200
    assert "Offer" in notifications.text


def test_settings_never_echoes_plain_api_key(
    authenticated_client: TestClient,
) -> None:
    marker = "sk-local-test-secret"
    response = authenticated_client.post(
        "/api/settings/llm",
        headers=_csrf_headers(authenticated_client),
        data={
            "provider": "mock",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": marker,
        },
    )
    assert response.status_code == 200
    assert marker not in response.text

    response = authenticated_client.get("/api/settings/llm")
    assert response.status_code == 200
    assert marker not in response.text
    assert response.json()["api_key_configured"] is True
    assert response.json()["api_key_hint"] == "••••cret"

    cleared = authenticated_client.post(
        "/api/settings/llm/clear",
        headers=_csrf_headers(authenticated_client),
    )
    assert cleared.status_code == 200
    assert cleared.json()["api_key_configured"] is False
    assert cleared.json()["provider"] == "mock"


def teardown_module() -> None:
    set_key(str(app_config.ENV_FILE), "LLM_API_KEY", "")
    app_db.engine.dispose()
    test_db = Path("data/test_recruitment.db")
    if test_db.exists():
        test_db.unlink()
