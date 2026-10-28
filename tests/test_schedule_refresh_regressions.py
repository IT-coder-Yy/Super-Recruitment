"""多人先后预约后，HR 刷新应拿到可用时段而不是旧冲突集合。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Interview, InterviewSlot, InterviewSlotStatus
from app.services.scheduling import DEFAULT_BUFFER_MINUTES, intervals_conflict
from tests.test_demo_contracts import env, application, enter_interview, post


def test_schedule_refresh_regressions_replaces_occupied_slots_and_keeps_round(env):
    client, headers, factory = env
    with factory() as session:
        records = [application(session) for _ in range(3)]
    schedules = []
    for record in records:
        enter_interview(client, headers, record.id)
        schedules.append(post(client, headers, f"/api/applications/{record.id}/schedule"))
    for index, slot_index in [(0, 0), (1, -1)]:
        booking = schedules[index]
        assert client.post(booking["booking_url"] + "/confirm", data={"slot_id": booking["slots"][slot_index]["id"]}).status_code == 200

    original = schedules[2]
    refreshed = post(client, headers, f"/api/applications/{records[2].id}/schedule")
    old_ids = {slot["id"] for slot in original["slots"]}
    assert refreshed["interview_id"] == original["interview_id"]
    assert refreshed["booking_url"] == original["booking_url"]
    assert len(refreshed["slots"]) == 5
    assert old_ids.isdisjoint(slot["id"] for slot in refreshed["slots"])
    with factory() as session:
        assert all(session.get(InterviewSlot, key).status == InterviewSlotStatus.RELEASED for key in old_ids)
        assert [item.round for item in session.scalars(select(Interview).where(Interview.application_id == records[2].id))] == [1]
        occupied = session.scalars(select(Interview).where(Interview.confirmed_start_at.is_not(None))).all()
        for slot in refreshed["slots"]:
            assert all(not intervals_conflict(slot["start_at"], slot["end_at"], other.confirmed_start_at,
                                             other.confirmed_end_at, buffer_minutes=DEFAULT_BUFFER_MINUTES) for other in occupied)
    assert client.post(original["booking_url"] + "/confirm", data={"slot_id": original["slots"][0]["id"]}).status_code == 409
    repeated = post(client, headers, f"/api/applications/{records[2].id}/schedule")
    assert [item["id"] for item in repeated["slots"]] == [item["id"] for item in refreshed["slots"]]
    assert client.post(refreshed["booking_url"] + "/confirm", data={"slot_id": refreshed["slots"][0]["id"]}).status_code == 200


def test_schedule_refresh_regressions_releases_expired_slots(env):
    client, headers, factory = env
    with factory() as session:
        record = application(session)
    enter_interview(client, headers, record.id)
    original = post(client, headers, f"/api/applications/{record.id}/schedule")
    old_id = original["slots"][0]["id"]
    with factory() as session:
        expired = session.get(InterviewSlot, old_id)
        expired.start_at = datetime.now(timezone.utc) - timedelta(days=2)
        expired.end_at = expired.start_at + timedelta(hours=1)
        session.commit()
    refreshed = post(client, headers, f"/api/applications/{record.id}/schedule")
    assert old_id not in {item["id"] for item in refreshed["slots"]}
    with factory() as session:
        assert session.get(InterviewSlot, old_id).status == InterviewSlotStatus.RELEASED
