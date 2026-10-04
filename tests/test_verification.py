import asyncio
import json

import pytest
from sqlalchemy import select

from app.agents import verification as V
from app.agents.base import AGENTS
from app.agents.communication import communication_agent
from app.agents.verification import VerificationFailed, check_email, company_mentioned, verification_agent
from app.core.config import settings
from app.core.errors import PermanentError
from app.core.llm_gateway import LLMBudgetExceeded, complete_json
from app.core.orchestrator import create_run, run_workflow
from app.db.models import Approval, Event, Message, Run, Task
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask, Plan, PlanTask
from tests.conftest import FakeLLM, approve_all
from tests.test_communication import AUDIT, CTX, DRAFT, LEAD, PROFILE
from tests.test_tool_guard import setup_run
from pydantic import BaseModel

SLOTS = [{"start": "2026-10-06T10:00", "label": "Tue 6 Oct, 10:00"}, {"start": "2026-10-07T14:00", "label": "Wed 7 Oct, 14:00"},
         {"start": "2026-10-08T11:30", "label": "Thu 8 Oct, 11:30"}]
GOOD = {"subject": "A quick idea for Smile Bright Dental",
        "body": "Hi there,\n\nSmile Bright Dental could take bookings online. Times: Tue 6 Oct, 10:00; Wed 7 Oct, 14:00; "
                "Thu 8 Oct, 11:30.\n\nIf this isn't relevant, just reply 'no thanks'.\n\nSam"}


# ------------------------------------------------------------------ pure rules
@pytest.mark.parametrize("company,text,ok", [
    ("Smile Bright Dental", "I looked at Smile Bright Dental today", True),
    ("Hawthorn Road Family Dental Clinic", "your Hawthorn Road practice", True),     # first two words
    ("Acme Dental", "I looked at Globex Dental", False),
    ("Zed", "Zed is great", True),                                                   # the full name is always enough
    ("Zed Co", "hello zed team", False),                                             # first TWO words are needed
    ("Northwind", "Hello NORTHWIND team", True),
    ("Smile  Bright, Dental!", "smile bright dental", True)])
def test_company_name_matching(company, text, ok):
    assert company_mentioned(company, text) is ok


def test_email_rules():
    assert check_email(GOOD, "Smile Bright Dental", SLOTS) == ([], [])
    errors, warnings = check_email({"subject": "", "body": ""}, "Acme")
    assert "no subject" in errors[0] and any("no body" in e for e in errors)
    errors, _ = check_email({"subject": "Hi", "body": "Hello [Name], we like your site and clinic a lot."}, "Acme")
    assert any("placeholder text '[Name]'" in e for e in errors) and any("never mentions the company" in e for e in errors)
    errors, warnings = check_email({"subject": "Acme idea", "body": "Hi, Acme has a site."}, "Acme", SLOTS)
    assert errors == [] and any("opt-out" in w for w in warnings) and any("Tue 6 Oct, 10:00" in w for w in warnings)


def test_website_rules(monkeypatch):
    issue = {"title": "t", "severity": "low", "detail": "d"}
    ok = {"audit": {"summary": "s", "issues": [issue] * 3, "contact": {}}, "evidence": ["u"]}
    assert V.check_website(ok) == []
    two = {**ok, "audit": {**ok["audit"], "issues": [issue] * 2}}
    assert "only 2 issue(s); at least 3" in V.check_website(two)[0]
    monkeypatch.setattr(settings, "verify_min_audit_issues", 1)
    assert V.check_website(two) == []
    bad = {**ok, "audit": {**ok["audit"], "issues": [{"title": "t", "severity": "huge", "detail": ""}] * 3}}
    assert any("missing a title, detail or valid severity" in i for i in V.check_website(bad))
    assert V.check_website({}) == ["the output has no website audit"]


