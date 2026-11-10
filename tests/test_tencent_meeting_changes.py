"""改期/取消与丢失响应恢复；真实协议替身，禁止外呼。"""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from app.models import Interview, InterviewStatus, ApplicationStatus, Notification
from app.services import meetings
from app.services.periodic import recover_inflight
from app.workbench import appointment_time
from tests.test_tencent_meeting_booking import env, wire, invite, confirm, reply


def prepared(env):
    record, booking = invite(env)
    assert confirm(env, booking).status_code == 200
    with env[2]() as session:
        row = session.get(Interview, booking['interview_id'])
        snapshot = dict(row.meeting_request)
    start = datetime.fromisoformat(snapshot['start_time']) + timedelta(days=9)
    data = {'action': 'update', 'revision': snapshot['attempt_id'], 'reason': '已协商改期',
            'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat()}
    return record, booking['interview_id'], snapshot, data


def change(env, ident, data):
    return env[0].post(f'/api/interviews/{ident}/meeting/change', headers=env[1], data=data)


def query(env, ident):
    return env[0].post(f'/api/interviews/{ident}/meeting/reconcile', headers=env[1])


def updated(snapshot, data):
    return httpx.Response(200, json=reply({**snapshot, 'start_time': data['start_at'], 'end_time': data['end_at']}))


def ack_cancel(body=''):
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {'content': [
        {'type': 'text', 'text': json.dumps({'status_code': 200, 'body': body})}]}})


def test_reschedule_query_cancel_and_stale_submit_never_repeat(env, wire):
    calls, responses = wire
    record, ident, snapshot, data = prepared(env)
    responses.extend([httpx.Response(200, json=reply(snapshot)), updated(snapshot, data)])
    assert change(env, ident, data).status_code == 200
    assert change(env, ident, data).status_code == 409
    assert [c['name'] for c in calls] == ['schedule_meeting', 'update_meeting', 'get_meeting']
    with env[2]() as session:
        row = session.get(Interview, ident)
        assert row.meeting_id == '123456789012345'
        assert appointment_time(row.confirmed_start_at) == datetime.fromisoformat(data['start_at'])
        assert row.meeting_request['start_time'] == data['start_at']
        revision = row.meeting_request['revision']
        assert len(meetings.occupied_intervals(session)) == 1
        assert list(session.scalars(select(Notification))) == []
    page = env[0].get('/interviews').text
    assert '确认改期' in page and '取消本次面试' in page
    cancellation = {'action': 'cancel', 'revision': revision, 'reason': '候选人申请重新排期'}
    responses.append(ack_cancel())
    assert change(env, ident, cancellation).status_code == 200
    assert change(env, ident, cancellation).status_code == 409
    assert len(calls) == 4 and calls[-1]['name'] == 'cancel_meeting'
    with env[2]() as session:
        row = session.get(Interview, ident)
        assert row.status == InterviewStatus.CANCELLED and row.meeting_status == 'real_cancelled'
        assert row.application.status == ApplicationStatus.INTERVIEW_PENDING
        assert row.meeting_id and row.confirmed_start_at  # 历史不删除；取消后不占用。
        assert meetings.occupied_intervals(session) == []
        assert list(session.scalars(select(Notification))) == []
    assert '本次会议已取消' in env[0].get('/interviews').text
    assert env[0].post(f'/api/applications/{record.id}/schedule', headers=env[1]).status_code == 200
    assert len(calls) == 4


def test_update_timeout_preserves_both_intervals_and_query_recovers_without_resend(env, wire):
    calls, responses = wire
    _, ident, snapshot, data = prepared(env)
    responses.append(httpx.ReadTimeout('private value'))
    assert change(env, ident, data).status_code == 503
    assert change(env, ident, data).status_code == 409
    with env[2]() as session:
        row = session.get(Interview, ident)
        assert row.meeting_status == 'real_update_unknown'
        assert len(meetings.occupied_intervals(session)) == 2
    blocked = env[0].post(f'/api/interviews/{ident}/feedback', headers=env[1],
                          data={'result': '通过', 'comment': '不能覆盖变更', 'next_action': 'offer'})
    assert blocked.status_code in {409, 422}
    page = env[0].get('/interviews').text
    assert '查询并核实变更' in page and 'name="next_action"' not in page
    responses.append(httpx.Response(200, json=reply(snapshot)))  # 旧时间不证明改期失败。
    assert query(env, ident).status_code == 503
    responses.append(updated(snapshot, data))
    assert query(env, ident).status_code == 200
    with env[2]() as session:
        assert session.get(Interview, ident).meeting_status == 'real_created'
        assert len(meetings.occupied_intervals(session)) == 1
    assert [c['name'] for c in calls].count('update_meeting') == 1


@pytest.mark.parametrize('cancelled_status', ['MEETING_STATE_CANCELLED', '已取消'])
def test_cancel_timeout_not_found_is_not_cancelled_and_restart_only_queries(env, wire, cancelled_status):
    calls, responses = wire
    _, ident, snapshot, data = prepared(env)
    data = {k: v for k, v in data.items() if k not in {'start_at', 'end_at'}}
    data['action'] = 'cancel'
    responses.append(httpx.WriteError('private value'))
    assert change(env, ident, data).status_code == 503
    with env[2]() as session:
        row = session.get(Interview, ident)
        row.meeting_status = 'real_cancelling'
        session.commit()
        recover_inflight(session)
        session.commit()
        assert row.meeting_status == 'real_cancel_unknown'
    responses.append(httpx.Response(404))
    assert query(env, ident).status_code == 503
    responses.append(httpx.Response(200, json=reply(snapshot, status='MEETING_STATE_INIT')))
    assert query(env, ident).status_code == 503
    with env[2]() as session:
        assert len(meetings.occupied_intervals(session)) == 1
    responses.append(httpx.Response(200, json=reply(snapshot, status=cancelled_status)))
    assert query(env, ident).status_code == 200
    assert change(env, ident, data).status_code == 409
    assert [c['name'] for c in calls].count('cancel_meeting') == 1
    with env[2]() as session:
        assert meetings.occupied_intervals(session) == []


