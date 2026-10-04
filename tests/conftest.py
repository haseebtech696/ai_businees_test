import os, tempfile
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"   # must be set before app imports
os.environ["STUB_DELAY"] = "0.05"
os.environ["WEB_SEARCH_ENABLED"] = "true"                  # tests must not depend on your .env
os.environ["RETRY_BACKOFF_S"] = "0"
os.environ["GROQ_API_KEY"] = ""                            # never use a real key in tests
os.environ["EMBED_BACKEND"] = "false"                       # tests use their own in-process API, never a real server

import pathlib
import httpx
import pytest
from app.db.session import init_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "site"


def pytest_configure(config):
    config.addinivalue_line("markers", "real_agents: use the real research/website/scoring agents instead of stubs")


@pytest.fixture(autouse=True, scope="session")
def _db():
    init_db()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Tests never reach a real LLM. Tests that need one replace _call_provider with a fake."""
    from app.core import llm_gateway

    async def blocked(messages):
        raise llm_gateway.LLMError("network disabled in tests")
    monkeypatch.setattr(llm_gateway, "_call_provider", blocked)


class FakeLLM:
    """Scripted replies. Each item is a str (raw text) or a dict (dumped to JSON) or an Exception."""
    def __init__(self, monkeypatch, replies, tokens=100):
        import json
        from app.core import llm_gateway
        self.replies, self.calls, self.tokens, self._json = list(replies), [], tokens, json
        monkeypatch.setattr(llm_gateway, "_call_provider", self._call)

    async def _call(self, messages):
        self.calls.append([dict(m) for m in messages])
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return (r if isinstance(r, str) else self._json.dumps(r)), self.tokens


def plan_json(*tasks):
    """plan_json(("t_01","research",[]), ("t_02","report",["t_01"]))"""
    return {"objective": "x", "tasks": [{"task_key": k, "agent": a, "goal": f"{a} goal", "depends_on": d, "input": {}}
                                        for k, a, d in tasks]}


@pytest.fixture(autouse=True)
def _stub_info_agents(monkeypatch, request):
    """Pipeline tests use the fast stub research/website/scoring. Mark a test @pytest.mark.real_agents to opt out."""
    if request.node.get_closest_marker("real_agents"):
        return
    from app.agents import stubs
    from app.agents.base import AGENTS
    for name, fn in [("research", stubs.research_agent), ("website", stubs.website_agent),
                     ("scoring", stubs.scoring_agent)]:
        monkeypatch.setitem(AGENTS, name, fn)


def _page(name):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          text=(FIXTURES / name).read_text(encoding="utf-8"))


def _site_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "html.duckduckgo.com":
        return _page("ddg.html")
    if host == "dental.example":
        routes = {"/": "index.html", "/contact": "contact.html", "/about": "about.html", "/services": "services.html"}
        return _page(routes[path]) if path in routes else httpx.Response(404)
    if host == "redirect.example":
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    if host == "loop.example":
        return httpx.Response(302, headers={"location": "https://loop.example/"})
    if host == "forbidden.example":
        return httpx.Response(403)
    if host == "flaky.example":
        return httpx.Response(503)
    if host == "slow.example":
        raise httpx.ConnectTimeout("timed out", request=request)
    if host == "files.example":
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    return httpx.Response(404)


@pytest.fixture
def mock_web(monkeypatch):
    """Fake internet: dental.example (4 pages) + failure hosts + DuckDuckGo. No real network, DNS faked."""
    from app.tools import fetch, search

    async def fake_resolve(host):
        return ["93.184.216.34"]                       # any non-local name resolves to a public IP
    monkeypatch.setattr(fetch, "_resolve", fake_resolve)
    monkeypatch.setattr(fetch, "_transport", httpx.MockTransport(_site_handler))
    monkeypatch.setattr(search, "_transport", httpx.MockTransport(_site_handler))


def approve_all(run_id: str, decision: str = "approve") -> None:
    """Approve every pending approval of a run, then resume it (what the dashboard + API do)."""
    import asyncio
    from sqlalchemy import select
    from app.core.approvals import decide
    from app.core.orchestrator import resume_workflow
    from app.db.models import Approval
    from app.db.session import SessionLocal
    with SessionLocal() as db:
        for a_id in [a.id for a in db.scalars(select(Approval).where(Approval.run_id == run_id,
                                                                       Approval.status == "pending"))]:
            decide(db, a_id, decision)
    asyncio.run(resume_workflow(run_id))