def test_scoring_research_scheduling_followup_rules():
    lead = {"company": "A", "score": 80, "tier": "A", "reasons": ["r"], "recommended_actions": ["a"], "pitch_angle": "p"}
    assert V.check_scoring(lead) == []
    assert "does not match score 80" in V.check_scoring({**lead, "tier": "C"})[0]
    assert V.check_scoring({**lead, "score": 101}) == ["score must be an integer from 0 to 100"]
    assert any("reasons is empty" in i for i in V.check_scoring({**lead, "reasons": []}))
    prof = {"profile": {"company_name": "A", "industry": "x", "summary": "s", "services": []}, "evidence": ["u"]}
    assert V.check_research(prof) == [] and "profile.industry is empty" in V.check_research(
        {**prof, "profile": {**prof["profile"], "industry": " "}})
    assert V.check_scheduling({"proposal": {"slots": SLOTS}}) == []
    assert "exactly 3" in V.check_scheduling({"proposal": {"slots": SLOTS[:2]}})[0]
    assert V.check_followup({"followups": [{"action": "a", "due_at": "2026-10-09T10:00"}] * 2}) == []
    assert "2 or 3" in V.check_followup({"followups": []})[0]


# ------------------------------------------------------------------ per-task retry in the orchestrator
ISSUE = {"title": "t", "severity": "low", "detail": "d"}


def website_out(n):
    return {"url": "https://x.example/", "evidence": ["https://x.example/"], "signals": {},
            "audit": {"summary": "s", "issues": [ISSUE] * n, "opportunities": [], "contact": {}}}


def run_plan(plan, input=None):
    with SessionLocal() as db:
        rid = create_run(db, "test objective", input or {"url": "https://x.example"}, plan).id
    asyncio.run(run_workflow(rid))
    return rid


def events(rid):
    with SessionLocal() as db:
        return list(db.scalars(select(Event).where(Event.run_id == rid).order_by(Event.id)))


PLAN = Plan(objective="x", tasks=[PlanTask(task_key="t_01", agent="website"),
                                  PlanTask(task_key="t_02", agent="scoring", depends_on=["t_01"]),
                                  PlanTask(task_key="t_03", agent="report", depends_on=["t_01", "t_02"])])


def test_failing_rule_is_retried_once_with_the_issues_as_feedback(monkeypatch):
    seen = []

    async def website(task, context):
        seen.append(task.input.get("verification_feedback"))
        return AgentResult(output=website_out(3 if task.input.get("verification_feedback") else 2))
    monkeypatch.setitem(AGENTS, "website", website)
    rid = run_plan(PLAN)
    assert seen[0] is None and "only 2 issue(s)" in seen[1] and len(seen) == 2
    with SessionLocal() as db:
        run = db.get(Run, rid)
        t = {x.agent: x for x in run.tasks}
        assert t["website"].status == "succeeded" and t["website"].attempts == 2 and len(t["website"].output_json["audit"]["issues"]) == 3
        assert run.status == "succeeded" and run.finished_at is not None
    assert "verification_retry" in [e.type for e in events(rid)]


def test_still_failing_after_the_retry_marks_it_failed_and_the_report_flags_it(monkeypatch):
    calls = []

    async def website(task, context):
        calls.append(1)
        return AgentResult(output=website_out(2))
    monkeypatch.setitem(AGENTS, "website", website)
    rid = run_plan(PLAN)
    assert len(calls) == 2                                              # one retry, no more
    with SessionLocal() as db:
        run = db.get(Run, rid)
        t = {x.agent: x for x in run.tasks}
        assert t["website"].status == "failed" and "VerificationFailed" in t["website"].error and "after one retry" in t["website"].error
        assert len(t["website"].output_json["audit"]["issues"]) == 2    # the output is kept for the report
        assert t["scoring"].status == "skipped"                         # nothing downstream used a rejected result
        assert t["report"].status == "succeeded"                        # ...but the report still ran
        assert run.status == "failed" and run.finished_at is not None
        assert "VerificationFailed" in run.report_md and "t_01 website" in run.report_md and "t_02 scoring" in run.report_md
        assert "finished with 2 problem(s)" in run.report_md
    types = [e.type for e in events(rid)]
    assert "verification_retry" in types and types[-1] == "run_failed"


