"""Phase 5 checkpoint, end to end through the HTTP API with the REAL agents (fake internet, fake LLM):
   start -> approval -> report with no manual fixes;  and a bad URL -> a report that lists the failure."""
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import llm_gateway
from app.core.config import settings
from app.db.models import Followup, Lead, Message, Run
from app.db.session import SessionLocal
from app.main import app
from tests.test_agents_info import AUDIT, LEAD, PROFILE

pytestmark = pytest.mark.real_agents          # keep the real research/website/scoring agents


class Router:
    """One fake LLM for every agent in the pipeline, routed by the system prompt."""
    def __init__(self, monkeypatch, claims=None):
        self.calls, self.claims = [], claims or {"supported": True, "unsupported_claims": []}
        monkeypatch.setattr(llm_gateway, "_call_provider", self.call)

    async def call(self, messages):
        sysmsg, user = messages[0]["content"], messages[1]["content"]
        self.calls.append(sysmsg[:40])
        if "Manager" in sysmsg:
            raise llm_gateway.LLMError("manager disabled: the default plan is used")
        if "website auditor" in sysmsg:
            r = AUDIT
        elif "business researcher" in sysmsg:
            r = PROFILE
        elif "fact-checker" in sysmsg:
            r = self.claims
        elif "outreach emails" in sysmsg:
            slots = re.findall(r"^- (\w{3} \d{1,2} \w{3}, \d\d:\d\d)$", user, re.M)
            r = {"subject": "A quick idea for Smile Bright Dental",
                 "body": "Hi there,\n\nI noticed Smile Bright Dental has no online booking, so after-hours patients cannot "
                         "book. We set up simple booking. Times: " + "; ".join(slots) + ".\n\nIf this isn't relevant, just "
                         "reply 'no thanks' and I won't contact you again.\n\nSam Carter\nFounder, Brightpath Digital",
                 "proposal_md": "## Proposal for Smile Bright Dental\n\n### What we noticed\nNo online booking.\n\n"
                                "### What we would do\nAdd booking.\n\n### Next step\nA call."}
        else:
            r = LEAD
        return json.dumps(r), 100


@pytest.fixture
def client(monkeypatch, mock_web):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(settings, "dry_run", True)
    with TestClient(app) as c:
        yield c


def start(c, url="https://dental.example"):
    r = c.post("/api/runs", json={"objective": "Assess this company as a sales lead and draft outreach",
                                  "input": {"url": url, "recipient_email": "me@myown.example"}})
    assert r.status_code == 202
    return r.json()["run_id"]


def tasks_of(c, rid):
    return {t["agent"]: t for t in c.get(f"/api/runs/{rid}").json()["tasks"]}


def test_one_full_run_start_to_approval_to_report(client, monkeypatch):
    router = Router(monkeypatch)
    rid = start(client)

    run = client.get(f"/api/runs/{rid}").json()
    t = tasks_of(client, rid)
    assert run["status"] == "awaiting_approval" and run["report_md"] is None and run["finished_at"] is None
    assert t["scheduling"]["status"] == "succeeded" and len(t["scheduling"]["output_json"]["proposal"]["slots"]) == 3
    assert t["scheduling"]["output_json"]["event"]["status"] == "logged (dry run)"        # DRY_RUN: logged, not created
    assert t["communication"]["status"] == "awaiting_approval"
    assert all(t[a]["status"] == "pending" for a in ("followup", "verification", "report"))

    (a,) = client.get("/api/approvals", params={"status": "pending", "run_id": rid}).json()
    body = a["payload_json"]["body"]
    assert all(s["label"] in body for s in t["scheduling"]["output_json"]["proposal"]["slots"])   # the email offers the slots
    assert a["payload_json"]["verification"]["passed"] and a["payload_json"]["verification"]["claims"]["checked"]

    assert client.post(f"/api/approvals/{a['id']}/decision", json={"decision": "approve"}).status_code == 200

    run = client.get(f"/api/runs/{rid}").json()
    t = tasks_of(client, rid)
    assert run["status"] == "succeeded" and run["finished_at"] is not None
    assert all(x["status"] == "succeeded" for x in t.values()) and len(t) == 8
    assert t["verification"]["output_json"]["passed"] and "reused" in t["verification"]["output_json"]["claims"]["reason"]

    md = run["report_md"]                                  # saved on the run
    for section in ("# Outreach report: Smile Bright Dental", "## Lead", "## Key findings", "## Actions taken",
                    "## Pending items", "## Verification", "## Failures"):
        assert section in md
    assert "Score 82/100, tier A" in md.replace("**", "") and "No online booking" in md
    assert "Email delivery status: **sent (dry run)**" in md and "Human review of draft revision 0: **approved**" in md
    assert "Scheduled 3 follow-up action(s)" in md and "Follow-up due" in md and "None. Every task succeeded." in md
    assert "6 of 6 checks passed" in md

    with SessionLocal() as db:
        fu = list(db.scalars(select(Followup).where(Followup.run_id == rid)))
        assert len(fu) == 3 and {f.status for f in fu} == {"pending"}                      # tier A -> 3 follow-ups
        assert db.scalars(select(Lead).where(Lead.run_id == rid)).one().score == 82
        assert [m.status for m in db.scalars(select(Message).where(Message.run_id == rid))] == ["sent (dry run)"]
        r = db.get(Run, rid)
        assert r.llm_calls == len(router.calls) == 6 and r.llm_calls <= settings.max_llm_calls_per_run
    assert client.get(f"/api/runs/{rid}/followups").json()[0]["status"] == "pending"


def test_a_bad_url_still_ends_with_a_report_that_lists_the_failure(client, monkeypatch):
    Router(monkeypatch)
    rid = start(client, url="https://dead.example/")
    run = client.get(f"/api/runs/{rid}").json()
    t = tasks_of(client, rid)
    assert run["status"] == "failed" and run["finished_at"] is not None
    assert t["research"]["status"] == t["website"]["status"] == "failed"
    assert all(t[a]["status"] == "skipped" for a in ("scoring", "scheduling", "communication", "followup", "verification"))
    assert t["report"]["status"] == "succeeded"                                        # the report ALWAYS runs
    md = run["report_md"]
    assert md and "# Outreach report: dead.example" in md
    assert "finished with 7 problem(s)" in md
    assert "## Failures" in md and "**t_01 research** is **failed**" in md and "HTTP 404" in md
    assert "**t_02 website** is **failed**" in md and "**t_05 communication** is **skipped**" in md
    assert "No lead score was produced" in md and "No research or website findings" in md
    assert "None. Every task succeeded." not in md
    assert client.get("/api/approvals", params={"run_id": rid}).json() == []           # nothing was drafted or sent
    assert client.get(f"/api/runs/{rid}/followups").json() == []


def test_a_blocked_address_is_reported_the_same_way(client, monkeypatch):
    Router(monkeypatch)
    rid = start(client, url="http://127.0.0.1:8000")
    md = client.get(f"/api/runs/{rid}").json()["report_md"]
    assert "Blocked" in md and client.get(f"/api/runs/{rid}").json()["status"] == "failed"
