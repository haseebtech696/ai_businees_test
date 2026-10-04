"""web_search via DuckDuckGo's HTML endpoint. Unreliable by nature (rate limits, captchas), so it is OPTIONAL:
it never raises and returns [] on any problem. Disable entirely with WEB_SEARCH_ENABLED=false."""
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.core.config import settings

log = logging.getLogger(__name__)
DDG_URL = "https://html.duckduckgo.com/html/"
_transport: httpx.AsyncBaseTransport | None = None   # tests inject httpx.MockTransport


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


def _real_url(href: str) -> str | None:
    if "uddg=" in href:
        vals = parse_qs(urlsplit(href).query).get("uddg")
        href = vals[0] if vals else ""
    elif href.startswith("//"):
        href = "https:" + href
    return href if href.startswith(("http://", "https://")) else None


def parse_ddg(html: str, max_results: int = 5) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for block in soup.select("div.result"):
        if "result--ad" in (block.get("class") or []):
            continue
        a = block.select_one("a.result__a")
        url = _real_url(a["href"]) if a and a.get("href") else None
        if not url:
            continue
        snip = block.select_one(".result__snippet")
        out.append(SearchResult(title=a.get_text(" ", strip=True)[:200], url=url,
                                snippet=snip.get_text(" ", strip=True)[:400] if snip else ""))
        if len(out) >= max_results:
            break
    return out


async def web_search(query: str, max_results: int = 5) -> list[SearchResult]:
    if not settings.web_search_enabled:
        return []
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=settings.fetch_timeout_s,
                                     headers={"User-Agent": settings.user_agent}) as client:
            resp = await client.post(DDG_URL, data={"q": query})
        if resp.status_code != 200:
            log.warning("web_search: HTTP %s for %r", resp.status_code, query)
            return []
        return parse_ddg(resp.text, max_results)
    except Exception as e:  # noqa: BLE001  optional tool: never break the agent
        log.warning("web_search failed for %r: %s", query, e)
        return []
