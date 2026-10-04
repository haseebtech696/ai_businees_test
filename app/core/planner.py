"""The default (fallback) plan. The Manager normally writes the plan; this one is used when it cannot.

   t_01 research ─┐
                  ├─> t_03 scoring ─> t_04 scheduling ─┐
   t_02 website ──┘            └──────────────────────┴─> t_05 communication ──(human approval)──┬─> t_06 followup ──┐
                                                                                                 └───────────────────┴─> t_07 verification ─> t_08 report
"""
from app.schemas.contracts import Plan, PlanTask


def stub_plan(objective: str, input: dict | None = None) -> Plan:
    inp = input or {}
    return Plan(objective=objective, tasks=[
        PlanTask(task_key="t_01", agent="research", goal="Find and profile the target company", input=inp),
        PlanTask(task_key="t_02", agent="website", goal="Audit its website", input=inp),
        PlanTask(task_key="t_03", agent="scoring", goal="Score and tier the lead",
                 depends_on=["t_01", "t_02"]),
        PlanTask(task_key="t_04", agent="scheduling", goal="Propose three meeting slots", depends_on=["t_03"]),
        PlanTask(task_key="t_05", agent="communication", goal="Draft the outreach email and proposal",
                 depends_on=["t_03", "t_04"]),
        PlanTask(task_key="t_06", agent="followup", goal="Plan follow-up actions", depends_on=["t_05"]),
        PlanTask(task_key="t_07", agent="verification", goal="Verify every result and the final email",
                 depends_on=["t_01", "t_02", "t_03", "t_04", "t_05", "t_06"]),
        PlanTask(task_key="t_08", agent="report", goal="Write the final report",
                 depends_on=["t_01", "t_02", "t_03", "t_04", "t_05", "t_06", "t_07"]),
    ])
