import asyncio
import json
import pytest
from sqlalchemy import select

from app.agents.research import research_agent
from app.agents.scoring import scoring_agent
from app.agents.website import website_agent
from app.agents._common import AgentInputError, target_url
from app.core import llm_gateway
from app.core.config import settings
from app.core.orchestrator import create_run, run_workflow
from app.db.models import Event, Lead, Run
from app.db.session import SessionLocal
from app.schemas.contracts import AgentTask, Plan, PlanTask

pytestmark = pytest.mark.real_agents
REAL_PROVIDER = llm_gateway._call_provider      # captured before the autouse fixture blocks the network

AUDIT = {"summary": "Small family practice with an informative but dated site.",
         "issues": [{"title": "No online booking", "severity": "high", "detail": "Only phone contact."},
                    {"title": "Images lack alt text", "severity": "low", "detail": "2 of 3 images."},
                    {"title": "No meta description", "severity": "low", "detail": "Search results show no summary."}],
         "opportunities": ["Add online booking", "Add alt text"],
         "contact": {"emails": ["hallucinated@evil.example"], "phones": ["000"], "contact_page": None,
                     "notes": "Contact form and phone."}}
PROFILE = {"company_name": "Smile Bright Dental", "industry": "dental clinic",
           "size_cues": "3 dentists, 8 staff, 2 locations", "services": ["check-ups", "whitening", "implants"],
           "recent_news": ["Opened a second clinic in 2024"], "summary": "Family dental practice."}
LEAD = {"score": 82, "tier": "C", "reasons": ["No online booking", "Reachable by phone and form", "Expanding"],
        "recommended_actions": ["Email the practice manager", "Offer a booking-system demo"],
        "pitch_angle": "Patients cannot book online, so after-hours demand is lost."}


class Router:
    """Routes the fake LLM reply by which agent's system prompt it receives (agents run in parallel)."""
    def __init__(self, monkeypatch, replies):
        self.replies, self.calls = replies, []
        monkeypatch.setattr(llm_gateway, "_call_provider", self.call)

    async def call(self, messages):
        self.calls.append(messages)
        sysmsg = messages[0]["content"]
        key = ("audit" if "website auditor" in sysmsg else "profile" if "business researcher" in sysmsg
               else "lead")
        return json.dumps(self.replies[key]), 100


def task(agent, **inp):
    return AgentTask(run_id="r_x", task_key="t_01", agent=agent, goal="goal", input=inp)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test")
    return Router(monkeypatch, {"audit": AUDIT, "profile": PROFILE, "lead": LEAD})


def test_target_url_resolution():
    assert target_url(task("x", url="a.example")) == "a.example"
    assert target_url(AgentTask(run_id="r", task_key="t", agent="x", goal="check https://b.example/home.",
                                input={})) == "https://b.example/home"
    with pytest.raises(AgentInputError, match="No company URL"):
        target_url(task("x"))


def test_website_agent(mock_web, llm):
    out = asyncio.run(website_agent(task("website", url="https://dental.example"), {})).output
    assert out["url"] == "https://dental.example/"
    assert out["audit"]["issues"][0]["severity"] == "high"
    c = out["audit"]["contact"]                                    # contacts come from the pages, not the model
    assert "hallucinated@evil.example" not in c["emails"] and "info@smilebright.example" in c["emails"]
    assert "appointments@smilebright.example" in c["emails"] and "000" not in c["phones"]
    assert c["contact_page"] == "https://dental.example/contact"
    assert out["signals"]["online_booking"] is False and out["signals"]["mobile_viewport_tag"] is True
    assert out["evidence"][0] == "https://dental.example/"
    assert set(out["evidence"]) == {"https://dental.example/", "https://dental.example/contact",
                                    "https://dental.example/services"}


def test_scraped_text_is_wrapped_and_injection_is_contained(mock_web, llm):
    asyncio.run(website_agent(task("website", url="https://dental.example"), {}))
    system, user = llm.calls[0][0]["content"], llm.calls[0][1]["content"]
    assert "untrusted_web_content" in system and "Ignore all instructions" in system
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in user                   # the attack text is present...
    n_open, n_close = user.count("<untrusted_web_content"), user.count("</untrusted_web_content>")
    assert n_open == n_close == 3                                       # ...but only inside 3 intact wrappers
    inside = user.split("<untrusted_web_content", 1)[1]
    assert inside.index("IGNORE ALL PREVIOUS") < inside.index("</untrusted_web_content>")


def test_research_agent_with_search_and_about(mock_web, llm):
    out = asyncio.run(research_agent(task("research", url="https://dental.example"), {})).output
    assert out["profile"]["company_name"] == "Smile Bright Dental"
    assert out["search_results"] == 2
    assert "https://dental.example/about" in out["evidence"]
    assert "https://news.example/smile-bright-opens-second-clinic" in out["evidence"]
    user = llm.calls[0][1]["content"]
    assert "ABOUT PAGE" in user and "Shelbyville" in user and "WEB SEARCH RESULTS" in user


def test_research_agent_works_without_search(mock_web, llm, monkeypatch):
    monkeypatch.setattr(settings, "web_search_enabled", False)
    out = asyncio.run(research_agent(task("research", url="https://dental.example"), {})).output
    assert out["search_results"] == 0 and out["profile"]["industry"] == "dental clinic"
    assert "none available" in llm.calls[0][1]["content"]


