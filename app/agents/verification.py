"""Verification (Phase 5).

 1. Rule checks per task type. They run right after each task (the orchestrator retries a failing task ONCE
    with the issues as feedback, then marks it failed) and again in the final Verification task.
 2. An optional LLM check that the email's claims match the evidence (warnings only: it never blocks a run).
 3. verification_agent: re-checks every finished task and the FINAL email (including the human's edits)."""
import hashlib
import json
import re
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents._common import find_output, load_prompt
from app.agents.scoring import tier_for
from app.core.config import settings
from app.core.errors import PermanentError
from app.core.llm_gateway import complete_json
from app.db.models import Task
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask
from app.schemas.messaging import PLACEHOLDER_RE
from app.tools.untrusted import wrap_untrusted


class VerificationFailed(PermanentError):
    """A check still fails after its one retry. Carries the structured results so the report can show them."""
    def __init__(self, message: str, output: dict | None = None):
        super().__init__(message)
        self.output = output


# ------------------------------------------------------------------ email checks
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def company_mentioned(company: str, text: str) -> bool:
    """True if the full company name, or its first two words (first word if it is a single long word), is in the text."""
    t, c = " " + _norm(text) + " ", _norm(company)
    if not c:
        return True
    if f" {c} " in t:
        return True
    words = c.split()
    short = " ".join(words[:2]) if len(words) >= 2 else (c if len(c) >= 5 else "")
    return bool(short) and f" {short} " in t


