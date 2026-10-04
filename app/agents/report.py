"""Report agent: the final Markdown report, built from the database only (no LLM, so nothing can be invented).
It always runs, even when other tasks failed, so failures are listed instead of silently lost."""
from sqlalchemy import select

from app.agents._common import host_of
from app.db.models import Approval, Followup, Message, Run, Task, now
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask

ICON = {"passed": "PASS", "failed": "FAIL", "flagged": "FLAGGED", "not_verified": "SKIPPED"}


def _cell(s) -> str:
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def _bullets(items) -> list[str]:
    return [f"- {i}" for i in items] or ["- none"]


async def report_agent(task: AgentTask, context: dict) -> AgentResult:
    with SessionLocal() as db:
        run = db.get(Run, task.run_id)
        tasks = sorted(db.scalars(select(Task).where(Task.run_id == task.run_id)), key=lambda t: t.task_key)
        approvals = list(db.scalars(select(Approval).where(Approval.run_id == task.run_id).order_by(Approval.created_at)))
        followups = list(db.scalars(select(Followup).where(Followup.run_id == task.run_id).order_by(Followup.due_at)))
        messages = list(db.scalars(select(Message).where(Message.run_id == task.run_id).order_by(Message.id)))
        objective, tokens, calls, created = run.objective, run.tokens, run.llm_calls, run.created_at
        run_input = dict(run.input_json or {})
        by_agent = {t.agent: {"key": t.task_key, "status": t.status, "error": t.error, "out": dict(t.output_json or {}),
                              "attempts": t.attempts} for t in tasks if t.task_key != task.task_key}
        label = {t.task_key: f"{t.task_key} {t.agent}" for t in tasks}
        failed_or_skipped = {t.task_key for t in tasks if t.status in ("failed", "skipped")}
        failures = [(t.task_key, t.agent, t.status,
                     t.error or ("blocked because " + ", ".join(label[d] for d in t.depends_on if d in failed_or_skipped)
                                 + " did not succeed" if t.status == "skipped" else None))
                    for t in tasks if t.task_key != task.task_key and t.status in ("failed", "skipped")]
        unfinished = [(t.task_key, t.agent, t.status) for t in tasks
                      if t.task_key != task.task_key and t.status not in ("succeeded", "failed", "skipped")]
        appr = [(a.action_type, a.status, a.reviewer_note, a.payload_json.get("revision", 0)) for a in approvals]
        pending_appr = [(a.id, a.action_type) for a in approvals if a.status == "pending"]
        fu = [(f.due_at.strftime("%Y-%m-%d"), f.action, f.status) for f in followups]
        msgs = [(m.to, m.subject, m.status) for m in messages]

    out = lambda agent: (by_agent.get(agent) or {}).get("out") or {}          # noqa: E731
    lead, prof, site = out("scoring"), (out("research").get("profile") or {}), (out("website").get("audit") or {})
    sched, comm, ver = out("scheduling"), out("communication"), out("verification")
    company = lead.get("company") or prof.get("company_name") or host_of(run_input.get("url", "")) or "the company"
    problems = len(failures)
    outcome = "completed successfully" if not problems else f"finished with {problems} problem(s): see Failures"
    L = [f"# Outreach report: {company}", "",
         f"**Objective:** {objective}", "",
         f"**Outcome:** {outcome}  ", f"**Run:** `{task.run_id}` | tokens {tokens} | LLM calls {calls} | "
         f"{int((now() - created).total_seconds())} s so far", ""]

    L += ["## Lead", ""]
    if lead.get("score") is not None:
        L += [f"**Score {lead['score']}/100, tier {lead['tier']}**", "", f"Pitch angle: {lead.get('pitch_angle', '')}", "",
              "Why:", *_bullets(lead.get("reasons", [])), "", "Recommended actions:", *_bullets(lead.get("recommended_actions", []))]
    else:
        L += ["No lead score was produced. " + (f"The scoring task is {by_agent['scoring']['status']}." if "scoring" in by_agent
                                                  else "There was no scoring task in this plan.")]
    L += ["", "## Key findings", ""]
    if prof:
        L += [f"**Company:** {prof.get('company_name')} ({prof.get('industry')}). {prof.get('summary', '')}",
              f"Size cues: {prof.get('size_cues', 'n/a')}", f"Services: {', '.join(prof.get('services', [])) or 'n/a'}", ""]
    if site:
        L += [f"**Website:** {site.get('summary', '')}", "", "| Severity | Issue | Detail |", "|---|---|---|"]
        L += [f"| {_cell(i.get('severity'))} | {_cell(i.get('title'))} | {_cell(i.get('detail'))} |" for i in site.get("issues", [])]
        c = site.get("contact") or {}
        L += ["", f"Contact: {', '.join(c.get('emails', [])) or 'no email found'} | {', '.join(c.get('phones', [])) or 'no phone found'}"]
    if not prof and not site:
        L += ["No research or website findings are available (those tasks did not succeed)."]

    L += ["", "## Actions taken", ""]
    taken = []
    pages = (out("website").get("evidence") or []) + (out("research").get("evidence") or [])
    if pages:
        taken.append(f"Crawled and analysed {len(set(pages))} source page(s).")
    if lead.get("score") is not None:
        taken.append(f"Scored the lead ({lead['score']}, tier {lead['tier']}) and saved it to the leads table.")
    if sched.get("proposal"):
        taken.append("Proposed meeting slots: " + "; ".join(s["label"] for s in sched["proposal"]["slots"]) +
                     f" ({sched['proposal']['duration_min']} min). Calendar hold: " + str((sched.get("event") or {}).get("status", "n/a")) + ".")
    if comm.get("draft"):
        d = comm["draft"]
        taken.append(f"Drafted an email to {d.get('to') or '(no address found)'}: \"{d.get('subject')}\" "
                     f"(written by {comm.get('generated_by', 'n/a')}, revision {comm.get('revision', 0)}).")
    for kind, status, note, rev in appr:
        if kind == "send_email":
            taken.append(f"Human review of draft revision {rev}: **{status}**" + (f" (note: {note})" if note else "") + ".")
    if (comm.get("delivery") or {}).get("status"):
        taken.append(f"Email delivery status: **{comm['delivery']['status']}**.")
    if fu:
        taken.append(f"Scheduled {len(fu)} follow-up action(s).")
    L += _bullets(taken)

    L += ["", "## Pending items", ""]
    pend = [f"Approval `{i}` ({k}) is still waiting for a decision." for i, k in pending_appr]
    pend += [f"{k} {a} is {s}." for k, a, s in unfinished]
    pend += [f"Follow-up due {d}: {a}" for d, a, s in fu if s == "pending"]
    pend += [f"Message to {to or '(no address)'} is '{st}'." for to, sub, st in msgs if st not in ("sent", "sent (dry run)", "rejected")]
    L += _bullets(pend)

    L += ["", "## Verification", ""]
    if ver.get("checks"):
        L += [f"**{ver.get('summary')}**", "", "| Task | Result | Details |", "|---|---|---|"]
        for c in ver["checks"]:
            details = "; ".join(c.get("issues", []) + [f"warning: {w}" for w in c.get("warnings", [])]) or "all rules satisfied"
            L.append(f"| {c['task_key']} {c['agent']} | {ICON.get(c['result'], c['result'])} | {_cell(details)} |")
        cl = ver.get("claims") or {}
        L += ["", "Claims check (email vs evidence): " +
              (("no unsupported claims found" if not cl.get("unsupported") else "unsupported: " + "; ".join(cl["unsupported"]))
               if cl.get("checked") else f"not run ({cl.get('reason', 'n/a')})")]
    else:
        v = by_agent.get("verification")
        L += [("The verification task did not run: " + str(v["status"])) if v else "This plan had no verification task."]

    L += ["", "## Failures", ""]
    if failures:
        L += [f"- **{k} {a}** is **{s}**" + (f": {_cell(e)}" if e else "") for k, a, s, e in failures]
    else:
        L += ["None. Every task succeeded."]
    return AgentResult(output={"report_md": "\n".join(L).strip() + "\n", "failures": [f[0] for f in failures],
                               "evidence": []})
