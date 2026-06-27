from __future__ import annotations

import json
import os
import re

import httpx


BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000")
USERNAME = os.getenv("SMOKE_USERNAME", "smoke-test-user")
PASSWORD = os.getenv("SMOKE_PASSWORD", "smoke-test-password")


def csrf_token(client: httpx.Client, path: str = "/") -> str:
    response = client.get(path)
    response.raise_for_status()
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    if not match:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    if not match:
        raise RuntimeError(f"CSRF token not found on {path}")
    return match.group(1)


def csrf_headers(client: httpx.Client) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token(client)}


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        health = client.get("/health")
        health.raise_for_status()

        registration = client.post(
            "/register",
            data={
                "username": USERNAME,
                "password": PASSWORD,
                "csrf_token": csrf_token(client, "/register"),
            },
            follow_redirects=False,
        )
        if registration.status_code not in {303, 409}:
            registration.raise_for_status()

        login = client.post(
            "/login",
            data={
                "username": USERNAME,
                "password": PASSWORD,
                "csrf_token": csrf_token(client, "/login"),
            },
            follow_redirects=False,
        )
        if login.status_code != 303:
            login.raise_for_status()

        applications = client.get("/api/candidates").json()
        target = next(
            (item for item in applications if item["status"] == "evaluation_review"),
            None,
        )
        needs_decision = target is not None
        if target is None:
            target = next(
                (item for item in applications if item["status"] == "interview_pending"),
                None,
            )
            needs_decision = False

        workflow: dict[str, object] = {
            "health": health.json(),
            "authentication": "ok",
            "adapter_boundaries": {
                "database": "SQLite is the source of truth",
                "ai": "mock by default; OpenAI-compatible key is optional",
                "meeting": "Mock Tencent Meeting without open-platform credentials",
                "wecom": "Webhook when configured, otherwise mock internal notifications",
                "tencent_docs": "XLSX/CSV export or mock sync record, not the database",
            },
        }

        if target:
            if needs_decision:
                decision = client.post(
                    f"/api/applications/{target['id']}/decision",
                    headers=csrf_headers(client),
                    data={
                        "status": "interview_pending",
                        "reason": "端到端冒烟测试：HR 人工通过",
                    },
                )
                decision.raise_for_status()
                workflow["decision"] = decision.json()

            schedule = client.post(
                f"/api/applications/{target['id']}/schedule",
                headers=csrf_headers(client),
            )
            schedule.raise_for_status()
            schedule_data = schedule.json()

            booking_url = schedule_data["booking_url"].removeprefix(BASE_URL)
            booking_page = client.get(booking_url)
            booking_page.raise_for_status()
            match = re.search(
                r'name="slot_id"\s+value="(\d+)"',
                booking_page.text,
            )
            if not match:
                raise RuntimeError("booking page has no confirmable slot")

            confirm = client.post(
                f"{booking_url}/confirm",
                data={"slot_id": match.group(1)},
            )
            confirm.raise_for_status()
            workflow.update(
                {
                    "target_application_id": target["id"],
                    "schedule_slot_count": len(schedule_data["slots"]),
                    "booking_page_status": booking_page.status_code,
                    "confirm_status": confirm.status_code,
                }
            )
        else:
            workflow["note"] = (
                "No evaluation_review or interview_pending application; "
                "state-changing loop skipped."
            )

        xlsx = client.get("/api/exports/recruitment.xlsx")
        csv = client.get("/api/exports/recruitment.csv")
        xlsx.raise_for_status()
        csv.raise_for_status()
        workflow.update(
            {
                "dashboard": client.get("/api/dashboard/summary").json(),
                "xlsx_bytes": len(xlsx.content),
                "csv_bytes": len(csv.content),
            }
        )
        print(json.dumps(workflow, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
