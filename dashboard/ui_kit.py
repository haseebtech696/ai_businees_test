"""Shared helpers for the dashboard pages.
 - api(): talks to the backend and NEVER raises (returns Result with a friendly error instead)
 - friendly wording for statuses, steps, events and errors (no jargon, no Python class names)
 - page_guard(): so an unexpected bug shows a polite message, never a traceback"""
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import streamlit as st

log = logging.getLogger("dashboard")
API_BASE = os.getenv("API_URL", "http://127.0.0.1:8000")

DEMO = {   # "Load demo scenario" (override with DEMO_URL / DEMO_COMPANY / DEMO_EMAIL)
    "objective": "Assess this company as a sales lead and draft outreach",
    "company": os.getenv("DEMO_COMPANY", "Hawthorn Road Family Dental Clinic"),
    "url": os.getenv("DEMO_URL", "https://familydentalclinic.com.au"),
    "email": os.getenv("DEMO_EMAIL", "demo.recipient@example.com"),
}

# status -> (icon, plain-English label, badge colour). Covers task AND run statuses.
STATUS = {
    "pending": ("⬜", "Waiting", "gray"),
    "planning": ("🧠", "Planning", "violet"),
    "running": ("🔄", "Working", "blue"),
    "succeeded": ("✅", "Done", "green"),
    "failed": ("❌", "Failed", "red"),
    "skipped": ("⏭️", "Skipped", "gray"),
    "awaiting_approval": ("⏸️", "Needs your approval", "orange"),
    "approved": ("👍", "Approved", "blue"),
}
FINAL_STATUSES = ("succeeded", "failed")          # everything else may still change, so the page keeps refreshing
AGENT_LABEL = {
    "research": "Research the company", "website": "Audit the website", "scoring": "Score the lead",
    "scheduling": "Propose meeting times", "communication": "Draft the email", "followup": "Plan follow-ups",
    "verification": "Check the results", "report": "Write the report",
}
SEVERITY = {"high": "🔴 High", "medium": "🟠 Medium", "low": "🟡 Low"}
TIER = {"A": ("green", "Hot lead"), "B": ("orange", "Warm lead"), "C": ("gray", "Cold lead")}


# ------------------------------------------------------------------ talking to the backend
@dataclass
class Result:
    ok: bool
    data: Any = None
    error: str = ""


def get_client() -> httpx.Client:
    return httpx.Client(timeout=15)


def _http_error(status: int, body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):                                   # validation errors from the API
        detail = "; ".join(str(d.get("msg", d)) for d in detail)
    if status >= 500:
        return "The server hit a problem. Check the terminal running uvicorn, then try again."
    if status == 404:
        return "That item was not found. It may have been deleted."
    return str(detail or f"The request was refused (error {status}).")


def api(method: str, path: str, **kw) -> Result:
    try:
        with contextlib.closing(get_client()) as c:
            r = c.request(method, f"{API_BASE}/api{path}", **kw)
    except httpx.ConnectError:
        return Result(False, error=f"Cannot reach the backend at {API_BASE}. Start it with `uvicorn app.main:app` and try again.")
    except httpx.TimeoutException:
        return Result(False, error="The backend took too long to answer. Try again in a moment.")
    except Exception as e:  # noqa: BLE001
        log.warning("api call failed: %s %s: %s", method, path, e)
        return Result(False, error="Could not talk to the backend. Try again in a moment.")
    try:
        body = r.json()
    except ValueError:
        return Result(False, error="The backend sent a reply the dashboard could not read.")
    if r.status_code >= 400:
        return Result(False, error=_http_error(r.status_code, body))
    return Result(True, data=body)


def get_run(run_id: str) -> Result:
    return api("GET", f"/runs/{run_id}")


def get_events(run_id: str) -> list[dict]:
    r = api("GET", f"/runs/{run_id}/events")
    return r.data if r.ok else []


def list_runs() -> list[dict]:
    r = api("GET", "/runs")
    return r.data if r.ok else []


def pending_approvals(run_id: str | None = None) -> list[dict]:
    r = api("GET", "/approvals", params={"status": "pending", **({"run_id": run_id} if run_id else {})})
    return r.data if r.ok else []


def is_dry_run() -> bool:
    r = api("GET", "/config")
    return bool(r.ok and r.data.get("dry_run", True))


