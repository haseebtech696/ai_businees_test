"""ToolGuard: every tool has a risk level, and HIGH-risk tools never run directly.

  request_action(...)  -> LOW risk: runs now.  HIGH risk (send_email, create_event): creates a pending
                          Approval row, sets the task to awaiting_approval, logs approval_requested, and
                          returns immediately. Nothing is sent.
  execute_approved(id) -> called by the orchestrator AFTER a human approved. With DRY_RUN=true the email
                          is only marked 'sent (dry run)'; SMTP is never touched.
Unknown tools are denied (default deny)."""
import asyncio
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from enum import Enum
from typing import Awaitable, Callable, Literal

from app.core.config import settings
from app.core.errors import PermanentError
from app.core.events import log_event
from app.db.models import Approval, Message, Task
from app.db.session import SessionLocal
from app.schemas.contracts import Status
from app.schemas.messaging import EMAIL_RE


class Risk(str, Enum):
    LOW = "low"        # read-only or local bookkeeping
    HIGH = "high"      # talks to the outside world on your behalf


class ToolNotAllowed(PermanentError):
    """The tool is not registered (default deny)."""


class ToolError(PermanentError):
    """An approved action failed to execute."""


Executor = Callable[[dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk: Risk
    description: str
    executor: Executor | None = None
    dry_run_skips_approval: bool = False     # DRY_RUN: run the (harmless) executor at once instead of asking


@dataclass
class GuardDecision:
    status: Literal["executed", "pending_approval"]
    approval_id: str | None = None
    result: dict | None = None

    @property
    def pending(self) -> bool:
        return self.status == "pending_approval"


# ---------------------------------------------------------------- executors
def _message_status(message_id: int | None) -> str | None:
    if not message_id:
        return None
    with SessionLocal() as db:
        m = db.get(Message, message_id)
        return m.status if m else None


def _set_message_status(message_id: int | None, status: str) -> None:
    if not message_id:
        return
    with SessionLocal() as db:
        m = db.get(Message, message_id)
        if m:
            m.status = status
            db.commit()


def _smtp_send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


async def _send_email(payload: dict) -> dict:
    to, subject, body = payload.get("to", ""), payload["subject"], payload["body"]
    mid = payload.get("message_id")
    prior = _message_status(mid)
    if prior and prior.startswith("sent"):                      # idempotent: never send the same message twice
        return {"delivery": "skipped", "status": prior, "to": to, "already_sent": True}
    if settings.dry_run:                                         # the safety switch: no SMTP, ever
        _set_message_status(mid, "sent (dry run)")
        return {"delivery": "dry_run", "status": "sent (dry run)", "to": to}
    if not EMAIL_RE.match(to or ""):
        raise ToolError(f"Cannot send: '{to}' is not a valid recipient address. Edit the draft and add one.")
    if not (settings.smtp_host and settings.smtp_from):
        raise ToolError("Cannot send: SMTP_HOST and SMTP_FROM are not configured in .env (DRY_RUN=false).")
    try:
        await asyncio.to_thread(_smtp_send, to, subject, body)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"SMTP send failed: {type(e).__name__}: {e}") from e
    _set_message_status(mid, "sent")
    return {"delivery": "smtp", "status": "sent", "to": to}


async def _create_event(payload: dict) -> dict:
    """Calendar integration is a mock. It never leaves this machine: DRY_RUN only logs it."""
    base = {"title": payload.get("title", ""), "start": payload.get("start", "")}
    if settings.dry_run:
        return {**base, "delivery": "dry_run", "status": "logged (dry run)"}
    return {**base, "delivery": "mock", "status": "created (mock)"}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("fetch_url", Risk.LOW, "Download a public web page (SSRF-guarded)."),
    ToolSpec("scrape_page", Risk.LOW, "Fetch and parse a public web page."),
    ToolSpec("web_search", Risk.LOW, "Search the web (read-only)."),
    ToolSpec("save_draft", Risk.LOW, "Store a draft in the local database."),
    ToolSpec("send_email", Risk.HIGH, "Send an email to an outside recipient.", _send_email),
    ToolSpec("create_event", Risk.HIGH, "Create a calendar event with an outside party.", _create_event,
             dry_run_skips_approval=True),
]}


# ---------------------------------------------------------------- the guard
async def request_action(run_id: str, task_key: str, agent: str, tool: str, payload: dict) -> GuardDecision:
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolNotAllowed(f"Tool '{tool}' is not registered; it is denied by default.")

    if spec.risk is Risk.HIGH and not (spec.dry_run_skips_approval and settings.dry_run):
        approval_id = f"a_{uuid.uuid4().hex[:8]}"
        with SessionLocal() as db:
            db.add(Approval(id=approval_id, run_id=run_id, task_key=task_key, action_type=tool,
                            payload_json=payload, status="pending"))
            task = db.get(Task, f"{run_id}:{task_key}")
            if task is not None:
                task.status = Status.AWAITING_APPROVAL.value
            db.commit()
            log_event(db, run_id, "approval_requested", agent, task_key=task_key, approval_id=approval_id,
                      action=tool, risk=spec.risk.value, to=payload.get("to"), subject=payload.get("subject"))
        return GuardDecision("pending_approval", approval_id)

    result = await spec.executor(payload) if spec.executor else {}
    with SessionLocal() as db:
        log_event(db, run_id, "tool_called", agent, task_key=task_key, tool=tool, risk=spec.risk.value,
                  status=(result or {}).get("status"))
    return GuardDecision("executed", None, result)


async def execute_approved(approval_id: str) -> dict:
    """Run the guarded action of an APPROVED approval. Refuses anything else."""
    with SessionLocal() as db:
        a = db.get(Approval, approval_id)
        if a is None:
            raise ToolError(f"Approval {approval_id} not found.")
        if a.status != "approved":
            raise ToolError(f"Approval {approval_id} is '{a.status}', not approved: refusing to run {a.action_type}.")
        tool, payload = a.action_type, dict(a.payload_json)
    spec = TOOLS.get(tool)
    if spec is None or spec.executor is None:
        raise ToolNotAllowed(f"Tool '{tool}' cannot be executed.")
    return await spec.executor(payload)