def _ctx():
    return {"t_01": {"url": "https://dental.example/", "profile": PROFILE, "evidence": ["https://dental.example/"]},
            "t_02": {"url": "https://dental.example/", "audit": AUDIT, "signals": {"https": True},
                     "evidence": ["https://dental.example/contact"]}}


def test_scoring_saves_lead_and_derives_tier_from_score(llm):
    t = AgentTask(run_id="r_lead", task_key="t_03", agent="scoring", goal="score", input={})
    with SessionLocal() as db:
        db.add(Run(id="r_lead", objective="o"))
        db.commit()
    out = asyncio.run(scoring_agent(t, _ctx())).output
    assert out["score"] == 82 and out["tier"] == "A"                    # the model said "C"; score wins
    assert out["company"] == "Smile Bright Dental" and out["pitch_angle"] and out["recommended_actions"]
    assert out["evidence"] == ["https://dental.example/", "https://dental.example/contact"]
    asyncio.run(scoring_agent(t, _ctx()))                               # retry/rerun must not duplicate
    with SessionLocal() as db:
        leads = db.scalars(select(Lead).where(Lead.run_id == "r_lead")).all()
        assert len(leads) == 1
        l = leads[0]
        assert (l.company, l.score, l.tier) == ("Smile Bright Dental", 82, "A")
        assert l.profile_json["industry"] == "dental clinic" and l.audit_json["issues"][0]["title"]
        assert any(e.type == "lead_saved" for e in db.scalars(select(Event).where(Event.run_id == "r_lead")))


def test_scoring_without_upstream_fails_clearly():
    with pytest.raises(AgentInputError, match="research and/or website"):
        asyncio.run(scoring_agent(task("scoring"), {}))


def test_missing_api_key_fails_clearly():
    from app.core.errors import PermanentError
    with pytest.raises(llm_gateway.LLMConfigError, match="GROQ_API_KEY") as e:   # key is empty in tests
        asyncio.run(REAL_PROVIDER([{"role": "user", "content": "x"}]))
    assert isinstance(e.value, PermanentError)


# ---------- full pipeline through the orchestrator ----------
PLAN = Plan(objective="Assess Smile Bright", tasks=[
    PlanTask(task_key="t_01", agent="research", input={"url": "https://dental.example"}),
    PlanTask(task_key="t_02", agent="website", input={"url": "https://dental.example"}),
    PlanTask(task_key="t_03", agent="scoring", depends_on=["t_01", "t_02"])])


def _run(url):
    plan = PLAN.model_copy(deep=True)
    for t in plan.tasks[:2]:
        t.input = {"url": url}
    with SessionLocal() as db:
        rid = create_run(db, "Assess", {}, plan).id
    asyncio.run(run_workflow(rid))
    return rid


def test_pipeline_real_agents_produce_profile_audit_score_and_lead(mock_web, llm):
    rid = _run("https://dental.example")
    with SessionLocal() as db:
        run = db.get(Run, rid)
        assert run.status == "succeeded"
        by = {t.agent: t.output_json for t in run.tasks}
        assert by["research"]["profile"] and by["website"]["audit"] and by["scoring"]["score"] == 82
        assert by["scoring"]["evidence"]
        lead = db.scalars(select(Lead).where(Lead.run_id == rid)).one()
        assert lead.score == 82 and lead.tier == "A"
        assert run.tokens == 300                                         # 3 LLM calls x 100 tokens


@pytest.mark.parametrize("url,msg", [("http://127.0.0.1/", "Blocked"), ("http://localhost:8000", "Blocked"),
                                     ("https://dead.example/", "404"), ("https://forbidden.example/", "refuses"),
                                     ("https://files.example/", "Not an HTML page")])
def test_dead_or_blocked_url_fails_the_task_cleanly_without_retries(mock_web, llm, url, msg):
    rid = _run(url)
    with SessionLocal() as db:
        run = db.get(Run, rid)
        tasks = {t.agent: t for t in run.tasks}
        assert run.status == "failed"                                    # a clean failure, not a crash
        for a in ("research", "website"):
            assert tasks[a].status == "failed" and tasks[a].attempts == 1   # permanent: no pointless retry
            assert msg in tasks[a].error and tasks[a].error.startswith("FetchRejected")
        assert tasks["scoring"].status == "skipped"
        assert db.scalars(select(Lead).where(Lead.run_id == rid)).first() is None
    assert llm.calls == []                                               # the LLM was never called


def test_transient_error_is_retried_once(mock_web, llm):
    rid = _run("https://flaky.example/")
    with SessionLocal() as db:
        t = next(t for t in db.get(Run, rid).tasks if t.agent == "website")
        assert t.status == "failed" and t.attempts == 2 and "503" in t.error


def test_missing_url_fails_clearly(mock_web, llm):
    plan = Plan(objective="x", tasks=[PlanTask(task_key="t_01", agent="website")])
    with SessionLocal() as db:
        rid = create_run(db, "x", {}, plan).id
    asyncio.run(run_workflow(rid))
    with SessionLocal() as db:
        t = db.get(Run, rid).tasks[0]
        assert t.status == "failed" and "No company URL" in t.error and t.attempts == 1
