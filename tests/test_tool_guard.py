import asyncio
import smtplib

import pytest
from sqlalchemy import select

from app.core import tool_guard
from app.core.approvals import decide
from app.core.config import settings
from app.core.orchestrator import create_run
from app.core.tool_guard import (Risk, TOOLS, ToolError, ToolNotAllowed, ToolSpec, execute_approved,
                                 request_action)
from app.db.models import Approval, Event, Message, Task
from app.db.session import SessionLocal
from app.schemas.contracts import Plan, PlanTask


def setup_run(to="owner@clinic.example"):
    with SessionLocal() as db:
        rid = create_run(db, "x", {}, Plan(objective="x", tasks=[PlanTask(task_key="t_01", agent="communication")])).id
        m = Message(run_id=rid, to=to, subject="Hello there", body="A body that is long enough to pass.", status="draft")
        db.add(m)
        db.commit()
        mid = m.id
    return rid, {"to": to, "subject": "Hello there", "body": "A body that is long enough to pass.", "message_id": mid}


def guard(rid, payload, tool="send_email"):
    return asyncio.run(request_action(rid, "t_01", "communication", tool, payload))


class FakeSMTP:
    sent, instances = [], []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.login_args = host, port, False, None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, pw):
        self.login_args = (user, pw)

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp_spy(monkeypatch):
    FakeSMTP.sent, FakeSMTP.instances = [], []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def smtp_forbidden(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("SMTP must never be used in dry-run mode")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)


def test_every_tool_has_a_risk_level_and_outside_world_tools_are_high():
    assert all(isinstance(t.risk, Risk) for t in TOOLS.values())
    assert TOOLS["send_email"].risk is Risk.HIGH and TOOLS["create_event"].risk is Risk.HIGH
    assert TOOLS["fetch_url"].risk is Risk.LOW and TOOLS["web_search"].risk is Risk.LOW


def test_send_email_creates_pending_approval_and_returns_immediately(smtp_forbidden):
    rid, payload = setup_run()
    d = guard(rid, payload)
    assert d.pending and d.approval_id and d.result is None
    with SessionLocal() as db:
        a = db.get(Approval, d.approval_id)
        assert a.status == "pending" and a.action_type == "send_email" and a.payload_json["subject"] == "Hello there"
        assert db.get(Task, f"{rid}:t_01").status == "awaiting_approval"
        assert db.get(Message, payload["message_id"]).status == "draft"          # nothing was sent
        ev = [e for e in db.scalars(select(Event).where(Event.run_id == rid)) if e.type == "approval_requested"]
        assert len(ev) == 1 and ev[0].payload_json["approval_id"] == d.approval_id and ev[0].payload_json["risk"] == "high"


def test_unknown_tool_is_denied():
    rid, payload = setup_run()
    with pytest.raises(ToolNotAllowed):
        guard(rid, payload, tool="delete_database")


def test_low_risk_tool_runs_immediately_without_approval(monkeypatch):
    async def ping(payload):
        return {"pong": payload["x"]}
    monkeypatch.setitem(TOOLS, "ping", ToolSpec("ping", Risk.LOW, "test tool", ping))
    rid, _ = setup_run()
    d = guard(rid, {"x": 7}, tool="ping")
    assert d.status == "executed" and d.result == {"pong": 7} and d.approval_id is None
    with SessionLocal() as db:
        assert db.scalars(select(Approval).where(Approval.run_id == rid)).first() is None


def test_execute_refuses_an_approval_that_is_not_approved():
    rid, payload = setup_run()
    d = guard(rid, payload)
    with pytest.raises(ToolError, match="not approved"):
        asyncio.run(execute_approved(d.approval_id))
    with SessionLocal() as db:
        decide(db, d.approval_id, "reject", note="no")
    with pytest.raises(ToolError, match="not approved"):
        asyncio.run(execute_approved(d.approval_id))


def test_dry_run_never_touches_smtp_and_marks_the_message(monkeypatch, smtp_forbidden):
    monkeypatch.setattr(settings, "dry_run", True)
    rid, payload = setup_run()
    d = guard(rid, payload)
    with SessionLocal() as db:
        decide(db, d.approval_id, "approve")
    res = asyncio.run(execute_approved(d.approval_id))
    assert res["delivery"] == "dry_run" and res["status"] == "sent (dry run)"
    with SessionLocal() as db:
        assert db.get(Message, payload["message_id"]).status == "sent (dry run)"


def test_live_send_goes_through_smtp(monkeypatch, smtp_spy):
    monkeypatch.setattr(settings, "dry_run", False)
    for k, v in dict(smtp_host="smtp.test", smtp_port=587, smtp_from="me@agency.example",
                     smtp_user="u", smtp_password="p").items():
        monkeypatch.setattr(settings, k, v)
    rid, payload = setup_run()
    d = guard(rid, payload)
    with SessionLocal() as db:
        decide(db, d.approval_id, "approve")
    res = asyncio.run(execute_approved(d.approval_id))
    assert res["delivery"] == "smtp" and res["status"] == "sent"
    msg = smtp_spy.sent[0]
    assert msg["To"] == "owner@clinic.example" and msg["From"] == "me@agency.example" and msg["Subject"] == "Hello there"
    assert smtp_spy.instances[0].tls and smtp_spy.instances[0].login_args == ("u", "p")
    with SessionLocal() as db:
        assert db.get(Message, payload["message_id"]).status == "sent"


def test_a_message_is_never_sent_twice(monkeypatch, smtp_spy):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "me@agency.example")
    rid, payload = setup_run()
    d = guard(rid, payload)
    with SessionLocal() as db:
        decide(db, d.approval_id, "approve")
    asyncio.run(execute_approved(d.approval_id))
    again = asyncio.run(execute_approved(d.approval_id))
    assert again["already_sent"] and len(smtp_spy.sent) == 1


@pytest.mark.parametrize("to,host,msg", [("not-an-email", "smtp.test", "valid recipient"),
                                          ("", "smtp.test", "valid recipient"),
                                          ("owner@clinic.example", "", "not configured")])
def test_live_send_refuses_bad_recipient_or_missing_smtp(monkeypatch, smtp_spy, to, host, msg):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "smtp_host", host)
    monkeypatch.setattr(settings, "smtp_from", "me@agency.example")
    rid, payload = setup_run(to="owner@clinic.example")
    payload["to"] = to
    d = guard(rid, payload)
    with SessionLocal() as db:
        decide(db, d.approval_id, "approve")
    with pytest.raises(ToolError, match=msg):
        asyncio.run(execute_approved(d.approval_id))
    assert smtp_spy.sent == []