def test_deterministic_agents_are_not_retried(monkeypatch):
    calls = []

    async def sched(task, context):
        calls.append(1)
        return AgentResult(output={"proposal": {"slots": SLOTS[:2]}})
    monkeypatch.setitem(AGENTS, "scheduling", sched)
    rid = run_plan(Plan(objective="x", tasks=[PlanTask(task_key="t_01", agent="scheduling")]))
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.get(Task, f"{rid}:t_01").status == "failed"


def test_report_waits_for_unfinished_dependencies():
    plan = Plan(objective="x", tasks=[PlanTask(task_key="t_01", agent="communication"),
                                      PlanTask(task_key="t_02", agent="report", depends_on=["t_01"])])
    rid = run_plan(plan, {"url": "https://x.example"})                  # communication has no scoring input: fails
    with SessionLocal() as db:
        assert {t.agent: t.status for t in db.get(Run, rid).tasks} == {"communication": "failed", "report": "succeeded"}


# ------------------------------------------------------------------ the email, before approval
def comm(ctx=None, **inp):
    rid, _ = setup_run()
    t = AgentTask(run_id=rid, task_key="t_01", agent="communication", goal="Draft", input=inp)
    return rid, asyncio.run(communication_agent(t, CTX if ctx is None else ctx))


PROPOSAL_CTX = {**CTX, "t_04": {"proposal": {"title": "Intro call", "duration_min": 30, "timezone": "Melbourne time", "slots": SLOTS}}}
WITH_SLOTS = {**DRAFT, "body": DRAFT["body"] + "\n\nTimes: Tue 6 Oct, 10:00; Wed 7 Oct, 14:00; Thu 8 Oct, 11:30."}
NO_COMPANY = {**DRAFT, "subject": "A quick idea", "body": DRAFT["body"].replace("Smile Bright Dental", "your clinic")}


def test_a_draft_without_the_company_name_is_regenerated_once_with_feedback(monkeypatch):
    llm = FakeLLM(monkeypatch, [NO_COMPANY, DRAFT])
    rid, res = comm()
    assert res.status == "needs_approval" and res.output["verification"]["regenerated"] is True
    assert len(llm.calls) == 2 and "never mentions the company name 'Smile Bright Dental'" in llm.calls[1][1]["content"]
    assert res.output["draft"]["subject"] == DRAFT["subject"]
    with SessionLocal() as db:        # (setup_run adds its own "Hello there" message)
        subjects = [m.subject for m in db.scalars(select(Message).where(Message.run_id == rid))]
    assert DRAFT["subject"] in subjects and "A quick idea" not in subjects             # the rejected draft was never saved


def test_a_draft_that_still_fails_is_never_shown_to_the_reviewer(monkeypatch):
    FakeLLM(monkeypatch, [NO_COMPANY, NO_COMPANY])
    rid, _ = setup_run()
    t = AgentTask(run_id=rid, task_key="t_01", agent="communication", goal="Draft", input={})
    with pytest.raises(VerificationFailed, match="failed verification after one retry") as e:
        asyncio.run(communication_agent(t, CTX))
    assert e.value.output["verification"]["errors"] and e.value.output["draft"]["subject"] == "A quick idea"
    with SessionLocal() as db:                                           # fail closed: nothing to approve, nothing saved
        assert db.scalars(select(Approval).where(Approval.run_id == rid)).first() is None
        assert [m.subject for m in db.scalars(select(Message).where(Message.run_id == rid))] == ["Hello there"]   # only setup_run's


def test_missing_meeting_slots_trigger_the_retry_and_the_email_offers_them(monkeypatch):
    llm = FakeLLM(monkeypatch, [DRAFT, WITH_SLOTS])
    rid, res = comm(ctx=PROPOSAL_CTX)
    assert len(llm.calls) == 2 and "does not offer these meeting slots" in llm.calls[1][1]["content"]
    assert all(s["label"] in res.output["draft"]["body"] for s in SLOTS)
    assert "Offer ALL of these" in llm.calls[0][1]["content"] and "- Tue 6 Oct, 10:00" in llm.calls[0][1]["content"]
    assert res.output["verification"]["warnings"] == []


def test_template_fallback_includes_the_slots_and_passes_the_rules():
    rid, res = comm(ctx=PROPOSAL_CTX)                                    # no LLM in tests
    assert res.output["generated_by"] == "template" and res.output["verification"]["passed"]
    assert all(s["label"] in res.output["draft"]["body"] for s in SLOTS) and "Melbourne time" in res.output["draft"]["body"]


