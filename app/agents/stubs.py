"""Stub info agents, used by the test-suite (see tests/conftest.py) so pipeline tests run fast and offline.
They return the SAME output shapes as the real research / website / scoring agents."""
import asyncio
import os

from app.schemas.contracts import AgentResult, AgentTask


async def _work():
    await asyncio.sleep(float(os.getenv("STUB_DELAY", "0.05")))


async def research_agent(task: AgentTask, context: dict) -> AgentResult:
    await _work()
    url = task.input.get("url", "https://acme-dental.example")
    return AgentResult(output={"url": url, "search_results": 0, "evidence": [url],
                               "profile": {"company_name": "Acme Dental", "industry": "dental clinic", "size_cues": "11-50 staff",
                                           "services": ["check-ups", "implants"], "recent_news": [], "summary": "Stub profile."}})


async def website_agent(task: AgentTask, context: dict) -> AgentResult:
    await _work()
    url = task.input.get("url", "https://acme-dental.example")
    issues = [{"title": "No online booking", "severity": "high", "detail": "Only phone contact."},
              {"title": "Images lack alt text", "severity": "low", "detail": "Most images have no alt text."},
              {"title": "No meta description", "severity": "low", "detail": "Search results show no summary."}]
    return AgentResult(output={"url": url, "evidence": [url], "signals": {"https": True},
                               "audit": {"summary": "Stub audit.", "issues": issues, "opportunities": ["Add online booking"],
                                         "contact": {"emails": ["info@acme-dental.example"], "phones": [], "contact_page": None,
                                                     "notes": ""}}})


async def scoring_agent(task: AgentTask, context: dict) -> AgentResult:
    await _work()
    prof = next((v["profile"] for v in context.values() if isinstance(v, dict) and "profile" in v), {})
    site = next((v for v in context.values() if isinstance(v, dict) and "audit" in v), {})
    company, url = prof.get("company_name", "Acme Dental"), site.get("url", "https://acme-dental.example")
    lead = {"company": company, "website": url, "score": 80, "tier": "A", "reasons": ["No online booking"],
            "recommended_actions": ["Email the owner"], "pitch_angle": "Your website could turn more visitors into enquiries.",
            "evidence": [url]}
    return AgentResult(output={**lead, "leads": [{k: lead[k] for k in ("company", "website", "score", "tier")}]})
