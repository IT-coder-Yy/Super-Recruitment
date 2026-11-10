"""预约邮件与群通知必须按同一次发送事实防重。"""
import httpx
import pytest
from sqlalchemy import select

from app.models import Notification, NotificationStatus
from app.services.periodic import recover_inflight
from tests.test_tencent_meeting_booking import env, invite


def test_repeated_invite_emits_one_group_notification(env, monkeypatch):
    calls = []
    monkeypatch.setenv('WECOM_WEBHOOK_URL', 'https://wecom.example.test/synthetic')
    def send(url, **kw):
        calls.append(kw['json'])
        return httpx.Response(200, json={'errcode': 0}, request=httpx.Request('POST', url))
    monkeypatch.setattr('app.adapters.notifications.httpx.post', send)
    _, booking = invite(env)
    url = f"/api/interviews/{booking['interview_id']}/send-invite"
    first = env[0].post(url, headers=env[1]); second = env[0].post(url, headers=env[1])
    assert first.status_code == second.status_code == 200
    assert first.json()['notification_id'] == second.json()['notification_id']
    assert len(calls) == 1
    with env[2]() as session:
        rows = list(session.scalars(select(Notification)))
        assert len(rows) == 2
        mail = next(n for n in rows if n.channel == 'email')
        assert 'Mock 腾讯会议' not in mail.content


def test_failed_email_never_announces_sent_in_group(env, monkeypatch):
    import smtplib
    monkeypatch.setenv('SMTP_HOST', 'smtp.example.test')
    monkeypatch.setenv('SMTP_SSL', 'true')
    def refuse(*args, **kwargs): raise smtplib.SMTPConnectError(421, 'synthetic refusal')
    monkeypatch.setattr('app.services.offers.smtplib.SMTP_SSL', refuse)
    monkeypatch.setattr('app.services.offers.send_wecom_notification', lambda *a, **k: pytest.fail('邮件失败不能通知已发送'))
    _, booking = invite(env)
    result = env[0].post(f"/api/interviews/{booking['interview_id']}/send-invite", headers=env[1])
    assert result.status_code == 200 and result.json()['status'] == 'failed'
    with env[2]() as session:
        assert not session.scalar(select(Notification.id).where(Notification.channel == 'wecom'))


def test_interrupted_group_send_keeps_claim_and_does_not_repeat(env, monkeypatch):
    from app.services.offers import notify_internal
    from tests.test_demo_contracts import application
    def interrupt(*args, **kwargs): raise KeyboardInterrupt()
    monkeypatch.setattr('app.services.offers.send_wecom_notification', interrupt)
    with env[2]() as session:
        rec = application(session)
        with pytest.raises(KeyboardInterrupt):
            notify_internal(session, title='虚构通知', content='测试', target_type='application', target_id=rec.id, dedupe_template='test-invite')
    with env[2]() as session:
        recover_inflight(session); session.commit()
        result = notify_internal(session, title='虚构通知', content='测试', target_type='application', target_id=rec.id, dedupe_template='test-invite')
        assert result.status == NotificationStatus.UNKNOWN
        assert len(list(session.scalars(select(Notification)))) == 1
