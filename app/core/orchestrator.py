"""Orchestrator. State lives in the DB, never in memory:
every loop iteration re-reads the tasks table, runs everything that is ready *in parallel*,
and writes the Task row + an Event row after each step. Calling run_workflow(run_id) again
resumes from whatever the DB says (this is what makes approval pause/resume easy in Phase 4)."""
import asyncio
import contextlib
import uuid
import weakref

from sqlalchemy import select

from app.agents.base import AGENTS
from app.agents.manager import make_plan_ex
from app.agents.verification import RETRY_ON_VERIFY, validate_output
from app.core.config import settings
from app.core.errors import PermanentError
from app.core.events import emit_event, log_event
from app.core.tool_guard import execute_approved
from app.db.models import Approval, Run, Task, now
from app.db.session import SessionLocal
from app.schemas.contracts import AgentResult, AgentTask, Plan, Status

S = Status
TERMINAL = (S.SUCCEEDED, S.FAILED, S.SKIPPED)
ALWAYS_RUN = {"report"}        # runs once every dependency is finished, even if some failed, so failures get reported

# One workflow at a time per run: a resume (approval) must wait for a still-running workflow to finish.
_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


@contextlib.asynccontextmanager
async def run_lock(run_id: str):
    loop = asyncio.get_running_loop()
    try:
        lock = _LOCKS.setdefault(loop, {}).setdefault(run_id, asyncio.Lock())
    except TypeError:                       # loop type that cannot be weak-referenced: run unlocked
        yield
        return
    async with lock:
        yield


def _ancestors(by_key: dict, task: Task) -> list[str]:
    """Every task upstream of `task` (direct and indirect), nearest first."""
    seen: list[str] = []
    queue = list(task.depends_on)
    while queue:
        k = queue.pop(0)
        if k not in seen:
            seen.append(k)
            queue.extend(by_key[k].depends_on)
    return seen


def context_for(run_id: str, task_key: str) -> dict:
    """{upstream task_key: output} for a task, read from the DB."""
    with SessionLocal() as db:
        by_key = {t.task_key: t for t in db.scalars(select(Task).where(Task.run_id == run_id))}
        return {k: (by_key[k].output_json or {}) for k in _ancestors(by_key, by_key[task_key])}


def _add_task_rows(run: Run, plan: Plan) -> None:
    for t in plan.tasks:
        if t.agent not in AGENTS:
            raise ValueError(f"unknown agent '{t.agent}'")
    for t in plan.tasks:
        run.tasks.append(Task(id=f"{run.id}:{t.task_key}", run_id=run.id, task_key=t.task_key,
                              agent=t.agent, goal=t.goal, depends_on=t.depends_on,
                              input_json=t.input, status=S.PENDING.value))


def create_run(db, objective: str, input: dict | None = None, plan: Plan | None = None) -> Run:
    """Persist a run. Without a plan, the Manager builds one when the workflow starts.
    (Passing a plan explicitly skips the Manager; used by tests.)"""
    run_id = f"r_{uuid.uuid4().hex[:8]}"
    run = Run(id=run_id, objective=objective, input_json=input or {}, status=S.PENDING.value,
              plan_json=plan.model_dump() if plan else None)
    if plan:
        _add_task_rows(run, plan)
    db.add(run)
    db.commit()
    log_event(db, run_id, "run_created", tasks=len(plan.tasks) if plan else 0)
    return run


def _persist_plan(run_id: str, plan: Plan, meta: dict) -> None:
    """Save plan_json, create the Task rows, log plan_created."""
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        run.plan_json = plan.model_dump()
        _add_task_rows(run, plan)
        db.commit()
        log_event(db, run_id, "plan_created", "manager", source=meta["source"], reason=meta["reason"],
                  tasks=len(plan.tasks), agents=[t.agent for t in plan.tasks])


