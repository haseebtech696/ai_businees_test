"""Human decisions on guarded actions: approve | edit | reject."""
from sqlalchemy.orm import Session

from app.core.events import emit_event, log_event
from app.core.orchestrator import context_for, execute_task, finalize_run, run_lock
from app.db.models import Approval, Message, Run, Task, now
from app.db.session import SessionLocal
from app.schemas.contracts import AgentTask, Status
from app.schemas.messaging import EmailDraft

DECISIONS = ("approve", "edit", "reject")


class ApprovalNotFound(LookupError):
    pass


class ApprovalConflict(Exception):
    pass


def decide(db: Session, approval_id: str, decision: str, *, to: str | None = None, subject: str | None = None,
           body: str | None = None, note: str | None = None) -> tuple[Approval, str]:
    """Record the decision. Returns (approval, next) where next is 'resume' or 'revise'."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    a = db.get(Approval, approval_id)
    if a is None:
        raise ApprovalNotFound(f"approval {approval_id} not found")
    if a.status != "pending":
        raise ApprovalConflict(f"approval {approval_id} was already {a.status}")

    payload = dict(a.payload_json)
    msg = db.get(Message, payload["message_id"]) if payload.get("message_id") else None
    task = db.get(Task, f"{a.run_id}:{a.task_key}")
    run = db.get(Run, a.run_id)

    if a.action_type != "send_email" and decision == "edit":
        raise ValueError(f"a {a.action_type} approval cannot be edited; approve or reject it")
    if decision == "edit":
        new = {"to": payload.get("to", "") if to is None else to,
               "subject": payload["subject"] if subject is None else subject,
               "body": payload["body"] if body is None else body}
        draft = EmailDraft.model_validate(new).model_dump()          # ValueError -> 422
        if all(draft[k] == (payload.get(k) or "") for k in ("to", "subject", "body")):
            raise ValueError("nothing was edited; use 'approve' to send the draft as it is")
        payload = {**payload, **draft, "original": payload.get("original") or
                   {k: payload.get(k) for k in ("to", "subject", "body")}}
        a.payload_json = payload
        if msg:
            msg.to, msg.subject, msg.body = draft["to"], draft["subject"], draft["body"]

    a.reviewer_note = note or ("edited by reviewer" if decision == "edit" else None)
    a.decided_at = now()
    if run:
        run.status = Status.RUNNING.value

    if decision == "reject" and a.action_type != "send_email":        # no rewrite for non-email actions
        a.status = "rejected"
        if task:
            task.status, task.error = Status.FAILED.value, f"Rejected by the reviewer: {note or 'no reason given'}"
        nxt = "resume"
    elif decision == "reject":
        a.status = "rejected"
        if msg:
            msg.status = "rejected"
        if task:
            task.status = Status.RUNNING.value                        # being revised right now
        nxt = "revise"
    else:
        a.status = "approved"
        if msg:
            msg.status = "approved"
        if task:
            task.status = Status.APPROVED.value
        nxt = "resume"
    db.commit()
    log_event(db, a.run_id, "approval_decided", "reviewer", task_key=a.task_key, approval_id=a.id,
              decision=decision, note=a.reviewer_note)
    return a, nxt


async def revise_after_reject(approval_id: str) -> None:
    """Re-run the agent ONCE with the reviewer's feedback. It produces a new draft and a new pending approval."""
    with SessionLocal() as db:
        a = db.get(Approval, approval_id)
        run_id, task_key, note, payload = a.run_id, a.task_key, a.reviewer_note, dict(a.payload_json)
        task = db.get(Task, f"{run_id}:{task_key}")
        agent, goal = task.agent, task.goal
        base_input = {**(db.get(Run, run_id).input_json or {}), **(task.input_json or {})}

    async with run_lock(run_id):
        revision = int(payload.get("revision", 0)) + 1
        emit_event(run_id, "draft_revision_started", agent, task_key=task_key, revision=revision, feedback=note)
        at = AgentTask(run_id=run_id, task_key=task_key, agent=agent, goal=goal, input={
            **base_input, "revision": revision,
            "revision_feedback": note or "The reviewer rejected this draft without a comment. Improve it.",
            "previous_draft": {k: payload.get(k) for k in ("to", "subject", "body")}})
        await execute_task(run_id, at, context_for(run_id, task_key))   # saves output, status and events
        finalize_run(run_id)
