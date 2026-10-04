import asyncio

from app.agents.manager import build_system_prompt, make_plan, make_plan_ex
from app.agents.base import AGENTS
from app.core import llm_gateway
from app.core.config import settings
from app.schemas.contracts import Plan
from tests.conftest import FakeLLM, plan_json

AUDIT = plan_json(("t_01", "website", []), ("t_02", "report", ["t_01"]))
LEADS = plan_json(("t_01", "research", []), ("t_02", "scoring", ["t_01"]), ("t_03", "report", ["t_02"]))


def _plan(monkeypatch, replies, objective="obj", input=None):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    llm = FakeLLM(monkeypatch, replies)
    plan, meta = asyncio.run(make_plan_ex(objective, input))
    return plan, meta, llm


def test_prompt_lists_every_agent_and_rules():
    p = build_system_prompt()
    assert all(f"`{a}`" in p for a in AGENTS)
    assert "{{AGENTS}}" not in p
    assert "never have dependencies" in p and "depends_on" in p


def test_two_objectives_two_different_valid_plans(monkeypatch):
    p1, m1, _ = _plan(monkeypatch, [AUDIT], "Audit acme.com")
    p2, m2, _ = _plan(monkeypatch, [LEADS], "Find logistics leads")
    assert m1["source"] == m2["source"] == "llm"
    assert isinstance(p1, Plan) and isinstance(p2, Plan)
    assert [t.agent for t in p1.tasks] != [t.agent for t in p2.tasks]
    assert p1.objective == "Audit acme.com"                      # always the user's objective


def test_run_input_reaches_the_first_tasks(monkeypatch):
    plan, _, _ = _plan(monkeypatch, [LEADS], input={"industry": "dental"})
    assert plan.tasks[0].input == {"industry": "dental"}
    assert plan.tasks[1].input == {}                             # dependent tasks get context instead


def test_invalid_json_is_retried_then_used(monkeypatch):
    plan, meta, llm = _plan(monkeypatch, ["{not json", AUDIT])
    assert meta["source"] == "llm" and len(llm.calls) == 2


def test_bad_plans_are_fed_back_to_the_model(monkeypatch):
    cycle = plan_json(("t_01", "scoring", ["t_02"]), ("t_02", "communication", ["t_01"]))
    ghost = plan_json(("t_01", "telepathy", []))
    root_dep = plan_json(("t_01", "research", []), ("t_02", "website", ["t_01"]))
    plan, meta, llm = _plan(monkeypatch, [cycle, ghost, root_dep])
    assert meta["source"] == "fallback"                          # 3 bad answers -> fallback
    errs = [c[-1]["content"] for c in llm.calls[1:]]
    assert "cycle" in errs[0] and "unknown agent" in errs[1]


def test_bad_first_then_good_second(monkeypatch):
    ghost = plan_json(("t_01", "telepathy", []))
    plan, meta, llm = _plan(monkeypatch, [ghost, LEADS])
    assert meta["source"] == "llm" and len(plan.tasks) == 3


def test_fallback_when_llm_fails(monkeypatch):
    plan, meta, _ = _plan(monkeypatch, [llm_gateway.LLMError("boom")])
    assert meta["source"] == "fallback" and "boom" in meta["reason"]
    assert len(plan.tasks) == 8                                   # the default hard-coded plan


def test_fallback_without_api_key_makes_no_call():
    # autouse fixture blocks the network and GROQ_API_KEY is empty
    plan = asyncio.run(make_plan("anything", {}))
    assert len(plan.tasks) == 8


def test_report_always_depends_on_every_other_task(monkeypatch):
    chain = plan_json(("t_01", "research", []), ("t_02", "website", []), ("t_03", "scoring", ["t_01", "t_02"]),
                      ("t_04", "communication", ["t_03"]), ("t_05", "report", ["t_04"]))   # LLM only linked t_04
    plan, meta, _ = _plan(monkeypatch, [chain])
    assert meta["source"] == "llm"
    report = next(t for t in plan.tasks if t.agent == "report")
    assert report.depends_on == ["t_01", "t_02", "t_03", "t_04"]


def test_nothing_may_depend_on_report(monkeypatch):
    bad = plan_json(("t_01", "research", []), ("t_02", "report", ["t_01"]), ("t_03", "scoring", ["t_02"]))
    plan, meta, llm = _plan(monkeypatch, [bad, bad, bad])
    assert meta["source"] == "fallback" and "must be last" in llm.calls[1][-1]["content"]


def test_end_of_pipeline_wiring_is_completed_by_the_system_not_the_llm(monkeypatch):
    sloppy = plan_json(("t_01", "research", []), ("t_02", "website", []), ("t_03", "scoring", ["t_01", "t_02"]),
                       ("t_04", "communication", ["t_03"]), ("t_05", "scheduling", ["t_04"]),   # scheduling wrongly after the email
                       ("t_06", "followup", ["t_03"]), ("t_07", "verification", ["t_04"]), ("t_08", "report", ["t_07"]))
    plan, meta, _ = _plan(monkeypatch, [sloppy])
    assert meta["source"] == "llm"
    deps = {t.agent: set(t.depends_on) for t in plan.tasks}
    assert deps["scheduling"] == {"t_03"}                               # scheduling follows scoring, not the email...
    assert deps["communication"] == {"t_03", "t_05"}                    # ...the email waits for scheduling (it offers the slots)
    assert deps["followup"] == {"t_03", "t_04"}                         # follow-ups wait for the email
    assert deps["verification"] == {"t_01", "t_02", "t_03", "t_04", "t_05", "t_06"}   # everything except the report
    assert deps["report"] == {f"t_0{i}" for i in range(1, 8)}


def test_the_default_plan_offers_slots_before_the_email_and_verifies_before_the_report():
    from app.core.planner import stub_plan
    order = {t.agent: t for t in stub_plan("x").tasks}
    keys = {a: t.task_key for a, t in order.items()}
    assert keys["scheduling"] in order["communication"].depends_on
    assert keys["communication"] in order["followup"].depends_on
    assert set(order["verification"].depends_on) == {k for a, k in keys.items() if a not in ("verification", "report")}
    assert set(order["report"].depends_on) == {k for a, k in keys.items() if a != "report"}
