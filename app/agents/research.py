"""Research agent: homepage + About page + web search results, one LLM call -> CompanyProfile."""
import asyncio
import logging

from app.agents._common import host_of, load_prompt, page_brief, target_url, feedback_block
from app.core.config import settings
from app.core.llm_gateway import complete_json
from app.schemas.contracts import AgentResult, AgentTask
from app.schemas.info import CompanyProfile
from app.tools.scrape import PageData, pick_links, scrape_page
from app.tools.search import web_search
from app.tools.untrusted import wrap_untrusted

log = logging.getLogger(__name__)
ABOUT_PATTERNS = [r"about", r"our-story|who-we-are|company", r"team|meet", r"über|ueber"]
MAX_RESULTS = 8


async def research_agent(task: AgentTask, context: dict) -> AgentResult:
    home = await scrape_page(target_url(task), settings.homepage_text_chars)   # dead/blocked URL fails the task here

    name = task.input.get("company") or host_of(home.url)
    about_urls = pick_links(home, ABOUT_PATTERNS, 1)
    about_task = scrape_page(about_urls[0], settings.subpage_text_chars) if about_urls else asyncio.sleep(0, None)
    about, found1, found2 = await asyncio.gather(
        about_task, web_search(name), web_search(f"{name} news"), return_exceptions=True)
    about = about if isinstance(about, PageData) else None
    results, seen = [], set()
    for batch in (found1, found2):
        for r in (batch if isinstance(batch, list) else []):
            if r.url not in seen:
                seen.add(r.url)
                results.append(r)
    results = results[:MAX_RESULTS]

    parts = [f"Task goal: {task.goal or 'profile this company'}\nCompany name hint: {name}\n",
             "HOMEPAGE:\n" + page_brief(home)]
    if about:
        parts.append("ABOUT PAGE:\n" + page_brief(about))
    if results:
        listing = "\n".join(f"{i}. {r.title}\n   {r.url}\n   {r.snippet}" for i, r in enumerate(results, 1))
        parts.append("WEB SEARCH RESULTS:\n" + wrap_untrusted(listing, source="web search"))
    else:
        parts.append("WEB SEARCH RESULTS: none available. Use the website only.")
    profile = await complete_json(load_prompt("company_research"), "\n\n".join(parts) + feedback_block(task), CompanyProfile,
                                  run_id=task.run_id)

    evidence = [home.url] + ([about.url] if about else []) + [r.url for r in results]
    return AgentResult(output={"url": home.url, "profile": profile.model_dump(), "search_results": len(results),
                               "evidence": evidence})