# ------------------------------------------------------------------ words
def friendly_error(err: str | None) -> str:
    """Turn a stored error like 'FetchError: HTTP 404 from https://x' into a sentence a person understands."""
    if not err:
        return "No details were recorded."
    text = str(err).strip().splitlines()[0]
    low = text.lower()
    if low.startswith("verificationfailed"):
        return "A quality check failed: " + re.sub(r"^\w+:\s*", "", text)[:300]
    if "timeouterror" in low or "ran longer than" in low:
        return "This step took too long and was stopped."
    if "budget exceeded" in low:
        return "This run used up its limit of AI calls and was stopped."
    if "provider error" in low or "ratelimit" in low or "429" in low:
        return "The AI service is unavailable or busy (a rate limit is likely). Wait a minute and run again."
    if "api_key" in low or "groq_api_key" in low:
        return "No AI key is configured. Add GROQ_API_KEY to the .env file and restart the backend."
    prefix = re.compile(r"^[A-Za-z_.]+(?:Error|Exception|Failed|Rejected|Blocked|Allowed):\s*")
    while prefix.match(text):                       # e.g. "PermanentError: FetchError: HTTP 404 ..." has two prefixes
        text = prefix.sub("", text, count=1)
    return text[:300] or "Something went wrong."


def badge(status: str) -> str:
    icon, label, color = STATUS.get(status, ("•", status.replace("_", " ").title(), "gray"))
    return f":{color}-badge[{icon} {label}]"


def step_label(agent: str) -> str:
    return AGENT_LABEL.get(agent, agent.replace("_", " ").title())


def parse_ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def elapsed_seconds(run: dict) -> float:
    start, end = parse_ts(run.get("created_at")), parse_ts(run.get("finished_at"))
    if not start:
        return 0.0
    return max(0.0, ((end or datetime.now(timezone.utc).replace(tzinfo=None)) - start).total_seconds())


def fmt_seconds(sec: float | None) -> str:
    if sec is None:
        return ""
    return f"{sec:.1f} s" if sec < 60 else f"{int(sec // 60)} min {int(sec % 60)} s"


def task_durations(events: list[dict]) -> dict[str, float]:
    start: dict[str, datetime] = {}
    out: dict[str, float] = {}
    for e in events:
        key, ts = (e.get("payload_json") or {}).get("task_key"), parse_ts(e.get("ts"))
        if not key or not ts:
            continue
        if e["type"] == "task_started":
            start[key] = ts
        elif e["type"] in ("task_succeeded", "task_failed", "approval_requested") and key in start:
            out[key] = (ts - start[key]).total_seconds()
    return out


def describe_event(e: dict, labels: dict[str, str]) -> tuple[str, str]:
    """(icon, sentence) for one row of the events table."""
    t, p = e["type"], e.get("payload_json") or {}
    who = labels.get(p.get("task_key")) or AGENT_LABEL.get(e.get("agent") or "", "")
    if t == "run_created":
        return "🆕", "Run created"
    if t == "run_started":
        return "🚀", "Run started"
    if t == "run_resumed":
        return "↩️", "Run resumed"
    if t == "plan_created":
        how = ("written by the AI manager" if p.get("source") == "llm"
               else "the default plan (the AI manager was unavailable)")
        return "🧠", f"Plan ready: {p.get('tasks', '?')} steps, {how}"
    if t == "task_started":
        return "▶️", f"{who} started"
    if t == "task_succeeded":
        return "✅", f"{who} finished"
    if t == "task_failed":
        return "❌", f"{who} failed. {friendly_error(p.get('error'))}"
    if t == "task_retry":
        return "🔁", f"{who} hit a problem and is trying again. {friendly_error(p.get('error'))}"
    if t == "task_skipped":
        return "⏭️", f"{who} was skipped because an earlier step failed"
    if t == "approval_requested":
        if p.get("action") == "send_email":
            return "⏸️", f"The email to {p.get('to') or '(no address yet)'} is waiting for your approval"
        return "⏸️", f"{str(p.get('action', 'An action')).replace('_', ' ').capitalize()} is waiting for your approval"
    if t == "approval_decided":
        verb = {"approve": "approved it", "edit": "edited and approved it", "reject": "rejected it"}.get(p.get("decision"), "decided")
        return "👤", f"You {verb}" + (f": “{p['note']}”" if p.get("note") else "")
    if t == "action_executed":
        return "📤", f"{str(p.get('status') or 'Action done').capitalize()}"
    if t == "draft_created":
        rev = f", revision {p['revision']}" if p.get("revision") else ""
        return "✍️", f"Email drafted (written by {'the AI' if p.get('generated_by') == 'llm' else 'a template'}{rev})"
    if t == "draft_revision_started":
        return "🔁", "Rewriting the email using your feedback"
    if t == "verification_retry":
        return "🔍", f"{who or 'A result'} failed a check and is being redone once: {'; '.join(p.get('issues', []))[:160]}"
    if t == "verification_failed":
        return "🛑", f"{who or 'A result'} failed its checks: {'; '.join(p.get('issues', []))[:160]}"
    if t == "llm_retry":
        return "🔁", "The AI answered in the wrong format, so it was asked again"
    if t == "llm_budget_exceeded":
        return "🛑", "The limit of AI calls for this run was reached"
    if t == "tool_called":
        extra = f": {p['status']}" if p.get("status") else ""
        return "🔧", f"Used the {str(p.get('tool', 'tool')).replace('_', ' ')} tool{extra}"
    if t == "lead_saved":
        return "🎯", f"Lead saved: {p.get('company')}, score {p.get('score')}, tier {p.get('tier')}"
    if t.startswith("run_"):
        status = t[4:]
        return {"succeeded": ("🏁", "Run finished successfully"), "failed": ("🏁", "Run finished with problems"),
                "awaiting_approval": ("⏸️", "Run paused: waiting for your approval")}.get(status, ("•", f"Run {status}"))
    return "•", t.replace("_", " ").capitalize()