def _claim_ready_tasks(run_id: str) -> list[tuple[AgentTask, dict]]:
    """Read the DB, mark blocked tasks skipped, mark every ready task running, return them."""
    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.run_id == run_id)).all()
        by_key = {t.task_key: t for t in tasks}
        run_input = dict(db.get(Run, run_id).input_json or {})     # run-level input (url, recipient_email...)

        changed = True
        while changed:  # propagate failures downstream
            changed = False
            for t in tasks:
                if t.status == S.PENDING and t.agent not in ALWAYS_RUN and any(
                        by_key[d].status in (S.FAILED, S.SKIPPED) for d in t.depends_on):
                    t.status = S.SKIPPED.value
                    db.commit()
                    log_event(db, run_id, "task_skipped", t.agent, task_key=t.task_key)
                    changed = True

        ready = [t for t in tasks if t.status == S.PENDING and (
                     all(by_key[d].status in TERMINAL for d in t.depends_on) if t.agent in ALWAYS_RUN
                     else all(by_key[d].status == S.SUCCEEDED for d in t.depends_on))]
        batch = []
        for t in ready:
            t.status = S.RUNNING.value
            db.commit()
            log_event(db, run_id, "task_started", t.agent, task_key=t.task_key)
            at = AgentTask(run_id=run_id, task_key=t.task_key, agent=t.agent,
                           goal=t.goal, input={**run_input, **(t.input_json or {})})
            ctx = {k: (by_key[k].output_json or {}) for k in _ancestors(by_key, t)}   # direct + indirect
            batch.append((at, ctx))
        return batch


def _save(run_id: str, task_key: str, event_type: str, agent: str, *, tokens: int = 0,
          event_extra: dict | None = None, **fields) -> None:
    """(a) update the Task row, (b) insert an Event row."""
    with SessionLocal() as db:
        task = db.get(Task, f"{run_id}:{task_key}")
        for k, v in fields.items():
            setattr(task, k, v)
        if tokens:
            db.get(Run, run_id).tokens += tokens
        db.commit()
        if event_type:
            log_event(db, run_id, event_type, agent, task_key=task_key, **(event_extra or {}))


async def _execute_task(run_id: str, at: AgentTask, ctx: dict) -> None:
    """Run one task with: a per-task timeout, retries for transient errors, and per-task verification.
    Verification: a finished task whose output breaks its rules is re-run ONCE with the issues as feedback;
    if it still breaks them the task is marked failed (its output is kept) and the run carries on."""
    agent_fn = AGENTS[at.agent]
    calls = errors = 0
    verify_retried = False
    while True:
        calls += 1
        try:
            result = AgentResult.model_validate(                       # enforce the contract
                await asyncio.wait_for(agent_fn(at, ctx), timeout=settings.task_timeout_s))
            if result.status == "error":
                raise RuntimeError(result.error or "agent returned error")
        except Exception as e:  # noqa: BLE001
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                last_error = f"TimeoutError: the {at.agent} task ran longer than the {settings.task_timeout_s:g}s limit"
            else:
                last_error = f"{type(e).__name__}: {e}"
            errors += 1
            if errors < settings.max_task_attempts and not isinstance(e, PermanentError):
                _save(run_id, at.task_key, "task_retry", at.agent, attempts=calls,
                      error=last_error, event_extra={"error": last_error, "attempt": errors})
                await asyncio.sleep(settings.retry_backoff_s * errors)   # let rate limits clear
                continue
            kept = {"output_json": e.output} if getattr(e, "output", None) is not None else {}
            _save(run_id, at.task_key, "task_failed", at.agent, attempts=calls, status=S.FAILED.value,
                  error=last_error, event_extra={"error": last_error}, **kept)
            return

        if result.status == "needs_approval":      # the ToolGuard already logged approval_requested
            _save(run_id, at.task_key, None, at.agent, tokens=result.tokens, attempts=calls,
                  status=S.AWAITING_APPROVAL.value, output_json=result.output, error=None)
            return

        issues = validate_output(at.agent, result.output)
        if issues:
            if at.agent in RETRY_ON_VERIFY and not verify_retried:
                verify_retried = True
                emit_event(run_id, "verification_retry", at.agent, task_key=at.task_key, issues=issues)
                at = at.model_copy(update={"input": {**at.input, "verification_feedback": "; ".join(issues)}})
                continue
            err = "VerificationFailed: " + "; ".join(issues) + (" (after one retry)" if verify_retried else "")
            _save(run_id, at.task_key, "task_failed", at.agent, tokens=result.tokens, attempts=calls,
                  status=S.FAILED.value, output_json=result.output, error=err,
                  event_extra={"error": err, "verification": True})
            return

        _save(run_id, at.task_key, "task_succeeded", at.agent, tokens=result.tokens, attempts=calls,
              status=S.SUCCEEDED.value, output_json=result.output, error=None, event_extra={"tokens": result.tokens})
        return


