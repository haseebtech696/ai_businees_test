"""Manager agent: objective -> validated Plan (LLM), with a hard-coded fallback so a bad LLM never kills a run."""
import logging
from pathlib import Path

from pydantic import model_validator

from app.agents.base import AGENT_DESCRIPTIONS, AGENTS
from app.core.llm_gateway import complete_json
from app.core.planner import stub_plan
from app.schemas.contracts import Plan

log = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "manager.md"
MAX_TASKS = 8
ROOT_AGENTS = {"research", "website"}


class ManagerPlan(Plan):
    """Plan + the Manager's extra rules. The LLM gateway feeds any violation back to the model."""
    objective: str = ""

    @model_validator(mode="after")
    def _manager_rules(self):
        if len(self.tasks) > MAX_TASKS:
            raise ValueError(f"at most {MAX_TASKS} tasks allowed")
        seen = set()
        for t in self.tasks:
            if t.agent not in AGENTS:
                raise ValueError(f"unknown agent '{t.agent}'; allowed: {sorted(AGENTS)}")
            if t.agent in seen:
                raise ValueError(f"agent '{t.agent}' used more than once")
            seen.add(t.agent)
            if t.agent in ROOT_AGENTS and t.depends_on:
                raise ValueError(f"'{t.agent}' must have no dependencies")
            if t.agent == "report" and not t.depends_on:
                raise ValueError("'report' must depend on the tasks it summarises")
        report_keys = {t.task_key for t in self.tasks if t.agent == "report"}
        for t in self.tasks:
            if report_keys & set(t.depends_on):
                raise ValueError("no task may depend on 'report'; it must be last")
        return self


def build_system_prompt() -> str:
    agents = "\n".join(f"- `{name}`: {desc}" for name, desc in AGENT_DESCRIPTIONS.items())
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{{AGENTS}}", agents)


def normalize_deps(tasks: list[dict]) -> list[dict]:
    """The wiring around the end of the pipeline is fixed, so the LLM cannot get it wrong:
       communication also waits for scheduling (the email offers the slots); followup waits for communication;
       verification waits for everything except the report; the report waits for everything."""
    key = {t["agent"]: t["task_key"] for t in tasks}
    for t in tasks:
        deps = set(t["depends_on"])
        if t["agent"] == "scheduling":
            deps -= {key.get(a) for a in ("communication", "followup", "verification", "report")}
            if "scoring" in key:
                deps.add(key["scoring"])
        if t["agent"] == "communication" and "scheduling" in key:
            deps.add(key["scheduling"])
        if t["agent"] == "followup" and "communication" in key:
            deps.add(key["communication"])
        if t["agent"] not in ("report", "verification"):
            deps -= {key.get("verification")}
        if t["agent"] == "verification":
            deps = {o["task_key"] for o in tasks if o["agent"] not in ("report", "verification")}
        if t["agent"] == "report":            # the report must see every result, not just the last task's
            deps = {o["task_key"] for o in tasks if o["agent"] != "report"}
        deps.discard(None)
        t["depends_on"] = sorted(deps)
    return tasks


def _with_run_input(plan: Plan, objective: str, input: dict) -> Plan:
    """Start-of-plan tasks also receive the run's input (industry, url, ...); task input wins on conflict."""
    tasks = []
    for t in plan.tasks:
        data = t.model_dump()
        if not t.depends_on:
            data["input"] = {**input, **t.input}
        tasks.append(data)
    return Plan.model_validate({"objective": objective, "tasks": normalize_deps(tasks)})


async def make_plan_ex(objective: str, input: dict | None = None, run_id: str | None = None) -> tuple[Plan, dict]:
    """Returns (plan, meta). meta = {"source": "llm" | "fallback", "reason": str | None}."""
    input = input or {}
    user = f"Objective: {objective}\nInput parameters: {input}"
    try:
        mp = await complete_json(build_system_prompt(), user, ManagerPlan, run_id=run_id)
        return _with_run_input(mp, objective, input), {"source": "llm", "reason": None}
    except Exception as e:  # noqa: BLE001  any failure -> fallback, the demo must go on
        reason = f"{type(e).__name__}: {e}"[:400]
        log.warning("Manager falling back to default plan: %s", reason)
        return stub_plan(objective, input), {"source": "fallback", "reason": reason}


async def make_plan(objective: str, input: dict | None = None, run_id: str | None = None) -> Plan:
    plan, _ = await make_plan_ex(objective, input, run_id)
    return plan
