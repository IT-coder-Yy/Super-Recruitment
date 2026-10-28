"""业务日期按北京时间判断，审计仍使用 UTC。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Application, ApplicationStatus, AuditLog, OnboardingTask
from app.services import offers, onboarding
from tests.test_demo_contracts import env, ready_offer, post


@pytest.mark.parametrize("clock", ["00:00", "01:00", "07:59", "08:00", "23:59"])
def test_onboarding_date_regressions_uses_beijing_day(env, monkeypatch, clock):
    client, headers, factory = env
    with factory() as session:
        offer, _ = ready_offer(session)
        offers.send_offer(session, offer.id)
        session.commit()
        offers.respond_offer(session, offer.candidate_token, accepted=True)
        session.commit()
        tasks = session.scalars(select(OnboardingTask).where(OnboardingTask.application_id == offer.application_id)).all()
    beijing = datetime.fromisoformat(f"2026-11-03T{clock}:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    fixed_now = beijing.astimezone(ZoneInfo("UTC"))
    monkeypatch.setattr(onboarding, "utc_now", lambda: fixed_now)
    today = beijing.date()
    endpoint = f"/api/applications/{offer.application_id}/onboarding"
    response = client.post(endpoint, headers=headers, data={"action": "update", "version": 0,
        "expected_date": (today - timedelta(days=1)).isoformat(), "location": "虚构前台"})
    assert response.status_code == 409
    for action in ["update", "defer"]:
        post(client, headers, endpoint, action=action, version=0 if action == "update" else 1,
             expected_date=today.isoformat(), location="虚构前台", note="HR 确认当天办理")
    for task in tasks:
        post(client, headers, f"/api/onboarding/{task.id}/complete")
    response = client.post(endpoint, headers=headers, data={"action": "join", "version": 2,
        "actual_date": (today + timedelta(days=1)).isoformat(), "note": "不允许确认明天到岗"})
    assert response.status_code == 409
    assert post(client, headers, endpoint, action="join", version=2,
                actual_date=today.isoformat(), note="HR 已现场核实")["status"] == "joined"
    with factory() as session:
        record = session.get(Application, offer.application_id)
        assert record.status == ApplicationStatus.JOINED
        assert record.extra_data["onboarding"]["actual_date"] == today.isoformat()
        assert record.extra_data["onboarding"]["updated_at"] == fixed_now.isoformat()
        assert len(session.scalars(select(AuditLog).where(AuditLog.entity_id == record.id, AuditLog.action == "onboarding.join")).all()) == 1
