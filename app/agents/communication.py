"""Communication agent: action plan + company profile + business_profile.yaml -> EmailDraft + proposal.
Saves the draft (status 'draft'), then asks the ToolGuard to send it. The ToolGuard pauses the run for approval."""
import json

from app.agents._common import AgentInputError, find_output, host_of, load_prompt
from app.agents.verification import (VerificationFailed, body_hash, check_email, claims_enabled, claims_evidence,
                                     llm_claims_check)
from app.core.business_profile import BusinessProfile, load_business_profile
from app.core.errors import PermanentError
from app.core.events import emit_event
from app.core.llm_gateway import complete_json
from app.core.tool_guard import request_action
from app.db.models import Message
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask
from app.schemas.messaging import EMAIL_RE, OutreachDraft
from app.tools.untrusted import wrap_untrusted


def _recipient(task: AgentTask, audit: dict | None) -> tuple[str, str]:
    override = (task.input.get("recipient_email") or "").strip()
    if override:
        return override, "override"
    emails = ((audit or {}).get("contact") or {}).get("emails") or []
    valid = [e for e in emails if EMAIL_RE.match(e)]
    return (valid[0], "website") if valid else ("", "none")


def _template(lead: dict, biz: BusinessProfile, proposal: dict | None) -> OutreachDraft:
    """Used only when the LLM is unavailable and this is a first draft, so the demo still produces an email."""
    company, host = lead["company"], host_of(lead.get("website") or "")
    sig = "\n".join(x for x in [biz.sender_name, ", ".join(p for p in [biz.sender_title, biz.agency_name] if p),
                                biz.website] if x)
    slots = (f"I could do any of these times ({proposal['timezone']}): "
             + "; ".join(x["label"] for x in proposal["slots"]) + ".\n\n") if proposal else ""
    body = (f"Hi there,\n\nI had a look at {company}'s website{f' ({host})' if host else ''}. {lead['pitch_angle']}\n\n"
            f"{biz.offer_summary or 'We help local businesses get more enquiries from their website.'} "
            f"If it would help, I can send over a few specific ideas, or we could have a quick {proposal['duration_min'] if proposal else 15}-minute call. "
            + slots.replace("\n\n", " ").strip() + "\n\n"
            "If this isn't relevant, just reply 'no thanks' and I won't contact you again.\n\n" + sig)
    actions = "\n".join(f"- {a}" for a in lead.get("recommended_actions", [])[:3]) or "- Review the website together"
    proposal = (f"## Proposal for {company}\n\n### What we noticed\n{lead['pitch_angle']}\n\n"
                f"### What we would do\n{actions}\n\n### Next step\nA 15-minute call to agree priorities.")
    return OutreachDraft(subject=f"A quick idea for {company}"[:120], body=body, proposal_md=proposal)


def _user_message(task: AgentTask, lead: dict, profile: dict | None, audit: dict | None,
                  biz: BusinessProfile, to: str, proposal: dict | None = None) -> str:
    plan = {k: lead.get(k) for k in ("score", "tier", "reasons", "recommended_actions", "pitch_angle")}
    audit_view = ({"summary": audit.get("summary"), "issues": audit.get("issues"),
                   "opportunities": audit.get("opportunities")} if audit else "NOT AVAILABLE")
    parts = [f"Company: {lead['company']}\nWebsite: {lead.get('website', '')}\n"
             f"Recipient email: {to or 'unknown (use the generic greeting)'}\nTask goal: {task.goal}",
             "## Sender (trusted)\n" + json.dumps(biz.model_dump(exclude={"meeting"}), indent=2),
             "## Action plan\n" + wrap_untrusted(json.dumps(plan, indent=2), "analysis derived from web content"),
             "## Company profile\n" + wrap_untrusted(json.dumps(profile or "NOT AVAILABLE", indent=2), "research"),
             "## Website audit\n" + wrap_untrusted(json.dumps(audit_view, indent=2), "website audit")]
    if proposal:
        parts.insert(2, f"## Meeting slots to offer (trusted): {proposal['title']}, {proposal['duration_min']} minutes, "
                        f"{proposal['timezone']}\nOffer ALL of these and copy each label exactly as written:\n"
                        + "\n".join(f"- {x['label']}" for x in proposal["slots"]))
    if task.input.get("verification_feedback"):
        parts.append("## Verification feedback: your previous draft failed these checks. Fix them\n"
                     + str(task.input["verification_feedback"]))
    if task.input.get("revision_feedback"):
        prev = task.input.get("previous_draft") or {}
        parts.append("## Reviewer feedback (trusted): rewrite the draft to address this\n"
                     f"{task.input['revision_feedback']}\n\nPrevious draft subject: {prev.get('subject', '')}\n"
                     f"Previous draft body:\n{prev.get('body', '')}")
    return "\n\n".join(parts)


