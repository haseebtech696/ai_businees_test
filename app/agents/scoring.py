"""Lead + Strategy agent: research + website outputs -> score, tier, reasons, actions, pitch angle. Saves the lead."""
import json

from sqlalchemy import delete

from app.agents._common import AgentInputError, find_output, host_of, load_prompt, feedback_block
from app.core.events import log_event
from app.core.llm_gateway import complete_json
from app.db.models import Lead
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask
from app.schemas.info import LeadAssessment, tier_for
from app.tools.untrusted import wrap_untrusted


def save_lead(run_id: str, company: str, website: str, score: int, tier: str,
              profile: dict | None, audit: dict | None) -> None:
    with SessionLocal() as db:
        db.execute(delete(Lead).where(Lead.run_id == run_id, Lead.website == website))   # safe on retry
        db.add(Lead(run_id=run_id, company=company, website=website, score=score, tier=tier,
                    profile_json=profile, audit_json=audit))
        db.commit()
        log_event(db, run_id, "lead_saved", "scoring", company=company, score=score, tier=tier)


async def scoring_agent(task: AgentTask, context: dict) -> AgentResult:
    research = find_output(context, "profile")
    website = find_output(context, "audit")
    if not research and not website:
        raise AgentInputError("Scoring needs the output of the research and/or website agent, but got neither.")

    analysis = {"company_profile": research["profile"] if research else "NOT AVAILABLE",
                "website_audit": website["audit"] if website else "NOT AVAILABLE",
                "technical_signals": website.get("signals") if website else "NOT AVAILABLE"}
    user = (f"Task goal: {task.goal or 'score this lead'}\n\n"
            + wrap_untrusted(json.dumps(analysis, indent=2), source="analysis derived from web content")
            + feedback_block(task))
    result = await complete_json(load_prompt("lead_strategy"), user, LeadAssessment, run_id=task.run_id)
    tier = tier_for(result.score)          # the score is the source of truth; never trust a mismatched tier

    url = (website or research)["url"]
    company = (research["profile"]["company_name"] if research else None) or host_of(url)
    evidence = list(dict.fromkeys((research or {}).get("evidence", []) + (website or {}).get("evidence", [])))
    save_lead(task.run_id, company, url, result.score, tier,
              {**research["profile"], "evidence": research.get("evidence", [])} if research else None,
              {**website["audit"], "signals": website.get("signals"), "evidence": website.get("evidence", [])}
              if website else None)

    return AgentResult(output={"company": company, "website": url, "score": result.score, "tier": tier,
                               "reasons": result.reasons, "recommended_actions": result.recommended_actions,
                               "pitch_angle": result.pitch_angle, "evidence": evidence})