execute_task = _execute_task


async def _run_approved_actions(run_id: str) -> None:
    """Tasks a human approved: run the guarded action now (DRY_RUN or SMTP), then mark them succeeded."""
    with SessionLocal() as db:
        approved = [(t.task_key, t.agent, dict(t.output_json or {}))
                    for t in db.scalars(select(Task).where(Task.run_id == run_id, Task.status == S.APPROVED.value))]
    for task_key, agent, output in approved:
        with SessionLocal() as db:
            approval = db.scalars(select(Approval).where(
                Approval.run_id == run_id, Approval.task_key == task_key, Approval.status == "approved"
            ).order_by(Approval.decided_at.desc())).first()
            info = (approval.id, approval.action_type, dict(approval.payload_json)) if approval else None
        if info is None:
            _save(run_id, task_key, "task_failed", agent, status=S.FAILED.value,
                  error="PermanentError: task was approved but no approved action was found",
                  event_extra={"error": "no approved action"})
            continue
        approval_id, action, payload = info
        try:
            delivery = await execute_approved(approval_id)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            _save(run_id, task_key, "task_failed", agent, status=S.FAILED.value, error=err, event_extra={"error": err})
            continue
        if action == "send_email":
            new_output = {**output, "draft": {k: payload.get(k) for k in ("to", "subject", "body")},
                          "delivery": delivery, "approval_id": approval_id, "approved_by": "human"}
        else:
            new_output = {**output, "event": delivery, "approval_id": approval_id, "approved_by": "human"}
        _save(run_id, task_key, "action_executed", agent, status=S.SUCCEEDED.value, error=None,
              output_json=new_output,
              event_extra={"approval_id": approval_id, "tool": action, **delivery})


def _finalize(run_id: str) -> None:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        statuses = [t.status for t in run.tasks]
        rep = next((t.output_json for t in run.tasks if t.agent == "report" and t.status == S.SUCCEEDED
                    and t.output_json), None)
        if rep:                                    # saved even when other tasks failed: it lists the failures
            run.report_md = rep.get("report_md")
        if S.FAILED in statuses:
            run.status = S.FAILED.value
        elif S.AWAITING_APPROVAL in statuses:      # not done, not failed: the workflow exits and waits
            run.status = S.AWAITING_APPROVAL.value
        elif all(s == S.SUCCEEDED for s in statuses):
            run.status = S.SUCCEEDED.value
        else:
            run.status = S.FAILED.value
        if run.status in (S.SUCCEEDED, S.FAILED):
            run.finished_at = now()
        db.commit()
        log_event(db, run_id, f"run_{run.status}",
                  failed_tasks=[t.task_key for t in run.tasks if t.status == S.FAILED])


finalize_run = _finalize


async def _run_workflow(run_id: str) -> None:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        objective, run_input = run.objective, run.input_json or {}
        needs_plan = len(run.tasks) == 0
        resumed = any(t.status == S.SUCCEEDED for t in run.tasks)
        for t in run.tasks:                      # crash recovery: a 'running' row has no live worker
            if t.status == S.RUNNING:
                t.status = S.PENDING.value
        run.status = (S.PLANNING if needs_plan else S.RUNNING).value
        db.commit()
        log_event(db, run_id, "run_resumed" if resumed else "run_started")

    if needs_plan:                               # Manager first: objective -> Plan -> Task rows
        plan, meta = await make_plan_ex(objective, run_input, run_id=run_id)
        _persist_plan(run_id, plan, meta)
        with SessionLocal() as db:
            db.get(Run, run_id).status = S.RUNNING.value
            db.commit()

    while True:
        await _run_approved_actions(run_id)          # approved by a human -> execute now
        batch = _claim_ready_tasks(run_id)
        if not batch:
            break
        await asyncio.gather(*(_execute_task(run_id, at, ctx) for at, ctx in batch))

    _finalize(run_id)


async def run_workflow(run_id: str) -> None:
    async with run_lock(run_id):
        await _run_workflow(run_id)


async def resume_workflow(run_id: str) -> None:
    """Pick up from the DB after an approval: approved actions run, pending tasks continue."""
    await run_workflow(run_id)