def check_email(draft: dict, company: str, slots: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors make a draft unusable; warnings are shown to the human reviewer."""
    subject, body = (draft.get("subject") or "").strip(), (draft.get("body") or "").strip()
    errors, warnings = [], []
    if not subject:
        errors.append("the email has no subject")
    if not body:
        errors.append("the email has no body")
    text = f"{subject}\n{body}"
    if body and not company_mentioned(company, text):
        errors.append(f"the email never mentions the company name '{company}'")
    m = PLACEHOLDER_RE.search(text)
    if m:
        errors.append(f"the email contains placeholder text '{m.group(0)}'")
    low = body.lower()
    if body and not any(k in low for k in ("no thanks", "unsubscribe", "won't contact you again", "not relevant")):
        warnings.append("the email has no opt-out line")
    missing = [s["label"] for s in (slots or []) if s["label"].lower() not in low]
    if body and missing:
        warnings.append("the email does not offer these meeting slots: " + "; ".join(missing))
    return errors, warnings


class ClaimsCheck(BaseModel):
    supported: bool = True
    unsupported_claims: list[str] = Field(default_factory=list)


def body_hash(body: str) -> str:
    return hashlib.sha1((body or "").strip().encode("utf-8")).hexdigest()[:12]


def claims_enabled() -> bool:
    return settings.verify_with_llm and settings.llm_configured


async def llm_claims_check(draft: dict, evidence: dict, run_id: str | None) -> dict:
    """One optional LLM call. Never raises: returns {"checked": bool, "unsupported": [...], "reason": str|None}."""
    if not claims_enabled():
        return {"checked": False, "unsupported": [], "reason": "skipped (LLM check disabled or no API key)"}
    user = ("## Evidence\n" + wrap_untrusted(json.dumps(evidence, indent=2, default=str), source="collected evidence")
            + "\n\n## Email to check\n" + wrap_untrusted(f"Subject: {draft.get('subject', '')}\n\n{draft.get('body', '')}",
                                                         source="draft email"))
    try:
        res = await complete_json(load_prompt("verification"), user, ClaimsCheck, run_id=run_id, agent="verification")
    except Exception as e:  # noqa: BLE001   optional check: a failure is a note, not a failure
        return {"checked": False, "unsupported": [], "reason": f"skipped ({type(e).__name__}: {e})"[:200]}
    return {"checked": True, "unsupported": [] if res.supported else res.unsupported_claims, "reason": None}


def claims_evidence(lead: dict | None, profile: dict | None, audit: dict | None, proposal: dict | None) -> dict:
    return {"lead": {k: (lead or {}).get(k) for k in ("company", "reasons", "recommended_actions", "pitch_angle")},
            "company_profile": profile, "website_audit": audit and {k: audit.get(k) for k in ("summary", "issues", "opportunities")},
            "meeting_proposal": proposal}


# ------------------------------------------------------------------ per-task rule checks
def _blank(v) -> bool:
    return not (isinstance(v, str) and v.strip())


def check_research(out: dict) -> list[str]:
    p, issues = out.get("profile"), []
    if not isinstance(p, dict):
        return ["the output has no company profile"]
    for f in ("company_name", "industry", "summary"):
        if _blank(p.get(f)):
            issues.append(f"profile.{f} is empty")
    if not isinstance(p.get("services"), list):
        issues.append("profile.services is missing")
    if not out.get("evidence"):
        issues.append("no evidence URLs were recorded")
    return issues


def check_website(out: dict) -> list[str]:
    a, issues = out.get("audit"), []
    if not isinstance(a, dict):
        return ["the output has no website audit"]
    if _blank(a.get("summary")):
        issues.append("audit.summary is empty")
    found = a.get("issues") or []
    if len(found) < settings.verify_min_audit_issues:
        issues.append(f"the audit lists only {len(found)} issue(s); at least {settings.verify_min_audit_issues} are required "
                      "(include every lower-severity issue the signals and text genuinely support; do not invent any)")
    for i, it in enumerate(found, 1):
        if _blank(it.get("title")) or _blank(it.get("detail")) or it.get("severity") not in ("high", "medium", "low"):
            issues.append(f"audit issue #{i} is missing a title, detail or valid severity")
    if not isinstance(a.get("contact"), dict):
        issues.append("audit.contact is missing")
    if not out.get("evidence"):
        issues.append("no evidence URLs were recorded")
    return issues


def check_scoring(out: dict) -> list[str]:
    issues = []
    score = out.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        issues.append("score must be an integer from 0 to 100")
    elif out.get("tier") != tier_for(score):
        issues.append(f"tier '{out.get('tier')}' does not match score {score} (expected '{tier_for(score)}')")
    for f in ("company", "pitch_angle"):
        if _blank(out.get(f)):
            issues.append(f"{f} is empty")
    for f in ("reasons", "recommended_actions"):
        if not out.get(f):
            issues.append(f"{f} is empty")
    return issues


def check_scheduling(out: dict) -> list[str]:
    slots = (out.get("proposal") or {}).get("slots") or []
    issues = [] if len(slots) == 3 else [f"the meeting proposal has {len(slots)} slot(s); exactly 3 are required"]
    for s in slots:
        try:
            datetime.fromisoformat(s["start"])
        except (KeyError, ValueError, TypeError):
            issues.append(f"slot {s!r} has no valid start time")
    return issues


def check_followup(out: dict) -> list[str]:
    items = out.get("followups") or []
    issues = [] if 2 <= len(items) <= 3 else [f"{len(items)} follow-up action(s); 2 or 3 are required"]
    for it in items:
        if _blank(it.get("action")) or _blank(it.get("due_at")):
            issues.append("a follow-up has no action text or due date")
    return issues


VALIDATORS: dict[str, Callable[[dict], list[str]]] = {
    "research": check_research, "website": check_website, "scoring": check_scoring,
    "scheduling": check_scheduling, "followup": check_followup,
}
RETRY_ON_VERIFY = {"research", "website", "scoring"}      # LLM agents can use feedback; the others are deterministic


def validate_output(agent: str, output: dict) -> list[str]:
    """Rule issues for a finished task ([] = fine, or no rules for this agent)."""
    fn = VALIDATORS.get(agent)
    return fn(output) if fn else []


# ------------------------------------------------------------------ the Verification task
def _final_email(rows: dict) -> tuple[dict | None, dict | None]:
    comm = rows.get("communication")
    return (comm["out"].get("draft"), comm["out"]) if comm and comm["status"] == "succeeded" and comm["out"] else (None, None)


async def verification_agent(task: AgentTask, context: dict) -> AgentResult:
    with SessionLocal() as db:
        rows = {t.agent: {"key": t.task_key, "status": t.status, "error": t.error, "out": dict(t.output_json or {})}
                for t in db.scalars(select(Task).where(Task.run_id == task.run_id))
                if t.task_key != task.task_key and t.agent != "report"}
    checks = []
    for agent, r in sorted(rows.items(), key=lambda kv: kv[1]["key"]):
        base = {"task_key": r["key"], "agent": agent}
        if r["status"] != "succeeded":
            checks.append({**base, "result": "not_verified", "issues": [f"the task is {r['status']}: {r['error'] or 'no output'}"],
                           "warnings": []})
        elif agent == "communication":
            pass                                                           # handled below with the final text
        elif agent in VALIDATORS:
            issues = VALIDATORS[agent](r["out"])
            checks.append({**base, "result": "failed" if issues else "passed", "issues": issues, "warnings": []})
        else:
            checks.append({**base, "result": "passed", "issues": [], "warnings": []})

    claims = {"checked": False, "unsupported": [], "reason": "no email to check"}
    draft, comm_out = _final_email(rows)
    if draft:
        lead, profile_out, audit_out = (rows.get("scoring") or {}).get("out"), (rows.get("research") or {}).get("out"), \
            (rows.get("website") or {}).get("out")
        proposal = ((rows.get("scheduling") or {}).get("out") or {}).get("proposal")
        slots = (proposal or {}).get("slots")
        errors, warnings = check_email(draft, (comm_out or {}).get("company") or (lead or {}).get("company", ""), slots)
        prior = (comm_out or {}).get("verification", {}).get("claims") or {}
        edited = (comm_out or {}).get("verification", {}).get("body_sha") != body_hash(draft.get("body", ""))
        if edited and errors:       # the human is the authority on their own edits: flag, never fail the run
            warnings, errors = [f"edited by the reviewer: {e}" for e in errors] + warnings, []
        if prior.get("checked") and not edited:
            claims = {**prior, "reason": "reused: the email was not edited after the pre-approval check"}
        else:                                                              # edited by the reviewer, or never checked
            claims = await llm_claims_check(draft, claims_evidence(lead, (profile_out or {}).get("profile"),
                                                                    (audit_out or {}).get("audit"), proposal), task.run_id)
        if claims.get("unsupported"):
            warnings.append("claims not supported by the evidence: " + "; ".join(claims["unsupported"]))
        checks.append({"task_key": rows["communication"]["key"], "agent": "communication", "result": "failed" if errors else ("flagged" if warnings and edited else "passed"),
                       "issues": errors, "warnings": warnings})
    failed = [c for c in checks if c["result"] == "failed"]
    output = {"passed": not failed, "checks": checks, "claims": claims,
              "summary": f"{sum(c['result'] == 'passed' for c in checks)} of {len(checks)} checks passed"
                         + (f"; {len(failed)} failed" if failed else "")
                         + (f"; {sum(c['result'] == 'flagged' for c in checks)} flagged" if any(c['result'] == 'flagged' for c in checks) else "")}
    if failed:
        detail = "; ".join(f"{c['task_key']} {c['agent']}: " + " / ".join(c["issues"]) for c in failed)
        raise VerificationFailed(f"Verification failed: {detail}", output=output)
    return AgentResult(output=output)
