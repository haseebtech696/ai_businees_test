"""Agent registry. Every agent: async def run(task: AgentTask, context: dict) -> AgentResult."""
from typing import Awaitable, Callable

from app.agents.communication import communication_agent  # real (Phase 4)
from app.agents.research import research_agent            # real (Phase 3)
from app.agents.scoring import scoring_agent              # real (Phase 3)
from app.agents.website import website_agent              # real (Phase 3)
from app.agents.followup import followup_agent            # real (Phase 5)
from app.agents.report import report_agent                # real (Phase 5)
from app.agents.scheduling import scheduling_agent        # real (Phase 5, mock calendar)
from app.agents.verification import verification_agent    # real (Phase 5)
from app.schemas.contracts import AgentResult, AgentTask

AgentFn = Callable[[AgentTask, dict], Awaitable[AgentResult]]

AGENTS: dict[str, AgentFn] = {
    "research": research_agent,
    "website": website_agent,
    "scoring": scoring_agent,
    "communication": communication_agent,
    "verification": verification_agent,
    "scheduling": scheduling_agent,
    "followup": followup_agent,
    "report": report_agent,
}


# One-line descriptions. The Manager prompt is built from this, so prompt and registry never drift.
AGENT_DESCRIPTIONS: dict[str, str] = {
    "research": "Profiles ONE company from its website URL plus web search: industry, size cues, services, recent news. Needs `url` in its input.",
    "website": "Audits ONE company website (homepage, contact page, key pages): issues with severity, opportunities, contact info. Needs `url` in its input.",
    "scoring": "Scores the lead 0-100, assigns a tier (A/B/C), and recommends actions and a pitch angle from the research and website outputs; saves the lead. Needs research and/or website output.",
    "communication": "Writes a personalised outreach email and a short proposal for the lead. The run then PAUSES until a human approves, edits or rejects the email; nothing is sent before that. Needs scoring output, and scheduling output when present.",
    "verification": "Re-checks every finished task and the final email (required fields, at least 3 audit issues, the company name in the email, no placeholder text) plus an optional LLM check that the email's claims match the evidence. Runs after the tasks it checks.",
    "scheduling": "Mock calendar: proposes three free meeting slots that the email offers, and logs a tentative calendar hold. Needs scoring output.",
    "followup": "Plans 2-3 follow-up actions (for example a +3 day nudge and a +7 day check-in) and saves them. Needs communication output.",
    "report": "Writes the final Markdown report (lead, findings, actions taken, pending items, verification, failures). Always the last task; it runs even when other tasks failed.",
}
assert set(AGENT_DESCRIPTIONS) == set(AGENTS)
