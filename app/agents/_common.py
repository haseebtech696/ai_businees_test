"""Helpers shared by the information agents."""
import re
from functools import lru_cache
from pathlib import Path

from app.core.errors import PermanentError
from app.schemas.contracts import AgentTask
from app.tools.scrape import PageData
from app.tools.untrusted import wrap_untrusted

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)


class AgentInputError(PermanentError):
    """The task is missing something it needs. Retrying cannot fix that."""


@lru_cache
def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def target_url(task: AgentTask) -> str:
    """The company URL: input.url / input.website / input.domain, else an explicit http(s) URL in the goal."""
    for key in ("url", "website", "domain"):
        v = task.input.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for text in [task.goal, *[v for v in task.input.values() if isinstance(v, str)]]:
        m = URL_RE.search(text or "")
        if m:
            return m.group(0).rstrip(".,;")
    raise AgentInputError("No company URL was provided. Add a website URL to the run input, "
                          'e.g. {"url": "https://example.com"}')


def find_output(context: dict, key: str) -> dict | None:
    """The upstream task output that contains `key` (plans are dynamic, so match by shape, not task key)."""
    for v in context.values():
        if isinstance(v, dict) and key in v:
            return v
    return None


def page_brief(page: PageData, text_chars: int | None = None) -> str:
    """One scraped page as untrusted text: structured facts + visible text."""
    text = page.text if text_chars is None else page.text[:text_chars]
    nav = " | ".join(f"{l.text or '(no text)'}" for l in page.nav_links[:20])
    lines = [
        f"URL: {page.url}",
        f"Title: {page.title or '(none)'}",
        f"Meta description: {page.meta_description or '(none)'}",
        f"Headings: {'; '.join(page.headings) or '(none)'}",
        f"Navigation: {nav or '(none)'}",
        f"Call-to-action keywords found: {', '.join(page.cta_keywords) or '(none)'}",
        f"Signals: https={page.https}, mobile viewport tag={page.viewport_meta}, online booking system found={page.has_booking}, appointment request button found={page.has_appointment_cta}, "
        f"contact/enquiry form={page.has_form}, crawler response time={page.fetch_seconds}s (measured from the machine running this crawler, includes network distance; NOT a page-speed score), html size={page.html_bytes // 1024} KB, "
        f"images without alt text={page.images_missing_alt}/{page.images_total}",
        f"Clickable phone links (tel:) on page={page.tel_links}; JSON-LD structured data types={', '.join(page.structured_data_types) or 'none found'}",
        f"Emails on page: {', '.join(page.emails) or '(none)'}",
        f"Phones on page: {', '.join(page.phones) or '(none)'}",
        "Visible text:",
        text,
    ]
    return wrap_untrusted("\n".join(lines), source=page.url)


def host_of(url: str) -> str:
    from urllib.parse import urlsplit
    h = (urlsplit(url).hostname or url).lower()
    return h[4:] if h.startswith("www.") else h


def feedback_block(task: AgentTask) -> str:
    """Appended to an agent's prompt when verification rejected its previous answer and asked for one retry."""
    fb = task.input.get("verification_feedback")
    if not fb:
        return ""
    return ("\n\n## Verification feedback: your previous answer was rejected\n" + str(fb) +
            "\nFix these problems. Stay within the evidence and never invent facts just to pass a check.")
