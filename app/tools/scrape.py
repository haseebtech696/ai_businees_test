"""scrape_page: fetch + parse one page into the facts the agents need."""
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.tools.fetch import fetch_url

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{7,18}\d")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif")
CTA_KEYWORDS = ["book now", "book online", "book an appointment", "book appointment", "book a", "schedule",
                "appointment", "request a quote", "get a quote", "free consultation", "free quote", "contact us",
                "call us", "call now", "get started", "sign up", "buy now", "order online", "reserve",
                "reservation", "enquire", "inquire", "request a call", "make an appointment"]
APPOINTMENT_WORDS = ("book", "appointment", "schedule", "reserve", "reservation")
BOOKING_HOSTS = ("calendly.com", "acuityscheduling.com", "setmore.com", "zocdoc.com", "opentable.com",
                 "simplybook.me", "squareup.com/appointments", "booksy.com", "fresha.com",
                 "hsone.app", "hotdoc.com.au", "healthengine.com.au", "nookal.com", "cliniko.com",
                 "jane.app", "mindbodyonline.com", "vagaro.com", "youcanbook.me", "tidycal.com",
                 "appointy.com", "timely.is", "zenoti.com", "localmed.com", "opencare.com")
BOOK_TEXT_RE = re.compile(r"\bbook(?:ing)?\b")
SKIP_TAGS = ["script", "style", "noscript", "svg", "template", "iframe"]
FILE_EXT = IMG_EXT + (".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".mp4", ".mp3")


class Link(BaseModel):
    text: str
    href: str


class PageData(BaseModel):
    url: str
    title: str = ""
    meta_description: str = ""
    headings: list[str] = []
    nav_links: list[Link] = []
    internal_links: list[Link] = []
    text: str = ""
    emails: list[str] = []
    phones: list[str] = []
    cta_keywords: list[str] = []
    has_booking: bool = False            # a real online-booking link/platform
    has_appointment_cta: bool = False    # "make an appointment" style button (may only open a contact form)
    tel_links: int = 0                   # number of clickable tel: links
    structured_data_types: list[str] = []  # @type values found in JSON-LD (e.g. Dentist, LocalBusiness)
    has_form: bool = False
    viewport_meta: bool = False
    https: bool = False
    images_total: int = 0
    images_missing_alt: int = 0
    fetch_seconds: float = 0.0
    html_bytes: int = 0


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + " …"


def _uniq(items, limit):
    seen, out = set(), []
    for i in items:
        if i and i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out[:limit]


def _phones(text: str, tel_hrefs: list[str]) -> list[str]:
    found = [_clean(h) for h in tel_hrefs]
    pos = 0
    while pos < len(text):
        m = PHONE_RE.search(text, pos)
        if not m:
            break
        start, end = m.start(), m.end()
        cut = re.search(r"(?<=\d)\s+(?=\()", text[start:end])      # "0418 190 554 (07) 3844 ..." = two numbers
        nxt = start + cut.end() if cut else end
        if cut:
            end = start + cut.start()
        pos = max(nxt, start + 1)
        cand = text[start:end]
        digits = re.sub(r"\D", "", cand)
        if not 9 <= len(digits) <= 15:
            continue
        if (re.search(r"\d{4}-\d{2}-\d{2}", cand) or re.search(r"\b(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}\b", cand)
                or re.search(r"\.\s", cand)):
            continue                                   # dates, year ranges, numbers spanning a sentence end
        if not re.search(r"[+()\s.-]", cand.strip()):
            continue                                   # bare digit runs (ids)
        found.append(_clean(cand))
    return _clean_phone_list(_uniq(found, 10))


def _clean_phone_list(found: list[str]) -> list[str]:
    """Drop duplicates written in another format, and numbers with a postcode glued on the front
    (e.g. 'VIC 3161 03 9533 0996' scraped as '3161 03 9533 0996')."""
    digs = [re.sub(r"\D", "", f) for f in found]
    keep, seen = [], set()
    for i, (f, d) in enumerate(zip(found, digs)):
        glued = (not f.lstrip().startswith("+")) and any(
            j != i and len(digs[j]) >= 8 and len(d) - len(digs[j]) in (4, 5) and d.endswith(digs[j])
            for j in range(len(found)))
        if glued:
            continue
        if d in seen:
            k = next(n for n, x in enumerate(keep) if re.sub(r"\D", "", x) == d)
            if re.fullmatch(r"\+?\d+", keep[k]) and not re.fullmatch(r"\+?\d+", f):
                keep[k] = f                            # '0738448606' -> '(07) 3844 8606'
            continue
        seen.add(d)
        keep.append(f)
    return keep


def _ld_types(soup) -> list[str]:
    types: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            t = o.get("@type")
            types.extend([t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for sc in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            walk(json.loads(sc.string or sc.get_text() or ""))
        except ValueError:
            continue
    return _uniq(types, 12)


def parse_html(html: str, url: str, max_chars: int = 8000, fetch_seconds: float = 0.0, html_bytes: int = 0) -> PageData:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_desc = _clean(md.get("content", "")) if md else ""
    viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}) is not None

    anchors = soup.find_all("a", href=True)
    mailtos = [a["href"][7:].split("?")[0] for a in anchors if a["href"].lower().startswith("mailto:")]
    tels = [a["href"][4:] for a in anchors if a["href"].lower().startswith("tel:")]

    # CTA / booking signals come from clickable things only (links, buttons), not prose
    me = _host(url)
    clickable = [(_clean(a.get_text()) + " " + a["href"]).lower() for a in anchors]
    book_links = []                      # (text, absolute href) of anchors, for the online-booking test
    for a in anchors:
        book_links.append((_clean(a.get_text()).lower(), urljoin(url, a["href"]).lower()))
    for b in soup.find_all(["button", "input"]):
        if b.name == "input" and b.get("type") not in ("submit", "button"):
            continue
        clickable.append(_clean(b.get_text() or b.get("value", "")).lower())
    ctas = _uniq([kw for kw in CTA_KEYWORDS if any(kw in c for c in clickable)], 12)
    has_appointment_cta = any(any(w in kw for w in APPOINTMENT_WORDS) for kw in ctas)
    # Online booking = a link to a booking platform, or a "book ..." link that leaves the site or
    # points at a /book path. A "make an appointment" button that only opens the contact page is NOT online booking.
    has_booking = any(
        any(h in href for h in BOOKING_HOSTS)
        or (BOOK_TEXT_RE.search(text) and (_host(href) != me or "book" in urlsplit(href).path))
        for text, href in book_links)

    ld_types = _ld_types(soup)                       # before <script> tags are removed below
    forms = soup.find_all("form")
    has_form = any(f.find("textarea") or f.find("input", attrs={"type": re.compile("^(email|tel)$", re.I)})
                   for f in forms)

    headings = [_clean(h.get_text())[:120] for h in soup.find_all(["h1", "h2", "h3"])]
    headings = _uniq([h for h in headings if h], 20)

    def collect(scope_tags) -> list[Link]:
        out = []
        for scope in scope_tags:
            for a in scope.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                out.append(Link(text=_clean(a.get_text())[:60], href=urljoin(url, href)))
        return out

    navs = soup.find_all("nav") or soup.find_all("header")
    nav_links = collect(navs)
    seen, nav_uniq = set(), []
    for l in nav_links:
        if l.href not in seen:
            seen.add(l.href)
            nav_uniq.append(l)

    seen, internal = set(), []
    for l in collect([soup]):
        p = urlsplit(l.href)
        if p.scheme in ("http", "https") and _host(l.href) == me and not p.path.lower().endswith(FILE_EXT):
            key = l.href.split("#")[0]
            if key not in seen:
                seen.add(key)
                internal.append(Link(text=l.text, href=key))

    imgs = soup.find_all("img")
    missing = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    for t in soup(SKIP_TAGS):
        t.decompose()
    text = _clean(soup.get_text(" "))
    emails = [e for e in EMAIL_RE.findall(text) + mailtos if not e.lower().endswith(IMG_EXT)]

    return PageData(
        url=url, title=title[:200], meta_description=meta_desc[:400], headings=headings,
        nav_links=nav_uniq[:30], internal_links=internal[:200], text=_trim(text, max_chars),
        emails=_uniq(emails, 10), phones=_phones(text, tels), cta_keywords=ctas, has_booking=has_booking, has_appointment_cta=has_appointment_cta,
        tel_links=len(tels), structured_data_types=ld_types,
        has_form=has_form, viewport_meta=viewport, https=url.lower().startswith("https://"),
        images_total=len(imgs), images_missing_alt=missing, fetch_seconds=fetch_seconds, html_bytes=html_bytes)


async def scrape_page(url: str, max_chars: int = 8000) -> PageData:
    """max_chars=8000 is roughly 2k tokens of page text."""
    res = await fetch_url(url)
    return parse_html(res.text, res.url, max_chars, res.elapsed_s, res.size)


def pick_links(page: PageData, patterns: list[str], limit: int = 1, exclude: list[str] | None = None) -> list[str]:
    """Pick internal links whose path or text matches one of the regex patterns (earlier pattern = better)."""
    skip = {u.rstrip("/") for u in (exclude or [])} | {page.url.rstrip("/")}
    scored = []
    for l in page.internal_links:
        if l.href.rstrip("/") in skip:
            continue
        hay_path = urlsplit(l.href).path.lower()
        for rank, pat in enumerate(patterns):
            if re.search(pat, hay_path) or re.search(pat, l.text.lower()):
                scored.append((rank, len(hay_path), l.href))
                break
    out = []
    for _, _, href in sorted(scored):
        if href.rstrip("/") not in {o.rstrip("/") for o in out}:
            out.append(href)
    return out[:limit]