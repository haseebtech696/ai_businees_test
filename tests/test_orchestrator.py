import asyncio
import pytest
from sqlalchemy import select

from app.agents.base import AGENTS
from app.core.orchestrator import create_run, run_workflow
from app.db.models import Event, Run
from app.db.session import SessionLocal
from app.core.config import settings
from app.schemas.contracts import AgentResult, Plan, PlanTask
from tests.conftest import FakeLLM, approve_all, plan_json


def _run(plan=None):
    with SessionLocal() as db:
        rid = create_run(db, "test objective", {}, plan).id
    asyncio.run(run_workflow(rid))
    return rid


def _run_and_approve(plan=None):
    rid = _run(plan)
    approve_all(rid)
    return rid


def _events(rid):
    with SessionLocal() as db:
        return list(db.scalars(select(Event).where(Event.run_id == rid).order_by(Event.id)))


def test_pipeline_pauses_at_the_email_then_finishes_after_approval():
    rid = _run()
    with SessionLocal() as db:
        run = db.get(Run, rid)
        st = {t.agent: t.status for t in run.tasks}
        assert run.status == "awaiting_approval" and run.finished_at is None
        assert st["communication"] == "awaiting_approval"
        assert st["verification"] == st["followup"] == st["report"] == "pending"     # blocked behind the email
        assert st["scheduling"] == "succeeded"                                        # independent branch kept going
    approve_all(rid)
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded" and all(t.status == "succeeded" for t in run.tasks)


def test_full_pipeline_succeeds():
    rid = _run_and_approve()
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded" and run.finished_at is not None
        assert len(run.tasks) == 8 and all(t.status == "succeeded" for t in run.tasks)
        assert run.report_md and "# Outreach report" in run.report_md
        scoring = next(t for t in run.tasks if t.agent == "scoring")
        assert scoring.output_json["leads"][0]["score"] >= 75   # used research + website context
    types = [e.type for e in _events(rid)]
    assert types[0] == "run_created" and types[-1] == "run_succeeded"
    assert types.count("task_started") == 8 and types.count("task_succeeded") == 7
    assert types.count("approval_requested") == 1 and types.count("action_executed") == 1


def test_research_and_website_run_in_parallel():
    ev = _events(_run())
    started = {e.payload_json["task_key"]: e for e in ev if e.type == "task_started"}
    done = {e.payload_json["task_key"]: e for e in ev if e.type == "task_succeeded"}
    # both started before either finished => they overlapped
    assert max(started["t_01"].id, started["t_02"].id) < min(done["t_01"].id, done["t_02"].id)
    # scoring waits for both
    assert started["t_03"].id > max(done["t_01"].id, done["t_02"].id)


def test_dependency_order_respected():
    ev = _events(_run_and_approve())
    started = {e.payload_json["task_key"]: e.id for e in ev if e.type == "task_started"}
    done = {e.payload_json["task_key"]: e.id for e in ev if e.type in ("task_succeeded", "action_executed")}
    from app.core.planner import stub_plan
    for t in stub_plan("x").tasks:
        for d in t.depends_on:
            assert done[d] < started[t.task_key]




def test_failure_skips_dependents(monkeypatch):
    async def boom(task, context):
        raise RuntimeError("nope")
    monkeypatch.setitem(AGENTS, "boom", boom)
    plan = Plan(objective="x", tasks=[
        PlanTask(task_key="a", agent="boom"),
        PlanTask(task_key="b", agent="research", depends_on=["a"]),
        PlanTask(task_key="c", agent="research"),
    ])
    rid = _run(plan)
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "failed"
        assert {t.task_key: t.status for t in run.tasks} == {"a": "failed", "b": "skipped", "c": "succeeded"}
        a = next(t for t in run.tasks if t.task_key == "a")
        assert a.attempts == 2 and "nope" in a.error


def test_needs_approval_pauses_then_resume_continues(monkeypatch):
    async def needs_ok(task, context):
        return AgentResult(status="needs_approval", output={"draft": "hi"})
    monkeypatch.setitem(AGENTS, "needs_ok", needs_ok)
    plan = Plan(objective="x", tasks=[PlanTask(task_key="a", agent="needs_ok"),
                                      PlanTask(task_key="b", agent="research", depends_on=["a"])])
    rid = _run(plan)
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "awaiting_approval"
        assert {t.task_key: t.status for t in run.tasks} == {"a": "awaiting_approval", "b": "pending"}
        next(t for t in run.tasks if t.task_key == "a").status = "succeeded"   # simulate approval
        db.commit()
    asyncio.run(run_workflow(rid))   # resume purely from DB state
    with SessionLocal() as db:
        assert db.get(Run, rid).status == "succeeded"
    assert "run_resumed" in [e.type for e in _events(rid)]


def test_unknown_agent_rejected():
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            create_run(db, "x", {}, Plan(objective="x", tasks=[PlanTask(task_key="a", agent="ghost")]))


def test_manager_plan_is_saved_and_executed(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    FakeLLM(monkeypatch, [plan_json(("t_01", "website", []), ("t_02", "report", ["t_01"]))], tokens=123)
    with SessionLocal() as db:
        rid = create_run(db, "Audit acme.com", {}).id          # no plan: the Manager writes it
        assert db.get(Run, rid).tasks == [] and db.get(Run, rid).plan_json is None
    asyncio.run(run_workflow(rid))
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded"
        assert [t.agent for t in run.tasks] == ["website", "report"]      # only what the plan asked for
        assert run.plan_json["tasks"][1]["depends_on"] == ["t_01"]       # plan_json saved
        assert run.tokens == 123 and run.report_md
    ev = _events(rid)
    created = next(e for e in ev if e.type == "plan_created")
    assert created.payload_json["source"] == "llm" and created.payload_json["agents"] == ["website", "report"]
    assert [e.type for e in ev][:3] == ["run_created", "run_started", "plan_created"]


def test_llm_failure_falls_back_and_run_still_succeeds(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    FakeLLM(monkeypatch, ["garbage", "garbage", "garbage"])
    rid = _run_and_approve()
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded" and len(run.tasks) == 8
    types = [e.type for e in _events(rid)]
    assert types.count("llm_retry") == 3
    assert next(e for e in _events(rid) if e.type == "plan_created").payload_json["source"] == "fallback"


def test_permanent_error_is_not_retried(monkeypatch):
    from app.core.errors import PermanentError

    async def blocked(task, context):
        raise PermanentError("blocked for good")
    monkeypatch.setitem(AGENTS, "blocked", blocked)
    rid = _run(Plan(objective="x", tasks=[PlanTask(task_key="a", agent="blocked")]))
    with SessionLocal() as db:
        t = db.get(Run, rid).tasks[0]
        assert t.status == "failed" and t.attempts == 1 and "blocked for good" in t.error


def test_retry_waits_before_trying_again(monkeypatch):
    import time
    monkeypatch.setattr(settings, "retry_backoff_s", 0.2)
    calls = {"n": 0}

    async def flaky(task, context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit")
        return AgentResult(output={"ok": True})
    monkeypatch.setitem(AGENTS, "flaky", flaky)
    t0 = time.monotonic()
    rid = _run(Plan(objective="x", tasks=[PlanTask(task_key="a", agent="flaky")]))
    assert time.monotonic() - t0 >= 0.2
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded" and run.tasks[0].attempts == 2
    retry = next(e for e in _events(rid) if e.type == "task_retry")
    assert "429" in retry.payload_json["error"]                  # the reason is recorded for the dashboard
