"""End to end through the HTTP API: pause at the email, then approve | edit | reject."""
import json
import re
import smtplib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import llm_gateway
from app.core.config import settings
from app.db.models import Event, Message
from app.db.session import SessionLocal
from app.main import app
from tests.test_communication import DRAFT as _SMILE

# the pipeline's stub lead is "Acme Dental": the email must name the company it is about
DRAFT = {k: v.replace("Smile Bright Dental", "Acme Dental").replace("Smile Bright", "Acme") for k, v in _SMILE.items()}


@pytest.fixture(autouse=True)
def _never_smtp(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("SMTP must not be used while DRY_RUN=true")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    monkeypatch.setattr(settings, "dry_run", True)


class CommLLM:
    """Scripted replies for the Communication agent only; the Manager is forced onto its fallback plan."""
    def __init__(self, monkeypatch, drafts):
        self.drafts, self.calls = list(drafts), []
        monkeypatch.setattr(llm_gateway, "_call_provider", self._call)

    async def _call(self, messages):
        if "Manager" in messages[0]["content"]:
            raise llm_gateway.LLMError("manager disabled in this test")
        self.calls.append(messages)
        d = dict(self.drafts.pop(0))
        slots = re.findall(r"^- (\w{3} \d{1,2} \w{3}, \d\d:\d\d)$", messages[1]["content"], re.M)
        if slots:                                  # like a real model: offer every slot, labels copied exactly
            d["body"] += "\n\nI could do: " + "; ".join(slots) + "."
        return json.dumps(d), 100


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def start(c, **extra):
    r = c.post("/api/runs", json={"objective": "Assess this company and draft outreach",
                                  "input": {"url": "https://dental.example", **extra}})
    assert r.status_code == 202
    return r.json()["run_id"]


def pending(c, run_id):
    return c.get("/api/approvals", params={"status": "pending", "run_id": run_id}).json()


def decide(c, approval_id, **body):
    return c.post(f"/api/approvals/{approval_id}/decision", json=body)


def run_of(c, run_id):
    return c.get(f"/api/runs/{run_id}").json()


def types(run_id):
    with SessionLocal() as db:
        return [e.type for e in db.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))]


def messages(run_id):
    with SessionLocal() as db:
        return list(db.scalars(select(Message).where(Message.run_id == run_id).order_by(Message.id)))


def test_run_stops_at_awaiting_approval_and_shows_the_draft(client):
    rid = start(client)
    run = run_of(client, rid)
    assert run["status"] == "awaiting_approval" and run["finished_at"] is None
    (a,) = pending(client, rid)
    assert a["action_type"] == "send_email" and a["payload_json"]["subject"] and a["payload_json"]["body"]
    comm = next(t for t in run["tasks"] if t["agent"] == "communication")
    assert comm["status"] == "awaiting_approval" and comm["output_json"]["draft"]["subject"] == a["payload_json"]["subject"]
    assert [m.status for m in messages(rid)] == ["draft"]                      # drafted, NOT sent
    assert "approval_requested" in types(rid) and "run_succeeded" not in types(rid)


