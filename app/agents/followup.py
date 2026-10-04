"""Follow-up agent: 2-3 dated actions after the email, saved to the `followups` table."""
from datetime import timedelta

from sqlalchemy import delete

from app.agents._common import find_output
from app.db.models import Followup, now
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask

# tier -> (days after the email, kind). Hotter leads get a third touch.
SCHEDULE = {"A": [(3, "nudge"), (7, "check-in"), (14, "last note")],
            "B": [(3, "nudge"), (7, "check-in")],
            "C": [(7, "check-in"), (14, "last note")]}


def _text(kind: str, days: int, company: str, pitch: str, slots: list[dict]) -> str:
    when = f"Day +{days}"
    if kind == "nudge":
        return f"{when}: if {company} has not replied, send a two-line nudge that repeats the idea: {pitch}"
    if kind == "check-in":
        offer = (f" and ask whether {slots[0]['label']} or another offered slot works" if slots else "")
        return f"{when}: check in with {company}{offer}; offer a short written summary instead if they prefer."
    return f"{when}: send a polite last note to {company} and close the thread if there is still no reply."


async def followup_agent(task: AgentTask, context: dict) -> AgentResult:
    lead = find_output(context, "pitch_angle") or {}
    comm = find_output(context, "draft") or {}
    company = lead.get("company") or comm.get("company") or "the company"
    tier = lead.get("tier") if lead.get("tier") in SCHEDULE else "B"
    slots = ((find_output(context, "proposal") or {}).get("proposal") or {}).get("slots") or []
    start = now()
    items = [{"kind": kind, "due_at": (start + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M"),
              "action": _text(kind, d, company, lead.get("pitch_angle", "our earlier idea"), slots)}
             for d, kind in SCHEDULE[tier]]
    with SessionLocal() as db:
        db.execute(delete(Followup).where(Followup.run_id == task.run_id))     # a retry must not duplicate rows
        for it in items:
            db.add(Followup(run_id=task.run_id, action=it["action"], due_at=start + timedelta(
                days=next(d for d, k in SCHEDULE[tier] if k == it["kind"])), status="pending"))
        db.commit()
    return AgentResult(output={"followups": items, "evidence": []})
