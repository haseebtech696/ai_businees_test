"""fetch_url: the only way agents touch the web.
 - http/https only, timeout, user-agent, body size cap
 - SSRF guard: every hostname is resolved and rejected if ANY address is private/loopback/link-local/
   reserved/multicast. Redirects are followed manually and each hop is re-checked.
 - Errors are explicit: FetchRejected = permanent (don't retry), FetchError = transient (retry may help).
Known limit: DNS is resolved once for the check and again by httpx (DNS-rebinding window). Fine for an MVP."""
import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import settings
from app.core.errors import PermanentError

MAX_REDIRECTS = 5
HTML_TYPES = ("text/html", "application/xhtml", "text/plain")
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
REDIRECT_CODES = {301, 302, 303, 307, 308}

_transport: httpx.AsyncBaseTransport | None = None   # tests inject httpx.MockTransport


class FetchError(Exception):
    """Transient problem (timeout, 5xx, connection reset). A retry may succeed."""


class FetchRejected(FetchError, PermanentError):
    """Permanent problem (blocked address, 404, 403, not HTML, bad URL). Retrying is pointless."""


@dataclass
class FetchResult:
    url: str            # final URL after redirects
    status: int
    text: str
    content_type: str
    elapsed_s: float
    size: int           # bytes received


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise FetchRejected("Empty URL")
    if "://" not in url:
        url = "https://" + url
    url, _ = urldefrag(url)
    if len(url) > 2048:
        raise FetchRejected("URL is too long")
    try:
        parts = urlsplit(url)
        if parts.netloc and not parts.path:
            url = urlunsplit(parts._replace(path="/"))      # https://example.com -> https://example.com/
    except ValueError:
        pass                                                # rejected later by check_url_allowed
    return url


def _ip_blocked(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (not ip.is_global) or ip.is_multicast or ip.is_unspecified


async def _resolve(host: str) -> list[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise FetchRejected(f"DNS lookup failed for '{host}': the domain does not exist or cannot be reached") from e
    return sorted({i[4][0] for i in infos})


async def check_url_allowed(url: str) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise FetchRejected(f"Invalid URL: {url[:100]}") from None
    if parts.scheme not in ("http", "https"):
        raise FetchRejected(f"Blocked: only http and https URLs are allowed (got '{parts.scheme or 'none'}')")
    if not host:
        raise FetchRejected("Invalid URL: no host name")
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        raise FetchRejected(f"Blocked: '{host}' is a local address")
    try:
        ips = [str(ipaddress.ip_address(host))]      # literal IP
    except ValueError:
        ips = await _resolve(host)                   # hostnames and odd forms like 0x7f000001 / 2130706433
    for ip in ips:
        if _ip_blocked(ip):
            raise FetchRejected(f"Blocked: '{host}' points to a private or local network address ({ip})")


def _decode(body: bytes, resp: httpx.Response) -> str:
    enc = resp.charset_encoding or "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch_url(url: str) -> FetchResult:
    url = normalize_url(url)
    start = time.monotonic()
    headers = {"User-Agent": settings.user_agent, "Accept-Language": "en",
               "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
    async with httpx.AsyncClient(transport=_transport, follow_redirects=False,
                                 timeout=settings.fetch_timeout_s, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await check_url_allowed(url)             # checked again on every redirect hop
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code in REDIRECT_CODES and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    _raise_for_status(resp.status_code, url)
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not ctype.startswith(HTML_TYPES):
                        raise FetchRejected(f"Not an HTML page (content-type: {ctype.split(';')[0]}): {url}")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) >= settings.fetch_max_bytes:
                            break                    # keep what we have; huge pages are truncated
                    return FetchResult(url=str(resp.url), status=resp.status_code, text=_decode(bytes(body), resp),
                                       content_type=ctype, elapsed_s=round(time.monotonic() - start, 2),
                                       size=len(body))
            except httpx.TimeoutException:
                raise FetchError(f"Timed out after {settings.fetch_timeout_s:.0f}s fetching {url}") from None
            except httpx.HTTPError as e:
                raise FetchError(f"Could not connect to {url} ({type(e).__name__})") from None
        raise FetchRejected(f"Too many redirects (more than {MAX_REDIRECTS}) starting from {url}")


def _raise_for_status(code: int, url: str) -> None:
    if 200 <= code < 300:
        return
    if code in (404, 410):
        raise FetchRejected(f"HTTP {code}: page not found: {url}")
    if code in (401, 403, 451):
        raise FetchRejected(f"HTTP {code}: the site refuses automated access: {url}")
    if code == 429 or code >= 500:
        raise FetchError(f"HTTP {code}: the site is temporarily unavailable or rate limiting: {url}")
    raise FetchRejected(f"HTTP {code} for {url}")
