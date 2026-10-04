"""Website agent: scrape homepage + contact page + 1-2 key pages, one LLM call -> SiteAudit."""
import asyncio
import logging

from app.agents._common import load_prompt, page_brief, target_url, feedback_block
from app.core.config import settings
from app.core.llm_gateway import complete_json
from app.schemas.contracts import AgentResult, AgentTask
from app.schemas.info import SiteAudit
from app.tools.scrape import PageData, pick_links, scrape_page

log = logging.getLogger(__name__)
CONTACT_PATTERNS = [r"contact", r"kontakt", r"get-in-touch", r"reach-us", r"enquir"]
KEY_PATTERNS = [r"book|appointment|reserv", r"service|treatment|solution|what-we-do|offer", r"pricing|price|plans|rates",
                r"product|menu|shop", r"team|doctors|staff"]


def signals(p: PageData, pages: list[PageData] | None = None) -> dict:
    pages = pages or [p]
    return {"https": p.https, "mobile_viewport_tag": p.viewport_meta, "online_booking": p.has_booking, "appointment_cta": p.has_appointment_cta,
            "contact_form": p.has_form, "crawler_response_s": p.fetch_seconds, "html_kb": p.html_bytes // 1024,
            "images_missing_alt": p.images_missing_alt, "images_total": p.images_total,
            "cta_keywords": p.cta_keywords,
            "clickable_phone_links": sum(x.tel_links for x in pages),
            "structured_data_types": list(dict.fromkeys(t for x in pages for t in x.structured_data_types))}


async def website_agent(task: AgentTask, context: dict) -> AgentResult:
    home = await scrape_page(target_url(task), settings.homepage_text_chars)   # dead/blocked URL fails the task here

    contact = pick_links(home, CONTACT_PATTERNS, 1)
    key = pick_links(home, KEY_PATTERNS, 2, exclude=contact)
    extra = await asyncio.gather(*(scrape_page(u, settings.subpage_text_chars) for u in contact + key),
                                 return_exceptions=True)
    pages = [home]
    for url, res in zip(contact + key, extra):
        if isinstance(res, PageData):
            pages.append(res)
        else:
            log.info("website agent: skipped %s (%s)", url, res)

    user = (f"Task goal: {task.goal or 'audit this website'}\n\n"
            f"Pages crawled ({len(pages)}): homepage first, then contact and key pages.\n\n"
            + "\n\n".join(page_brief(p) for p in pages) + feedback_block(task))
    audit = await complete_json(load_prompt("website_audit"), user, SiteAudit, run_id=task.run_id)

    # Contact details come from the pages, never from the model.
    audit.contact.emails = list(dict.fromkeys(e for p in pages for e in p.emails))[:5]
    audit.contact.phones = list(dict.fromkeys(ph for p in pages for ph in p.phones))[:5]
    if not audit.contact.contact_page and contact and contact[0] in {p.url for p in pages}:
        audit.contact.contact_page = contact[0]

    return AgentResult(output={"url": home.url, "audit": audit.model_dump(), "signals": signals(home, pages),
                               "evidence": [p.url for p in pages]})
