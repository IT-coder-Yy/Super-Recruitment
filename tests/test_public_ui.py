from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app.workbench import appointment_time


templates = Environment(
    loader=FileSystemLoader(Path(__file__).parents[1] / "app" / "templates"),
    autoescape=select_autoescape(["html"]),
)
templates.filters["appointment_time"] = appointment_time


def make_offer(status="sent"):
    return SimpleNamespace(
        id=7, job_title="后端工程师", candidate_name="候选人", content="录用内容 <script>bad()</script>",
        report_time=None, report_location=None, office_location=None,
        status=SimpleNamespace(value=status),
    )


@pytest.mark.parametrize("name,context", [
    ("login.html", {"csrf_token": "token", "registered": False}),
    ("register.html", {"csrf_token": "token"}),
    ("booking.html", {"token": "booking-token", "slots": []}),
    ("booking_success.html", {}),
    ("offer_public.html", {"token": "offer-token", "offer": make_offer()}),
])
def test_public_pages_render_shared_theme_and_main_landmark(name, context):
    html = templates.get_template(name).render(**context)
    assert html.count('id="main-content"') == 1
    assert '<script src="/static/js/theme.js"></script>' in html
    assert '/static/css/theme.css' in html and '/static/css/public.css' in html
    for theme in ("light", "dark", "system"):
        assert f'data-theme-choice="{theme}"' in html


@pytest.mark.parametrize("name,action,autocomplete", [
    ("login.html", "/login", "current-password"),
    ("register.html", "/register", "new-password"),
])
def test_auth_forms_preserve_csrf_credentials_and_safe_error_rendering(name, action, autocomplete):
    html = templates.get_template(name).render(
        csrf_token="csrf-value", username='user"><script>bad()</script>', error="请重试", registered=False,
    )
    assert f'method="post" action="{action}"' in html
    assert 'name="csrf_token" value="csrf-value"' in html
    assert 'name="username"' in html and 'name="password" type="password"' in html
    assert f'autocomplete="{autocomplete}"' in html
    assert '<script>bad()</script>' not in html
    assert 'role="alert" tabindex="-1" data-error-summary' in html


def test_booking_keeps_slot_submission_and_handles_cross_day_and_empty_states():
    start = datetime(2026, 10, 24, 23, 30, tzinfo=timezone(timedelta(hours=8)))
    slot = SimpleNamespace(id=12, start_at=start, end_at=start + timedelta(hours=1))
    template = templates.get_template("booking.html")
    html = template.render(token="booking-token", slots=[slot])
    assert 'method="post" action="/schedule/booking-token/confirm"' in html
    assert 'name="slot_id" value="12"' in html
    assert '23:30 – 10月25日 00:30' in html
    assert '北京时间（UTC+8）' in html
    assert '暂无可用时段' in template.render(token="booking-token", slots=[])
    invalid = template.render(token="booking-token", slots=[slot], error="链接已过期")
    assert '链接已过期' in invalid and 'name="slot_id"' not in invalid


@pytest.mark.parametrize("start", [
    datetime(2026, 10, 24, 9, 0),
    datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 24, 9, 0, tzinfo=timezone(timedelta(hours=8))),
])
def test_booking_normalizes_display_time_without_shifting_sqlite_local_values(start):
    slot = SimpleNamespace(id=1, start_at=start, end_at=start + timedelta(hours=1))
    html = templates.get_template("booking.html").render(token="test", slots=[slot])
    assert "09:00 – 10:00" in html
    assert '2026-10-24T09:00:00+08:00' in html


@pytest.mark.parametrize("status,show_actions", [("sent", True), ("accepted", False), ("declined", False), ("expired", False)])
def test_offer_response_contract_and_content_escaping(status, show_actions):
    html = templates.get_template("offer_public.html").render(token="offer-token", offer=make_offer(status))
    assert '<script>bad()</script>' not in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html
    assert ('action="/offer/offer-token/respond"' in html) is show_actions
    if show_actions:
        assert 'name="accepted" value="true"' in html
        assert 'name="accepted" value="false"' in html
    else:
        assert '如需调整，请联系 HR' in html