def test_claims_check_warnings_reach_the_approval_but_never_block_it(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    claims = {"supported": False, "unsupported_claims": ["20 years of experience"]}

    class Both(FakeLLM):
        async def _call(self, messages):
            self.calls.append(messages)
            r = claims if "fact-checker" in messages[0]["content"] else DRAFT
            return json.dumps(r), 10
    Both(monkeypatch, [])
    rid, res = comm()
    assert res.status == "needs_approval"
    v = res.output["verification"]
    assert v["claims"]["checked"] and v["claims"]["unsupported"] == ["20 years of experience"]
    assert v["passed"] and any("20 years of experience" in w for w in v["warnings"])
    with SessionLocal() as db:
        a = db.scalars(select(Approval).where(Approval.run_id == rid)).one()
        assert a.payload_json["verification"]["warnings"] == v["warnings"]


def test_claims_check_is_skipped_without_an_api_key_or_when_disabled(monkeypatch):
    out = asyncio.run(V.llm_claims_check(DRAFT, {}, None))
    assert out["checked"] is False and "skipped" in out["reason"]
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(settings, "verify_with_llm", False)
    assert asyncio.run(V.llm_claims_check(DRAFT, {}, None))["checked"] is False


def test_claims_check_failure_is_a_note_not_an_error(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    FakeLLM(monkeypatch, ["junk", "junk", "junk"])
    out = asyncio.run(V.llm_claims_check(DRAFT, {}, None))
    assert out["checked"] is False and "skipped" in out["reason"]


# ------------------------------------------------------------------ the Verification task
def crafted_run(email=None, **over):
    """A finished run with hand-made outputs, so the Verification task can be tested on its own."""
    outputs = {
        "research": {"profile": {"company_name": "Smile Bright Dental", "industry": "dental", "summary": "s", "services": ["a"]}, "evidence": ["u"]},
        "website": website_out(3),
        "scoring": {"company": "Smile Bright Dental", "score": 80, "tier": "A", "reasons": ["r"], "recommended_actions": ["a"], "pitch_angle": "p"},
        "scheduling": {"proposal": {"slots": SLOTS}},
        "communication": {"company": "Smile Bright Dental", "draft": email or GOOD, "verification": {"body_sha": V.body_hash((email or GOOD)["body"]),
                          "claims": {"checked": True, "unsupported": [], "reason": None}}},
        "followup": {"followups": [{"action": "a", "due_at": "2026-10-09T10:00"}] * 2},
    }
    outputs.update(over)
    plan = Plan(objective="x", tasks=[PlanTask(task_key=f"t_{i:02d}", agent=a) for i, a in enumerate(outputs, 1)]
                                      + [PlanTask(task_key="t_99", agent="verification")])
    with SessionLocal() as db:
        rid = create_run(db, "x", {}, plan).id
        for t in db.scalars(select(Task).where(Task.run_id == rid)):
            if t.agent in outputs:
                t.status, t.output_json = "succeeded", outputs[t.agent]
        db.commit()
    return rid


def verify(rid):
    t = AgentTask(run_id=rid, task_key="t_99", agent="verification", goal="Verify", input={})
    return asyncio.run(verification_agent(t, {})).output


def test_verification_task_passes_a_good_run_and_reuses_the_earlier_claims_check(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    llm = FakeLLM(monkeypatch, [])                                         # any LLM call would raise IndexError
    out = verify(crafted_run())
    assert out["passed"] and out["summary"] == "6 of 6 checks passed" and not llm.calls
    assert out["claims"]["checked"] and "reused" in out["claims"]["reason"]


def test_verification_task_fails_when_a_result_breaks_its_rules():
    rid = crafted_run(website=website_out(1))
    with pytest.raises(VerificationFailed, match="t_02 website") as e:
        verify(rid)
    assert e.value.output["passed"] is False and "only 1 issue(s)" in " ".join(
        i for c in e.value.output["checks"] for i in c["issues"])


def test_unfinished_tasks_are_listed_as_not_verified_not_double_failed():
    rid = crafted_run()
    with SessionLocal() as db:
        t = next(t for t in db.scalars(select(Task).where(Task.run_id == rid)) if t.agent == "followup")
        t.status, t.error = "failed", "boom"
        db.commit()
    out = verify(rid)
    assert out["passed"] and any(c["agent"] == "followup" and c["result"] == "not_verified" and "boom" in c["issues"][0] for c in out["checks"])


def test_an_email_edited_by_the_reviewer_is_rechecked_and_only_flagged(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    edited = {"subject": "Hello", "body": "Hello, it's me.\n\nSam"}        # lost the company name, slots, opt-out line
    class Claims(FakeLLM):
        async def _call(self, messages):
            self.calls.append(messages)
            return json.dumps({"supported": False, "unsupported_claims": ["a made-up award"]}), 10
    llm = Claims(monkeypatch, [])
    rid = crafted_run(email=edited)
    with SessionLocal() as db:                                             # the stored check belonged to the ORIGINAL body
        t = next(t for t in db.scalars(select(Task).where(Task.run_id == rid)) if t.agent == "communication")
        t.output_json = {**t.output_json, "verification": {"body_sha": "original-hash", "claims": {"checked": True, "unsupported": []}}}
        db.commit()
    out = verify(rid)
    comm_check = next(c for c in out["checks"] if c["agent"] == "communication")
    assert len(llm.calls) == 1                                             # edited -> checked again
    assert out["passed"] and comm_check["result"] == "flagged"            # the human decides; we only flag
    assert any("edited by the reviewer: the email never mentions" in w for w in comm_check["warnings"])
    assert any("a made-up award" in w for w in comm_check["warnings"])


# ------------------------------------------------------------------ global guards
class Thing(BaseModel):
    ok: bool = True


def test_llm_call_budget_stops_the_run_at_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls_per_run", 2)
    llm = FakeLLM(monkeypatch, ["{}", "{}", "{}"])
    with SessionLocal() as db:
        rid = create_run(db, "x").id
    for _ in range(2):
        asyncio.run(complete_json("s", "u", Thing, run_id=rid))
    with pytest.raises(LLMBudgetExceeded, match="budget exceeded") as e:
        asyncio.run(complete_json("s", "u", Thing, run_id=rid))
    assert len(llm.calls) == 2 and isinstance(e.value, PermanentError)              # the third call never reached the provider
    with SessionLocal() as db:
        assert db.get(Run, rid).llm_calls == 2
    assert "llm_budget_exceeded" in [x.type for x in events(rid)]


def test_every_retry_attempt_counts_against_the_budget(monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls_per_run", 12)
    FakeLLM(monkeypatch, ["junk", {"ok": True}])
    with SessionLocal() as db:
        rid = create_run(db, "x").id
    asyncio.run(complete_json("s", "u", Thing, run_id=rid))
    with SessionLocal() as db:
        assert db.get(Run, rid).llm_calls == 2


def test_a_task_that_runs_too_long_times_out_and_the_run_carries_on(monkeypatch):
    monkeypatch.setattr(settings, "task_timeout_s", 0.05)

    async def slow(task, context):
        await asyncio.sleep(2)
        return AgentResult()
    monkeypatch.setitem(AGENTS, "website", slow)
    rid = run_plan(PLAN)
    with SessionLocal() as db:
        run = db.get(Run, rid)
        t = {x.agent: x for x in run.tasks}
        assert t["website"].status == "failed" and "TimeoutError" in t["website"].error and "0.05s limit" in t["website"].error
        assert t["website"].attempts == 2                                   # timeouts are retried once
        assert t["report"].status == "succeeded" and run.status == "failed" and run.finished_at is not None


def test_budget_error_is_not_retried(monkeypatch):
    async def broke(task, context):
        raise LLMBudgetExceeded("LLM call budget exceeded")
    monkeypatch.setitem(AGENTS, "website", broke)
    rid = run_plan(PLAN)
    with SessionLocal() as db:
        assert next(t for t in db.get(Run, rid).tasks if t.agent == "website").attempts == 1