# ------------------------------------------------------------------ validation
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if not raw or "://" in raw else "https://" + raw


def validate_new_run(objective: str, url: str, email: str) -> list[str]:
    problems = []
    if len(objective.strip()) < 3:
        problems.append("Write what you want done (at least a few words).")
    u = urlsplit(normalize_url(url))
    if not url.strip():
        problems.append("Enter the company's website address.")
    elif u.scheme not in ("http", "https") or "." not in (u.hostname or ""):
        problems.append("The website address does not look right. Example: https://example.com")
    if email.strip() and not EMAIL_RE.match(email.strip()):
        problems.append("The contact email does not look right. Example: name@company.com (or leave it empty).")
    return problems


# ------------------------------------------------------------------ page plumbing
@contextlib.contextmanager
def page_guard():
    """An unexpected bug shows a polite message, never a traceback. (st.rerun/st.stop are not Exceptions: they pass through.)"""
    try:
        yield
    except Exception as e:  # noqa: BLE001
        log.exception("dashboard page failed")
        st.error("Something went wrong while showing this page. Your run is not affected. Reload the page or try again.")
        with st.expander("Details for support"):
            st.code(f"{type(e).__name__}: {str(e)[:300]}")


def current_run_id() -> str | None:
    return st.session_state.get("run_id")


def set_run(run_id: str) -> None:
    st.session_state["run_id"] = run_id


def go(page: str) -> None:
    """Switch page; outside the full app (e.g. in a test) just stay put."""
    try:
        st.switch_page(page)
    except st.errors.StreamlitAPIException:
        pass


def link(page: str, label: str, icon: str | None = None) -> None:
    try:
        st.page_link(page, label=label, icon=icon)
    except st.errors.StreamlitAPIException:
        pass


def run_title(r: dict) -> str:
    company = (r.get("input_json") or {}).get("company") or (urlsplit(normalize_url((r.get("input_json") or {}).get("url", ""))).hostname or "")
    icon = STATUS.get(r["status"], ("•",))[0]
    return f"{icon} {r['id']}  |  {company or r['objective'][:40]}"


def run_picker(key: str) -> str | None:
    """A 'which run?' dropdown shared by Live run / Lead / Report. Shows a friendly empty state when there are no runs."""
    runs = list_runs()
    if not runs:
        st.info("There are no runs yet. Start one on the **New run** page. The demo scenario takes about a minute.")
        link("views/new_run.py", "Go to New run", "➕")
        return None
    ids = [r["id"] for r in runs]
    cur = current_run_id()
    if cur not in ids:
        cur = ids[0]
        set_run(cur)
    chosen = st.selectbox("Run", ids, index=ids.index(cur), key=f"picker_{key}",
                          format_func=lambda i: run_title(next(r for r in runs if r["id"] == i)))
    if chosen != cur:
        set_run(chosen)
    return chosen


def output_of(run: dict, agent: str) -> dict | None:
    t = next((t for t in run.get("tasks", []) if t["agent"] == agent), None)
    return (t or {}).get("output_json") or None


def task_of(run: dict, agent: str) -> dict | None:
    return next((t for t in run.get("tasks", []) if t["agent"] == agent), None)