def test_approve_resumes_the_run_and_finishes_in_dry_run(client):
    rid = start(client)
    (a,) = pending(client, rid)
    r = decide(client, a["id"], decision="approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    run = run_of(client, rid)
    assert run["status"] == "succeeded" and all(t["status"] == "succeeded" for t in run["tasks"])
    comm = next(t for t in run["tasks"] if t["agent"] == "communication")
    assert comm["output_json"]["delivery"]["status"] == "sent (dry run)" and comm["output_json"]["approved_by"] == "human"
    assert [m.status for m in messages(rid)] == ["sent (dry run)"]
    t = types(rid)
    assert t.index("approval_requested") < t.index("approval_decided") < t.index("action_executed") < t.index("run_succeeded")
    assert pending(client, rid) == []


def test_edit_sends_the_edited_text(client):
    rid = start(client)
    (a,) = pending(client, rid)
    new_subject, new_body = "My own subject", "Hello, this is my own wording for the email, long enough to pass."
    r = decide(client, a["id"], decision="edit", subject=new_subject, body=new_body, to="owner@clinic.example")
    assert r.status_code == 200
    run = run_of(client, rid)
    assert run["status"] == "succeeded"
    (m,) = messages(rid)
    assert (m.subject, m.body, m.to, m.status) == (new_subject, new_body, "owner@clinic.example", "sent (dry run)")
    done = client.get(f"/api/approvals/{a['id']}").json()
    assert done["payload_json"]["original"]["subject"] == a["payload_json"]["subject"]   # the original is kept
    comm = next(t for t in run["tasks"] if t["agent"] == "communication")
    assert comm["output_json"]["draft"]["subject"] == new_subject


def test_bad_edits_are_rejected_and_the_approval_stays_pending(client):
    rid = start(client)
    (a,) = pending(client, rid)
    assert decide(client, a["id"], decision="edit", subject="").status_code == 422          # too short
    assert decide(client, a["id"], decision="edit", subject="Two\nlines").status_code == 422
    assert decide(client, a["id"], decision="edit", to="not-an-email").status_code == 422
    assert decide(client, a["id"], decision="edit").status_code == 422                       # nothing changed
    assert decide(client, a["id"], decision="maybe").status_code == 422
    assert len(pending(client, rid)) == 1 and run_of(client, rid)["status"] == "awaiting_approval"


def test_reject_produces_one_revised_draft_and_the_run_keeps_waiting(client, monkeypatch):
    revised = {**DRAFT, "subject": "Shorter idea for Acme Dental", "body": DRAFT["body"].replace("Hi there", "Hello")}
    llm = CommLLM(monkeypatch, [DRAFT, revised])
    rid = start(client)
    (a1,) = pending(client, rid)
    assert a1["payload_json"]["subject"] == DRAFT["subject"] and len(llm.calls) == 1
    r = decide(client, a1["id"], decision="reject", note="Too long. Mention the booking problem first.")
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    old = client.get(f"/api/approvals/{a1['id']}").json()
    assert old["status"] == "rejected" and old["reviewer_note"].startswith("Too long")
    (a2,) = pending(client, rid)                                          # a NEW pending approval, revised draft
    assert a2["id"] != a1["id"] and a2["payload_json"]["subject"] == "Shorter idea for Acme Dental"
    assert a2["payload_json"]["revision"] == 1 and len(llm.calls) == 2    # exactly one rewrite
    prompt = llm.calls[1][1]["content"]
    assert "Too long. Mention the booking problem first." in prompt and DRAFT["subject"] in prompt
    assert run_of(client, rid)["status"] == "awaiting_approval"
    assert [m.status for m in messages(rid)] == ["rejected", "draft"]
    assert {"approval_decided", "draft_revision_started"} <= set(types(rid))

    assert decide(client, a2["id"], decision="approve").status_code == 200   # approving the revision finishes the run
    assert run_of(client, rid)["status"] == "succeeded"
    assert [m.status for m in messages(rid)] == ["rejected", "sent (dry run)"]


def test_each_rejection_triggers_exactly_one_rewrite(client, monkeypatch):
    drafts = [DRAFT, {**DRAFT, "subject": "Second try for Acme Dental"}, {**DRAFT, "subject": "Third try for Acme Dental"}]
    llm = CommLLM(monkeypatch, drafts)
    rid = start(client)
    for note in ("not good", "still not good"):
        (a,) = pending(client, rid)
        assert decide(client, a["id"], decision="reject", note=note).status_code == 200
    (last,) = pending(client, rid)
    assert last["payload_json"]["revision"] == 2 and last["payload_json"]["subject"] == "Third try for Acme Dental"
    assert len(llm.calls) == 3                                           # no hidden loops
    all_approvals = client.get("/api/approvals", params={"run_id": rid}).json()
    assert sorted(a["status"] for a in all_approvals) == ["pending", "rejected", "rejected"]


def test_reject_without_an_llm_fails_the_run_with_a_clear_error(client):
    rid = start(client)                                                  # first draft came from the template
    (a,) = pending(client, rid)
    assert decide(client, a["id"], decision="reject", note="shorter please").status_code == 200
    run = run_of(client, rid)
    comm = next(t for t in run["tasks"] if t["agent"] == "communication")
    assert run["status"] == "failed" and comm["status"] == "failed" and "Could not revise" in comm["error"]
    assert pending(client, rid) == []


def test_decisions_cannot_be_repeated_or_aimed_at_nothing(client):
    rid = start(client)
    (a,) = pending(client, rid)
    assert decide(client, a["id"], decision="approve").status_code == 200
    assert decide(client, a["id"], decision="approve").status_code == 409
    assert decide(client, a["id"], decision="reject").status_code == 409
    assert decide(client, "a_nope", decision="approve").status_code == 404
    assert client.get("/api/approvals/a_nope").status_code == 404
    assert len(messages(rid)) == 1 and messages(rid)[0].status == "sent (dry run)"


def test_recipient_override_reaches_the_email(client):
    rid = start(client, recipient_email="me@myown.example")
    (a,) = pending(client, rid)
    assert a["payload_json"]["to"] == "me@myown.example" and a["payload_json"]["recipient_source"] == "override"


def test_live_mode_without_smtp_config_fails_safely(client, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "smtp_host", "")
    rid = start(client, recipient_email="me@myown.example")
    (ev,) = pending(client, rid)                       # live mode: even the calendar hold needs approval
    assert ev["action_type"] == "create_event"
    assert decide(client, ev["id"], decision="approve").status_code == 200
    (a,) = pending(client, rid)
    assert a["action_type"] == "send_email"
    assert decide(client, a["id"], decision="approve").status_code == 200
    run = run_of(client, rid)
    comm = next(t for t in run["tasks"] if t["agent"] == "communication")
    assert run["status"] == "failed" and "SMTP_HOST" in comm["error"]
    assert [m.status for m in messages(rid)] == ["approved"]               # approved but never sent


def test_config_endpoint_reports_dry_run_and_no_secrets(client):
    cfg = client.get("/api/config").json()
    assert cfg == {"dry_run": True}
