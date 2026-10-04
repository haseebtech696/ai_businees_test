"""Scheduling agent (mock calendar): three free slots from business_profile.yaml -> MeetingProposal.
The proposal is offered in the email. A tentative hold on the first slot goes through the ToolGuard:
with DRY_RUN=true it is only logged; with DRY_RUN=false it needs approval like any outside action."""
from app.agents._common import find_output
from app.core.business_profile import load_business_profile
from app.core.tool_guard import request_action
from app.db.models import now
from app.schemas.contracts import AgentResult, AgentTask
from app.tools.calendar import build_proposal


async def scheduling_agent(task: AgentTask, context: dict) -> AgentResult:
    lead = find_output(context, "pitch_angle") or {}
    company = lead.get("company") or "the prospect"
    proposal = build_proposal(load_business_profile(), now())
    first = proposal.slots[0]
    decision = await request_action(
        task.run_id, task.task_key, "scheduling", "create_event",
        {"title": f"{proposal.title} with {company}", "start": first.start, "duration_min": proposal.duration_min,
         "tentative": True})
    output = {"proposal": proposal.model_dump(), "event": decision.result, "approval_id": decision.approval_id,
              "evidence": []}
    return AgentResult(status="needs_approval" if decision.pending else "ok", output=output)
