"""Dashboard tests: each page is run with Streamlit's AppTest against the real API (in-process, fake internet/LLM)."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.agents.base import AGENTS
from app.core.errors import PermanentError
from app.main import app
from app.schemas.contracts import Status

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "dashboard"))        # appended, never first: dashboard/app.py must not shadow the `app` package
import ui_kit as ui  # noqa: E402


@pytest.fixture(autouse=True)
def backend(monkeypatch):
    monkeypatch.setattr(ui, "get_client", lambda: TestClient(app))


def page(name: str) -> AppTest:
    return AppTest.from_file(str(ROOT / "dashboard" / "views" / f"{name}.py"), default_timeout=30)


def api() -> TestClient:
    return TestClient(app)


def make_run(approve=False, url="https://acme.example", company="Acme Dental") -> str:
    rid = api().post("/api/runs", json={"objective": "Assess this company", "input": {"url": url, "company": company}}).json()["run_id"]
    if approve:
        for a in api().get("/api/approvals", params={"status": "pending", "run_id": rid}).json():
            assert api().post(f"/api/approvals/{a['id']}/decision", json={"decision": "approve"}).status_code == 200
    return rid


def failed_run(monkeypatch) -> str:
    async def dead(task, context):
        raise PermanentError("FetchError: HTTP 404 from https://dead.example/")
    monkeypatch.setitem(AGENTS, "website", dead)
    rid = make_run(url="https://dead.example")
    assert api().get(f"/api/runs/{rid}").json()["status"] == "failed"
    return rid


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [e.value for e in at.error] + [w.value for w in at.warning] \
        + [i.value for i in at.info] + [s.value for s in at.success] + [c.value for c in at.caption] \
        + [t.value for t in at.title] + [h.value for h in at.header] + [h.value for h in at.subheader]
    return "\n".join(parts)


def shown_ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert "Traceback" not in texts(at) and "File \"" not in texts(at)


# ------------------------------------------------------------------ words and rules
def test_every_status_the_backend_can_produce_has_a_label():
    assert {s.value for s in Status} <= set(ui.STATUS)


@pytest.mark.parametrize("raw,expected", [
    ("FetchError: HTTP 404 from https://x.example/", "HTTP 404 from https://x.example/"),
    ("VerificationFailed: the audit lists only 2 issue(s) (after one retry)", "A quality check failed: the audit lists only 2 issue(s)"),
    ("TimeoutError: the website task ran longer than the 90s limit", "This step took too long and was stopped."),
    ("LLMBudgetExceeded: LLM call budget exceeded: 12 calls", "This run used up its limit of AI calls and was stopped."),
    ("LLMError: provider error: RateLimitError: 429 too many requests", "The AI service is unavailable or busy"),
    ("LLMConfigError: GROQ_API_KEY is not set (add it to .env)", "No AI key is configured"),
    ("ToolError: Cannot send: 'x' is not a valid recipient address.", "Cannot send: 'x' is not a valid recipient address."),
    (None, "No details were recorded."),
])
def test_errors_are_translated_into_plain_sentences(raw, expected):
    assert ui.friendly_error(raw).startswith(expected)


def test_error_text_is_a_single_short_line_even_for_a_traceback():
    out = ui.friendly_error("RuntimeError: boom\nTraceback (most recent call last):\n  File \"x.py\", line 1\n" + "x" * 900)
    assert out == "boom" and "\n" not in out


@pytest.mark.parametrize("url,email,ok", [
    ("https://example.com", "", True), ("example.com", "a@b.co", True), ("", "", False), ("not a url", "", False),
    ("ftp://example.com", "", False), ("https://example.com", "nope", False)])
def test_new_run_validation(url, email, ok):
    assert (ui.validate_new_run("Assess this company", url, email) == []) is ok
    assert ui.validate_new_run("x", "https://example.com", "")                      # a too-short objective is rejected


def test_durations_and_formatting():
    ev = [{"type": "task_started", "ts": "2026-10-03T10:00:00", "payload_json": {"task_key": "t_01"}},
          {"type": "task_succeeded", "ts": "2026-10-03T10:00:04.500000", "payload_json": {"task_key": "t_01"}}]
    assert ui.task_durations(ev) == {"t_01": 4.5}
    assert ui.fmt_seconds(4.54) == "4.5 s" and ui.fmt_seconds(125) == "2 min 5 s" and ui.fmt_seconds(None) == ""


def test_the_page_keeps_refreshing_until_the_run_is_really_finished():
    assert all(s not in ui.FINAL_STATUSES for s in ("pending", "planning", "running", "awaiting_approval"))
    assert set(ui.FINAL_STATUSES) == {"succeeded", "failed"}


def test_every_kind_of_event_is_described_in_words():
    rid = make_run()
    a = api().get("/api/approvals", params={"run_id": rid}).json()[0]
    api().post(f"/api/approvals/{a['id']}/decision", json={"decision": "approve", "note": "ok"})
    events = api().get(f"/api/runs/{rid}/events").json()
    labels = {t["task_key"]: ui.step_label(t["agent"]) for t in api().get(f"/api/runs/{rid}").json()["tasks"]}
    seen = set()
    for e in events:
        icon, text = ui.describe_event(e, labels)
        assert icon and text and text != e["type"] and "{" not in text and "_" not in text.split(":")[0], (e["type"], text)
        seen.add(e["type"])
    assert {"plan_created", "task_started", "task_succeeded", "approval_requested", "approval_decided", "action_executed",
            "draft_created", "run_succeeded"} <= seen
    for t in ("task_failed", "task_retry", "task_skipped", "verification_retry", "verification_failed", "llm_retry",
              "llm_budget_exceeded", "tool_called", "lead_saved", "draft_revision_started", "run_awaiting_approval", "mystery_event"):
        icon, text = ui.describe_event({"type": t, "agent": "website", "payload_json": {"task_key": "t_01", "error": "FetchError: HTTP 500"}}, labels)
        assert icon and text


# ------------------------------------------------------------------ New run
def test_demo_button_fills_in_the_form_and_starting_creates_a_run():
    at = page("new_run").run()
    shown_ok(at)
    assert at.session_state["nr_url"] == ""
    next(b for b in at.button if "demo" in b.label.lower()).click().run()
    assert at.session_state["nr_url"] == ui.DEMO["url"] and at.session_state["nr_company"] == ui.DEMO["company"]
    assert at.session_state["nr_email"] == ui.DEMO["email"] and at.text_input(key="nr_url").value == ui.DEMO["url"]
    next(b for b in at.button if "Start run" in b.label).click().run()
    shown_ok(at)
    rid = at.session_state["run_id"]
    run = api().get(f"/api/runs/{rid}").json()
    assert run["input_json"] == {"url": ui.DEMO["url"], "company": ui.DEMO["company"], "recipient_email": ui.DEMO["email"]}
    assert run["status"] == "awaiting_approval"                                   # the demo reaches the approval step


def test_bad_form_input_shows_friendly_messages_and_starts_nothing():
    before = len(api().get("/api/runs").json())
    at = page("new_run").run()
    at.text_input(key="nr_url").set_value("not a url").run()
    at.text_input(key="nr_email").set_value("nope").run()
    next(b for b in at.button if "Start run" in b.label).click().run()
    shown_ok(at)
    msgs = " ".join(e.value for e in at.error)
    assert "website address does not look right" in msgs and "contact email does not look right" in msgs
    assert len(api().get("/api/runs").json()) == before and "run_id" not in at.session_state


def test_new_run_page_explains_itself_and_the_safety_mode():
    at = page("new_run").run()
    t = texts(at)
    assert "no real email is ever sent" in t and "pauses and waits for you" in t.replace("**", "") or "waits for you" in t
    assert [e.label for e in at.expander] == ["How it works"]


# ------------------------------------------------------------------ Live run
def live(rid: str) -> AppTest:
    at = page("live_run")
    at.session_state["run_id"] = rid
    return at.run()


def test_live_run_shows_checklist_timeline_and_json_for_every_step():
    rid = make_run()
    at = live(rid)
    shown_ok(at)
    t = texts(at)
    for step in ("Research the company", "Audit the website", "Score the lead", "Propose meeting times", "Draft the email",
                 "Plan follow-ups", "Check the results", "Write the report"):
        assert step in t
    assert "✅ **Research the company**" in t and "⏸️ **Draft the email**" in t and "⬜ **Write the report**" in t
    assert "waiting for you" in t and "Nothing is sent until you decide" in t
    assert "waiting for your approval" in t                                           # the timeline sentence
    assert [m.value for m in at.metric if m.label == "Steps done"] == ["4 of 8"]
    labels = [e.label for e in at.expander]
    assert sum("(done)" in x or "(waiting)" in x or "(needs your approval)" in x for x in labels) == 8
    assert len(at.json) >= 16                                                         # input + output per step
    assert "updates by itself every 2 seconds" in t                                   # still live: it refreshes itself


def test_live_run_when_finished_celebrates_and_stops_refreshing():
    rid = make_run(approve=True)
    at = live(rid)
    shown_ok(at)
    t = texts(at)
    assert "finished successfully" in t and "updates by itself" not in t
    assert all(f"✅ **{s}**" in t for s in ("Draft the email", "Write the report"))
    assert any(m.value == "8 of 8" for m in at.metric)


def test_live_run_of_a_failed_run_explains_what_went_wrong_in_plain_words(monkeypatch):
    rid = failed_run(monkeypatch)
    at = live(rid)
    shown_ok(at)
    t = texts(at)
    assert "finished with problems" in t and "HTTP 404 from https://dead.example/" in t and "FetchError" not in t
    assert "❌ **Audit the website**" in t and "⏭️ **Score the lead**" in t


def test_live_run_with_no_runs_or_an_unknown_run_is_friendly(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(ui, "list_runs", lambda: [])
        at = page("live_run").run()
        shown_ok(at)
        assert "no runs yet" in texts(at).lower()
    at = live("r_does_not_exist")
    shown_ok(at)                                                                       # falls back to the newest run


# ------------------------------------------------------------------ Lead
def test_lead_page_shows_score_tier_reasons_and_issues():
    rid = make_run()
    at = page("lead")
    at.session_state["run_id"] = rid
    at.run()
    shown_ok(at)
    t = texts(at)
    assert [m.value for m in at.metric if m.label == "Lead score"] == ["80 / 100"]
    assert ":green-badge[Tier A: Hot lead]" in t and "Pitch angle" in t and "No online booking" in t
    assert "Why this score" in t and "What to do next" in t and "Email the owner" in t
    rows = at.dataframe[0].value
    assert len(rows) == 3 and set(rows.columns) == {"Severity", "Issue", "Detail"} and "🔴 High" in set(rows["Severity"])


def test_lead_page_explains_when_there_is_no_lead(monkeypatch):
    rid = failed_run(monkeypatch)
    at = page("lead")
    at.session_state["run_id"] = rid
    at.run()
    shown_ok(at)
    assert "No lead score was produced" in texts(at)


# ------------------------------------------------------------------ Report
def test_report_page_renders_the_report_and_offers_a_download():
    rid = make_run(approve=True)
    at = page("report")
    at.session_state["run_id"] = rid
    at.run()
    shown_ok(at)
    assert any(m.value.startswith("# Outreach report") for m in at.markdown) and not at.code   # rendered Markdown, not a code dump
    assert [d.label for d in at.download_button] == ["⬇ Download report (.md)"]


def test_report_page_before_the_report_exists_says_what_to_do():
    rid = make_run()
    at = page("report")
    at.session_state["run_id"] = rid
    at.run()
    shown_ok(at)
    assert "after you decide on the email" in texts(at) and not at.download_button


def test_report_page_of_a_failed_run_warns_and_lists_failures(monkeypatch):
    rid = failed_run(monkeypatch)
    at = page("report")
    at.session_state["run_id"] = rid
    at.run()
    shown_ok(at)
    assert "This run had problems" in texts(at) and "HTTP 404" in texts(at) and len(at.download_button) == 1


# ------------------------------------------------------------------ History
def test_history_lists_runs_filters_them_and_opens_one(monkeypatch):
    ok, bad = make_run(approve=True), failed_run(monkeypatch)
    at = page("history").run()
    shown_ok(at)
    table = at.dataframe[0].value
    assert {"Run", "Company", "Objective", "Status", "Duration", "AI calls", "Tokens"} <= set(table.columns)
    assert {ok, bad} <= set(table["Run"])
    at.selectbox[0].select("Failed").run()
    assert set(at.dataframe[0].value["Status"]) == {"❌ Failed"} and bad in set(at.dataframe[0].value["Run"])
    at.selectbox[1].select(bad).run()
    next(b for b in at.button if "Report" in b.label).click().run()
    assert at.session_state["run_id"] == bad


def test_history_with_no_runs_is_friendly(monkeypatch):
    monkeypatch.setattr(ui, "list_runs", lambda: [])
    at = page("history").run()
    shown_ok(at)
    assert "No runs yet" in texts(at)


# ------------------------------------------------------------------ Approvals + sidebar badge
def test_approvals_page_approve_resumes_the_run_and_the_badge_goes_away():
    rid = make_run()
    app_at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30).run()
    shown_ok(app_at)
    assert any("waiting for your approval" in w.value for w in app_at.sidebar.warning)   # the count badge in the sidebar

    at = page("approvals").run()
    shown_ok(at)
    aid = api().get("/api/approvals", params={"status": "pending", "run_id": rid}).json()[0]["id"]
    t = texts(at)
    assert "Automatic checks passed" in t and any(f"approve_{aid}" == b.key for b in at.button)
    next(b for b in at.button if b.key == f"approve_{aid}").click().run()
    shown_ok(at)
    assert api().get(f"/api/runs/{rid}").json()["status"] == "succeeded"
    assert api().get("/api/approvals", params={"status": "pending", "run_id": rid}).json() == []
    assert not any(f"{rid}" in w.value for w in at.warning)


def test_approvals_reject_needs_no_special_knowledge_and_edit_requires_a_change():
    rid = make_run()
    at = page("approvals").run()
    aid = api().get("/api/approvals", params={"status": "pending", "run_id": rid}).json()[0]["id"]
    next(b for b in at.button if b.key == f"edit_{aid}").click().run()
    assert "You have not changed anything yet" in texts(at)                               # nothing sent to the API
    assert api().get(f"/api/approvals/{aid}").json()["status"] == "pending"
    at.text_input(key=f"subject_{aid}").set_value("A brand new subject line").run()
    next(b for b in at.button if b.key == f"edit_{aid}").click().run()
    shown_ok(at)
    assert api().get(f"/api/approvals/{aid}").json()["status"] == "approved"
    assert api().get(f"/api/approvals/{aid}").json()["payload_json"]["subject"] == "A brand new subject line"


def test_approvals_page_with_nothing_waiting_is_friendly(monkeypatch):
    monkeypatch.setattr(ui, "pending_approvals", lambda run_id=None: [])
    at = page("approvals").run()
    shown_ok(at)
    assert "Nothing is waiting for you" in texts(at)


# ------------------------------------------------------------------ nothing ever shows a traceback
VIEWS = ["new_run", "live_run", "lead", "approvals", "report", "history"]


@pytest.mark.parametrize("view", VIEWS)
def test_backend_down_gives_a_friendly_message_on_every_page(view, monkeypatch):
    def down():
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(ui, "get_client", down)
    at = page(view)
    at.session_state["run_id"] = "r_x"
    at.run()
    shown_ok(at)


def test_entry_page_when_the_backend_is_down_says_how_to_start_it(monkeypatch):
    def down():
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(ui, "get_client", down)
    at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30).run()
    shown_ok(at)
    assert "cannot reach the backend" in texts(at) and "uvicorn app.main:app" in texts(at)


@pytest.mark.parametrize("view", ["live_run", "lead", "report", "history", "approvals"])
def test_an_unexpected_bug_becomes_a_polite_message_not_a_traceback(view, monkeypatch):
    rid = make_run()

    def boom(*a, **k):
        raise RuntimeError("secret internal failure")
    monkeypatch.setattr(ui, "run_picker", boom) if view in ("live_run", "lead", "report") else monkeypatch.setattr(ui, "list_runs", boom) \
        if view == "history" else monkeypatch.setattr(ui, "pending_approvals", boom)
    at = page(view)
    at.session_state["run_id"] = rid
    at.run()
    assert not at.exception                                                              # Streamlit never saw an error
    t = texts(at)
    assert "Something went wrong while showing this page" in t and "Traceback" not in t and "File \"" not in t


def test_streamlit_is_configured_to_hide_error_details():
    text = (ROOT / ".streamlit" / "config.toml").read_text()
    assert 'showErrorDetails = "none"' in text


def test_api_errors_never_leak_internal_text(monkeypatch):
    class Boom:
        def request(self, *a, **k):
            return httpx.Response(500, json={"detail": "Traceback (most recent call last): secret"})

        def close(self):
            pass
    monkeypatch.setattr(ui, "get_client", lambda: Boom())
    r = ui.api("GET", "/runs")
    assert not r.ok and "Traceback" not in r.error and "secret" not in r.error
