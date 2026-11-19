"""交接后须显式确认发件身份，仍通过原业务入口发送；仅使用 SMTP 替身。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from html import unescape
import json
import re
import smtplib
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, update

from app import accounts, config
from app.accounts import assign_data
from app.db import Base, init_db
from app.models import ApplicationStatus, AuditLog, ExamDispatch, ExamResource, Interview, Notification, NotificationStatus, Offer, User, utc_now
from app.ownership import account_scope
from app.services import offers
from tests.test_account_isolation import secure_env
from tests.test_demo_contracts import application, ready_offer, resource


@pytest.fixture
def handover(secure_env, monkeypatch):
    a, b, ha, hb, factory, (aid, bid) = secure_env
    attempts, messages, delivery = [], [], [False]

    class SMTP:
        def __init__(self, *args, **kwargs):
            attempts.append(args)
            if not delivery[0]:
                raise smtplib.SMTPConnectError(421, "synthetic refusal before DATA")

        def __enter__(self): return self
        def __exit__(self, *args): return False
        def login(self, *args): pass
        def send_message(self, message):
            messages.append(message)
            return {}

    monkeypatch.setattr(offers.smtplib, "SMTP_SSL", SMTP)
    for owner, address in ((aid, "source@example.test"), (bid, "receiver@example.test")):
        config.save_smtp_settings(user_id=owner, host="smtp.example.test", port=465, use_ssl=True,
                                 username=address, password="synthetic-smtp-secret", from_address=address)

    def prepare(kind="offer", *, transfer=True):
        values = {}
        with account_scope(aid), factory() as session:
            if kind == "offer":
                record, _ = ready_offer(session)
                target_id = record.id
                url = f"/api/offers/{target_id}/send"
            elif kind == "exam":
                record = application(session, ApplicationStatus.ASSESSMENT_PENDING)
                paper = resource(session)
                values = {"resource_id": paper.id, "candidate_revision": 1}
                url = f"/api/applications/{record.id}/exams/send"
            else:
                record = application(session, ApplicationStatus.INTERVIEW_PENDING)
        if kind == "interview":
            booked = a.post(f"/api/applications/{record.id}/schedule", headers=ha)
            assert booked.status_code == 200, booked.text
            target_id = booked.json()["interview_id"]
            url = f"/api/interviews/{target_id}/send-invite"
        first = a.post(url, headers=ha, data=values)
        assert first.status_code == 200, first.text
        with factory() as session:
            notification = session.get(Notification, first.json()["notification_id"])
            assert notification.status == NotificationStatus.FAILED
            nid, target_id = notification.id, notification.target_id
            original = deepcopy(notification.provider_response)
        if transfer:
            with factory.begin() as session:
                assign_data(session, session.get(User, bid), source=session.get(User, aid))
        return {"a": a, "b": b, "ha": ha, "hb": hb, "factory": factory, "aid": aid, "bid": bid,
                "kind": kind, "url": url, "values": values, "nid": nid, "target_id": target_id,
                "original": original, "attempts": attempts, "messages": messages, "delivery": delivery}

    return prepare


def confirm(case, **changes):
    values = {"sender": config.smtp_settings(case["bid"])["SMTP_FROM"], "reason": "职责交接后继续处理失败邮件", **changes}
    return case["b"].post(f"/api/notifications/{case['nid']}/authorize-resend", headers=case["hb"], data=values)


@pytest.mark.parametrize("kind", ["offer", "exam", "interview"])
@pytest.mark.parametrize("same_mailbox", [False, True])
def test_handover_confirmation_preserves_history_and_reuses_business_send(handover, kind, same_mailbox):
    case = handover(kind)
    if same_mailbox:
        config.save_smtp_settings(user_id=case["bid"], host="smtp.example.test", port=465, use_ssl=True,
            username="source@example.test", password="synthetic-smtp-secret", from_address="source@example.test")
    assert case["b"].post(case["url"], headers=case["hb"], data=case["values"]).status_code in {409, 422}
    endpoint = f"/api/notifications/{case['nid']}/authorize-resend"
    assert case["a"].post(endpoint, headers=case["ha"], data={"sender": "source@example.test", "reason": "无权操作"}).status_code == 403
    assert case["b"].post(endpoint, data={"sender": "receiver@example.test", "reason": "缺少 CSRF"}).status_code == 403
    page = case["b"].get("/notifications")
    assert endpoint in page.text and "暂不发送" in page.text
    assert confirm(case).status_code == confirm(case).status_code == 200
    assert len(case["attempts"]) == 1 and not case["messages"]
    case["delivery"][0] = True
    sent = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
    repeated = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
    assert sent.status_code == repeated.status_code == 200
    assert sent.json()["notification_id"] == repeated.json()["notification_id"]
    assert len(case["attempts"]) == 2 and len(case["messages"]) == 1
    with case["factory"]() as session:
        old = session.get(Notification, case["nid"])
        new = session.get(Notification, sent.json()["notification_id"])
        assert old.status == NotificationStatus.FAILED and old.owner_user_id == case["bid"]
        assert all(old.provider_response[key] == value for key, value in case["original"].items())
        assert old.provider_response["retry_id"] == new.id
        assert new.status == NotificationStatus.SENT and new.provider_response["smtp_owner_id"] == case["bid"]
        assert new.provider_response["retry_of"] == old.id
        assert (new.recipient, new.subject, new.content) == (old.recipient, old.subject, old.content)
        authorization = session.get(AuditLog, new.provider_response["resend_authorization_id"])
        handover_audit = session.get(AuditLog, new.provider_response["handover_audit_id"])
        assert authorization.actor == f"{case['bid']}:owner-b"
        assert authorization.details["reason"] == "职责交接后继续处理失败邮件"
        assert authorization.details["handover_audit_id"] == handover_audit.id
        assert handover_audit.action == "account.data_transferred" and handover_audit.entity_id == case["bid"]
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "notification.resend_authorized")) == 1
        assert "synthetic-smtp-secret" not in json.dumps([old.provider_response, new.provider_response, authorization.after_data])
    assert confirm(case).status_code == 409


@pytest.mark.parametrize("invalid", ["blank_reason", "wrong_sender", "missing_handover", "wrong_audit", "stale_attempt"])
def test_handover_confirmation_rejects_invalid_authorization(handover, invalid):
    case = handover()
    values = {"reason": " "} if invalid == "blank_reason" else {"sender": "stale@example.test"} if invalid == "wrong_sender" else {}
    with case["factory"].begin() as session:
        old = session.get(Notification, case["nid"])
        if invalid == "missing_handover":
            old.provider_response = case["original"]
        elif invalid == "wrong_audit":
            audit = session.scalar(select(AuditLog).where(AuditLog.action == "account.created"))
            old.provider_response = {**old.provider_response, "current_handover_audit_id": audit.id}
        elif invalid == "stale_attempt":
            session.add(Notification(owner_user_id=case["bid"], channel="email", status=NotificationStatus.FAILED,
                content=old.content, target_type=old.target_type, target_id=old.target_id, template=old.template))
    assert confirm(case, **values).status_code == 409
    assert len(case["attempts"]) == 1
    with case["factory"]() as session:
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "notification.resend_authorized")) == 0


@pytest.mark.parametrize("status", [NotificationStatus.UNKNOWN, NotificationStatus.SENDING, NotificationStatus.SENT])
def test_handover_cannot_authorize_nonfailed_delivery(handover, status):
    case = handover()
    with case["factory"].begin() as session:
        session.get(Notification, case["nid"]).status = status
    assert confirm(case).status_code == 409
    assert len(case["attempts"]) == 1
    if status == NotificationStatus.UNKNOWN:
        verified = case["b"].post(f"/api/notifications/{case['nid']}/verify", headers=case["hb"],
                                 data={"delivered": "false", "note": "虚构收件人确认未收到"})
        assert verified.status_code == 200
        assert confirm(case).status_code == 200


def test_sender_change_after_confirmation_requires_new_authorization(handover):
    case = handover()
    assert confirm(case).status_code == 200
    config.save_smtp_settings(user_id=case["bid"], host="smtp.example.test", port=465, use_ssl=True,
        username="new@example.test", password="synthetic-smtp-secret", from_address="new@example.test")
    assert case["b"].post(case["url"], headers=case["hb"]).status_code == 422
    assert len(case["attempts"]) == 1
    assert confirm(case).status_code == 200


@pytest.mark.parametrize("kind", ["offer", "exam", "interview"])
def test_authorization_does_not_bypass_business_guards(handover, kind):
    case = handover(kind)
    assert confirm(case).status_code == 200
    with case["factory"].begin() as session:
        if kind == "offer":
            session.get(Offer, case["target_id"]).expires_at = utc_now() - timedelta(days=1)
        elif kind == "exam":
            dispatch = session.get(ExamDispatch, case["target_id"])
            session.get(ExamResource, dispatch.resource_id).active = False
        else:
            session.get(Interview, case["target_id"]).booking_token_expires_at = utc_now() - timedelta(days=1)
    assert case["b"].post(case["url"], headers=case["hb"], data=case["values"]).status_code in {409, 422}
    assert len(case["attempts"]) == 1


def test_concurrent_confirm_and_send_do_not_duplicate_mail(handover):
    case = handover()
    with ThreadPoolExecutor(2) as pool:
        assert all(r.status_code == 200 for r in pool.map(lambda _: confirm(case), range(2)))
    case["delivery"][0] = True
    with ThreadPoolExecutor(2) as pool:
        assert all(r.status_code == 200 for r in pool.map(
            lambda _: case["b"].post(case["url"], headers=case["hb"]), range(2)))
    assert len(case["messages"]) == 1 and len(case["attempts"]) == 2


def test_second_handover_requires_receiver_to_confirm_again(handover):
    case = handover()
    assert confirm(case).status_code == 200
    # 再交接回原账号，旧授权必须不能替代本次交接确认。
    with case["factory"].begin() as session:
        assign_data(session, session.get(User, case["aid"]), source=session.get(User, case["bid"]))
    config.save_smtp_settings(user_id=case["aid"], host="smtp.example.test", port=465, use_ssl=True,
        username="receiver@example.test", password="synthetic-smtp-secret", from_address="receiver@example.test")
    assert case["a"].post(case["url"], headers=case["ha"]).status_code == 422
    result = case["a"].post(f"/api/notifications/{case['nid']}/authorize-resend", headers=case["ha"],
                           data={"sender": "receiver@example.test", "reason": "再次交接"})
    assert result.status_code == 200


@pytest.mark.parametrize("kind", ["offer", "exam", "interview"])
@pytest.mark.parametrize("record_type, current_filter", [("autumn", "internship"), ("internship", "autumn")])
def test_handover_retry_link_reaches_record_across_recruitment_filters(handover, kind, record_type, current_filter):
    case = handover(kind)
    with case["factory"].begin() as session:
        model = {"offer": Offer, "exam": ExamDispatch, "interview": Interview}[kind]
        session.get(model, case["target_id"]).application.job.recruitment_type = record_type
    page = case["b"].get("/notifications?recruitment_type=" + current_filter)
    href = unescape(re.search(r'<a href="([^"]+)">回原业务页面重试</a>', page.text)[1])
    target = case["b"].get(href)
    assert target.status_code == 200
    if kind == "exam":
        assert "暂无发放记录" not in target.text
    else:
        assert f'action="{case["url"]}"' in target.text
    foreign = case["a"].get(href)
    assert foreign.status_code == 403 or (
        "暂无发放记录" in foreign.text if kind == "exam" else f'action="{case["url"]}"' not in foreign.text)


@pytest.mark.parametrize("kind", ["offer", "exam", "interview"])
def test_repeated_handover_failures_keep_history_and_allow_fresh_confirmation(handover, kind):
    case = handover(kind)
    assert confirm(case).status_code == 200
    previous_id = case["nid"]
    with case["factory"]() as session:
        handover_id = session.get(Notification, previous_id).current_handover_audit_id
    for _ in range(3):
        response = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
        assert response.status_code == 200
        latest_id = response.json()["notification_id"]
        with case["factory"]() as session:
            previous, latest = session.get(Notification, previous_id), session.get(Notification, latest_id)
            assert latest.status == NotificationStatus.FAILED
            assert latest.provider_response["handover_audit_id"] == handover_id
            assert latest.provider_response["retry_of"] == previous.id
            assert previous.provider_response["retry_id"] == latest.id
            assert (latest.recipient, latest.subject, latest.content) == (previous.recipient, previous.subject, previous.content)
        previous_id = latest_id
    assert confirm(case).status_code == 409
    case["nid"] = latest_id
    assert f'/api/notifications/{latest_id}/authorize-resend' in case["b"].get("/notifications").text
    config.save_smtp_settings(user_id=case["bid"], host="smtp.example.test", port=465, use_ssl=True,
        username="changed@example.test", password="synthetic-smtp-secret", from_address="changed@example.test")
    assert case["b"].post(case["url"], headers=case["hb"], data=case["values"]).status_code in {409, 422}
    assert len(case["attempts"]) == 4
    assert confirm(case).status_code == 200
    case["delivery"][0] = True
    sent = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
    repeated = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
    assert sent.status_code == repeated.status_code == 200
    assert sent.json()["notification_id"] == repeated.json()["notification_id"]
    assert len(case["attempts"]) == 5 and len(case["messages"]) == 1
    with case["factory"]() as session:
        latest = session.get(Notification, sent.json()["notification_id"])
        assert latest.status == NotificationStatus.SENT
        assert latest.provider_response["smtp_from"] == "changed@example.test"
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "notification.resend_authorized")) == 2


@pytest.mark.parametrize("kind", ["offer", "exam", "interview"])
@pytest.mark.parametrize("already_assigned", [False, True])
def test_legacy_assignment_cli_allows_controlled_resend(handover, monkeypatch, kind, already_assigned):
    case = handover(kind, transfer=False)
    with case["factory"].begin() as session:
        for table in Base.metadata.sorted_tables:
            if "owner_user_id" in table.c:
                session.execute(update(table).values(owner_user_id=None))
    engine = case["factory"].kw["bind"]
    monkeypatch.setattr(accounts, "engine", engine)
    monkeypatch.setattr(accounts, "SessionLocal", case["factory"])
    monkeypatch.setattr(accounts, "init_db", lambda: init_db(engine))
    command = ["accounts", "assign-legacy", "owner-b"]
    monkeypatch.setattr(sys, "argv", command)
    accounts.main()
    if already_assigned:
        # 模拟旧版本已分配但未记录邮件来源，普通 assign-legacy 不得自动补授权。
        with case["factory"].begin() as session:
            session.get(Notification, case["nid"]).provider_response = case["original"]
        accounts.main()
        assert confirm(case).status_code == 409
        with case["factory"].begin() as session:
            unassigned = Notification(channel="email", status=NotificationStatus.FAILED, content="未分配对照",
                                      provider_response=case["original"])
            session.add(unassigned)
            session.flush()
            unassigned_id = unassigned.id
        monkeypatch.setattr(sys, "argv", command + ["--confirm-assigned-mail", str(case["nid"])])
        accounts.main()
    assert case["b"].post(case["url"], headers=case["hb"], data=case["values"]).status_code in {409, 422}
    assert confirm(case).status_code == 200
    assert len(case["attempts"]) == 1
    with case["factory"]() as session:
        old = session.get(Notification, case["nid"])
        assert all(old.provider_response[key] == value for key, value in case["original"].items())
        audit = session.get(AuditLog, old.current_handover_audit_id)
        assert audit.action == "account.legacy_assigned" and audit.entity_id == case["bid"]
        if already_assigned:
            assert audit.after_data["confirmed_notification_ids"] == [case["nid"]]
            assert audit.after_data["counts"] == {}
            untouched = session.get(Notification, unassigned_id)
            assert untouched.owner_user_id is None and untouched.provider_response == case["original"]
    case["delivery"][0] = True
    sent = case["b"].post(case["url"], headers=case["hb"], data=case["values"])
    assert sent.status_code == 200 and len(case["messages"]) == 1
    with case["factory"]() as session:
        assert session.get(Notification, sent.json()["notification_id"]).status == NotificationStatus.SENT


@pytest.mark.parametrize("invalid", ["missing", "foreign_owner", "unassigned", "unknown", "sent", "channel", "already_marked"])
def test_legacy_mail_confirmation_rejects_invalid_records_atomically(handover, invalid):
    case = handover()
    with case["factory"].begin() as session:
        old = session.get(Notification, case["nid"])
        if invalid != "already_marked":
            old.provider_response = case["original"]
        if invalid in {"foreign_owner", "unassigned"}:
            old.owner_user_id = case["aid"] if invalid == "foreign_owner" else None
        elif invalid in {"unknown", "sent"}:
            old.status = NotificationStatus(invalid)
        elif invalid == "channel":
            old.channel = "wecom"
        valid = Notification(owner_user_id=case["bid"], channel="email", status=NotificationStatus.FAILED,
                             content="虚构补录对照", provider_response=case["original"])
        session.add(valid)
        session.flush()
        valid_id = valid.id
    with pytest.raises(ValueError), case["factory"].begin() as session:
        assign_data(session, session.get(User, case["bid"]),
                    confirmed_mail_ids=[valid_id, 999999 if invalid == "missing" else case["nid"]])
    with case["factory"]() as session:
        assert session.get(Notification, valid_id).provider_response == case["original"]
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "account.legacy_assigned")) == 0


def test_return_transfer_keeps_the_handover_used_by_each_attempt(handover):
    case = handover()
    assert confirm(case).status_code == 200
    for _ in range(2):
        assert case["b"].post(case["url"], headers=case["hb"]).status_code == 200
    query = select(Notification).where(Notification.channel == "email").order_by(Notification.id)
    with case["factory"].begin() as session:
        attempts = session.scalars(query).all()
        assert len(attempts) == 3
        history = [{k: v for k, v in n.provider_response.items() if k != "current_handover_audit_id"} for n in attempts]
        assert "handover_audit_id" not in history[0]
        authorization = session.get(AuditLog, attempts[1].provider_response["resend_authorization_id"])
        original_handover = authorization.details["handover_audit_id"]
        assert all(n.provider_response["handover_audit_id"] == original_handover for n in attempts[1:])
        assign_data(session, session.get(User, case["aid"]), source=session.get(User, case["bid"]))
    with case["factory"]() as session:
        attempts = session.scalars(query).all()
        assert [{k: v for k, v in n.provider_response.items() if k != "current_handover_audit_id"} for n in attempts] == history
        current = session.scalar(select(func.max(AuditLog.id)).where(AuditLog.action == "account.data_transferred"))
        assert current != original_handover
        assert all(n.owner_user_id == case["aid"] and n.current_handover_audit_id == current for n in attempts)


@pytest.mark.parametrize("scenario", ["legacy_backfill", "previous_version", "return_to_sender"])
def test_first_retry_requires_confirmation_for_this_handover(handover, scenario):
    case = handover()
    client, headers, owner = case["b"], case["hb"], case["bid"]
    if scenario == "return_to_sender":
        assert confirm(case).status_code == 200
        with case["factory"].begin() as session:
            assign_data(session, session.get(User, case["aid"]), source=session.get(User, owner))
        client, headers, owner = case["a"], case["ha"], case["aid"]
    else:
        with case["factory"].begin() as session:
            old = session.get(Notification, case["nid"])
            old.provider_response = {"mode": "smtp", "transport": "ssl"}
            if scenario == "legacy_backfill":
                assign_data(session, session.get(User, owner), confirmed_mail_ids=[old.id])
            else:
                audit = session.scalar(select(AuditLog).where(AuditLog.action == "account.data_transferred"))
                old.provider_response = {**old.provider_response, "handover_audit_id": audit.id}
    # 在创建发送记录之前拦住任何未确认的尝试，测试不允许它到达 SMTP。
    with patch.object(Notification, "__init__", side_effect=AssertionError("尚未确认就开始新发送")):
        assert client.post(case["url"], headers=headers).status_code == 422
    assert len(case["attempts"]) == 1
    confirmed = client.post(f"/api/notifications/{case['nid']}/authorize-resend", headers=headers,
        data={"sender": config.smtp_settings(owner)["SMTP_FROM"], "reason": "核实本次交接"})
    assert confirmed.status_code == 200 and len(case["attempts"]) == 1
    case["delivery"][0] = True
    sent = client.post(case["url"], headers=headers)
    assert sent.status_code == 200 and len(case["messages"]) == 1
    with case["factory"]() as session:
        new = session.get(Notification, sent.json()["notification_id"])
        authorization = session.get(AuditLog, new.provider_response["resend_authorization_id"])
        assert new.provider_response["handover_audit_id"] == authorization.details["handover_audit_id"]


def test_older_notification_stays_reachable_after_confirming(handover):
    case = handover()
    now = utc_now()
    with case["factory"].begin() as session:
        session.add_all(Notification(owner_user_id=case["bid"], channel="internal", status=NotificationStatus.SENT,
                                     content="后续通知", created_at=now) for _ in range(200))
        session.add(Notification(owner_user_id=case["bid"], channel="internal", status=NotificationStatus.SENT,
                                 content="回填的历史通知", created_at=now - timedelta(days=1)))
    endpoint = f'/api/notifications/{case["nid"]}/authorize-resend'
    first = case["b"].get("/notifications")
    assert endpoint not in first.text and "回填的历史通知" not in first.text
    older = re.search(r'<a[^>]+href="([^"]+)"[^>]*>更早的通知</a>', first.text)
    assert older, "第 201 条通知无法从列表打开"
    link = unescape(older[1])
    older_page = case["b"].get(link)
    assert endpoint in older_page.text and "回填的历史通知" in older_page.text
    assert case["a"].get(link).status_code == 404
    confirmed = confirm(case)
    assert confirmed.status_code == 200
    assert endpoint in case["b"].get(confirmed.json()["redirect"]).text


def test_old_offer_retry_opens_exact_detail_beyond_list_limit(handover):
    case = handover()
    with account_scope(case["bid"]), case["factory"]() as session:
        newer = application(session, ApplicationStatus.OFFER_PENDING)
        newer.job.recruitment_type = "internship"
        session.add_all(Offer(application_id=newer.id, candidate_name="较新实习", job_title="实习", content="虚构条款")
                        for _ in range(200))
        session.commit()
    assert confirm(case).status_code == 200
    html = case["b"].get("/notifications?recruitment_type=internship").text
    href = unescape(re.search(r'<a href="([^"]+)">回原业务页面重试</a>', html)[1])
    target = case["b"].get(href)
    button = f'action="{case["url"]}"'
    assert button in case["b"].get(f'/offers?offer_id={case["target_id"]}&recruitment_type=autumn').text
    assert target.status_code == 200 and button in target.text
    assert case["a"].get(href).status_code == 403