async def _generate(task, lead, profile, audit, biz, to, proposal, revision) -> tuple[OutreachDraft, str, str | None]:
    try:
        d = await complete_json(load_prompt("communication"), _user_message(task, lead, profile, audit, biz, to, proposal),
                                OutreachDraft, run_id=task.run_id, agent="communication")
        return d, "llm", None
    except Exception as e:  # noqa: BLE001
        if revision or task.input.get("verification_feedback"):     # a template cannot follow feedback
            raise PermanentError(f"Could not revise the draft: the LLM is unavailable ({type(e).__name__}: {e})") from e
        return _template(lead, biz, proposal), "template", f"{type(e).__name__}: {e}"[:300]


async def _verify(draft: OutreachDraft, company: str, slots, evidence, generated_by: str, run_id: str) -> dict:
    errors, warnings = check_email(draft.model_dump(), company, slots)
    claims = {"checked": False, "unsupported": [], "reason": "skipped (draft has rule errors or was not written by the LLM)"}
    if not errors and generated_by == "llm":
        claims = await llm_claims_check(draft.model_dump(), evidence, run_id)
        if claims.get("unsupported"):
            warnings.append("claims not supported by the evidence: " + "; ".join(claims["unsupported"]))
    return {"errors": errors, "warnings": warnings, "claims": claims, "body_sha": body_hash(draft.body)}


async def communication_agent(task: AgentTask, context: dict) -> AgentResult:
    lead = find_output(context, "pitch_angle")
    if not lead:
        raise AgentInputError("Communication needs the scoring output (an action plan with a pitch angle) but got none.")
    research, site = find_output(context, "profile"), find_output(context, "audit")
    profile = research["profile"] if research else None
    audit = site["audit"] if site else None
    proposal = (find_output(context, "proposal") or {}).get("proposal")
    slots = proposal["slots"] if proposal else None
    biz = load_business_profile()
    to, to_source = _recipient(task, audit)
    revision = int(task.input.get("revision", 0))
    company = lead["company"]
    evidence = claims_evidence(lead, profile, audit, proposal)

    draft, generated_by, llm_error = await _generate(task, lead, profile, audit, biz, to, proposal, revision)
    check = await _verify(draft, company, slots, evidence, generated_by, task.run_id)
    regenerated = False
    problems = check["errors"] + check["warnings"]
    if problems and generated_by == "llm":            # the one retry: tell the model exactly what to fix
        feedback = "; ".join(problems)
        retry_task = task.model_copy(update={"input": {**task.input, "verification_feedback": feedback}})
        emit_event(task.run_id, "verification_retry", "communication", task_key=task.task_key, issues=problems)
        draft, generated_by, llm_error = await _generate(retry_task, lead, profile, audit, biz, to, proposal, revision)
        check = await _verify(draft, company, slots, evidence, generated_by, task.run_id)
        regenerated = True
    verification = {**check, "regenerated": regenerated, "passed": not check["errors"]}

    if check["errors"]:                                # still unusable after the retry: fail, never send
        emit_event(task.run_id, "verification_failed", "communication", task_key=task.task_key, issues=check["errors"])
        raise VerificationFailed("Email failed verification after one retry: " + "; ".join(check["errors"]),
                                 output={"draft": {"to": to, "subject": draft.subject, "body": draft.body},
                                         "company": company, "verification": verification, "generated_by": generated_by})

    with SessionLocal() as db:                           # (1) save the draft
        m = Message(run_id=task.run_id, to=to, subject=draft.subject, body=draft.body, status="draft")
        db.add(m)
        db.commit()
        message_id = m.id
    emit_event(task.run_id, "draft_created", "communication", task_key=task.task_key, message_id=message_id,
               generated_by=generated_by, revision=revision, warnings=check["warnings"])

    payload = {"to": to, "subject": draft.subject, "body": draft.body, "message_id": message_id,
               "company": company, "proposal_md": draft.proposal_md, "revision": revision,
               "recipient_source": to_source, "verification": verification}
    decision = await request_action(task.run_id, task.task_key, "communication", "send_email", payload)   # (2) guard

    output = {"draft": {"to": to, "subject": draft.subject, "body": draft.body}, "proposal_md": draft.proposal_md,
              "message_id": message_id, "approval_id": decision.approval_id, "revision": revision,
              "generated_by": generated_by, "recipient_source": to_source, "company": company,
              "verification": verification, "evidence": lead.get("evidence", [])}
    if llm_error:
        output["llm_error"] = llm_error
    return AgentResult(status="needs_approval" if decision.pending else "ok", output=output)