def test_definite_failure_keeps_old_schedule_but_invalidates_old_form(env, wire):
    calls, responses = wire
    _, ident, snapshot, data = prepared(env)
    responses.append(httpx.Response(401))
    assert change(env, ident, data).status_code == 503
    assert change(env, ident, data).status_code == 409
    with env[2]() as session:
        row = session.get(Interview, ident)
        assert row.meeting_status == 'real_created' and 'change' not in row.meeting_request
        assert appointment_time(row.confirmed_start_at) == datetime.fromisoformat(snapshot['start_time'])
        assert row.meeting_request['revision'] != data['revision']
        data['revision'] = row.meeting_request['revision']
    responses.extend([httpx.Response(200, json=reply(snapshot)), httpx.Response(401)])
    assert change(env, ident, data).status_code == 503
    with env[2]() as session:
        assert session.get(Interview, ident).meeting_status == 'real_update_unknown'
    assert len(calls) == 4  # 更新已获响应后的查询失败不能释放新时段。


def test_validation_auth_account_and_conflicts_cannot_send(env, wire, monkeypatch):
    calls, _ = wire
    _, ident, snapshot, data = prepared(env)
    assert env[0].post(f'/api/interviews/{ident}/meeting/change', data=data).status_code == 403
    for altered in ({'revision': 'stale'}, {'reason': ' '}, {'action': 'unsupported'},
                    {'start_at': '2020-01-01T12:00'}, {'end_at': data['start_at']}):
        assert change(env, ident, {**data, **altered}).status_code == 409
    monkeypatch.setenv('TENCENT_MEETING_TOKEN', 'another-account')
    assert change(env, ident, data).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize('body', ['', {}, '{}', None])
def test_cancel_accepts_only_successful_empty_body(env, wire, body):
    calls, responses = wire
    _, ident, _, data = prepared(env)
    responses.append(ack_cancel(body))
    assert change(env, ident, {**data, 'action': 'cancel'}).status_code == 200
    assert len(calls) == 2


def test_change_releases_database_lock_and_blocks_concurrent_writer(env, wire):
    calls, responses = wire
    _, ident, snapshot, data = prepared(env)
    def during_send(params):
        with env[2]() as session:
            assert len(meetings.occupied_intervals(session)) == 2
            with pytest.raises(ValueError, match='状态或操作版本'):
                meetings.change_booking(session, ident, action='cancel', revision=data['revision'], reason='重复提交')
        return httpx.Response(200, json=reply(snapshot))
    responses.extend([during_send, updated(snapshot, data)])
    assert change(env, ident, data).status_code == 200
    assert len(calls) == 3


@pytest.mark.parametrize('action', ['update', 'cancel'])
@pytest.mark.parametrize('inflight', [False, True])
@pytest.mark.parametrize('valid_response', [False, True])
def test_late_creation_response_cannot_overwrite_pending_change(env, wire, action, inflight, valid_response):
    _, responses = wire
    _, ident, snapshot, data = prepared(env)
    responses.append(httpx.ReadTimeout('change response lost'))
    assert change(env, ident, {**data, 'action': action}).status_code == 503
    wrapped = json.loads(reply(snapshot)['result']['content'][0]['text'])
    result = json.loads(wrapped['body'])['meeting_info_list'][0]
    if not valid_response:
        result['join_url'] = 'https://invalid.example.test/meeting'
    with env[2]() as session:
        row = session.get(Interview, ident)
        if inflight:
            row.meeting_status = 'real_updating' if action == 'update' else 'real_cancelling'
            session.commit()
        before = (row.meeting_status, dict(row.meeting_request), meetings.occupied_intervals(session))
        with pytest.raises(ValueError):
            meetings._accept_result(session, ident, snapshot, result, remember_id=True)
        session.refresh(row)
        assert (row.meeting_status, row.meeting_request, meetings.occupied_intervals(session)) == before


@pytest.mark.parametrize('action', ['update', 'cancel'])
def test_late_creation_error_cannot_overwrite_recovered_meeting_change(env, wire, action):
    calls, responses = wire
    _, booking = invite(env)
    ident = booking['interview_id']
    def creation_finishes_after_recovery(params):
        with env[2]() as session:
            snapshot = dict(session.get(Interview, ident).meeting_request)
            wrapped = json.loads(reply(snapshot)['result']['content'][0]['text'])
            result = json.loads(wrapped['body'])['meeting_info_list'][0]
            meetings._accept_result(session, ident, snapshot, result)
            start = datetime.fromisoformat(snapshot['start_time']) + timedelta(days=9)
            responses.append(httpx.ReadTimeout('change response lost'))
            with pytest.raises(meetings.MeetingError):
                meetings.change_booking(session, ident, action=action, revision=snapshot['attempt_id'],
                                        reason='已协商变更', start_at=start, end_at=start + timedelta(minutes=30))
        raise httpx.ReadTimeout('late creation response lost')
    responses.append(creation_finishes_after_recovery)
    assert confirm(env, booking).status_code == 503
    with env[2]() as session:
        row = session.get(Interview, ident)
        assert row.meeting_status == f'real_{action}_unknown'
        assert row.meeting_request['change']['action'] == action
        assert len(meetings.occupied_intervals(session)) == (2 if action == 'update' else 1)
    assert [c['name'] for c in calls] == ['schedule_meeting', f'{action}_meeting']
