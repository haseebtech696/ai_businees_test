import asyncio
import pytest

from app.core.errors import PermanentError
from app.tools import fetch
from app.tools.fetch import FetchError, FetchRejected, fetch_url


def _get(url):
    return asyncio.run(fetch_url(url))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/", "http://192.168.0.1/", "http://0.0.0.0/", "http://100.64.0.1/",
    "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://0x7f000001/", "http://224.0.0.1/",
    "http://app.localhost/", "http://printer.local/", "ftp://example.com/", "file:///etc/passwd",
])
def test_ssrf_guard_blocks_local_and_private_targets(url):
    with pytest.raises(FetchRejected) as e:        # real DNS code path; no request is ever sent
        _get(url)
    assert isinstance(e.value, PermanentError)


def test_blocked_message_is_clear():
    with pytest.raises(FetchRejected, match="Blocked"):
        _get("http://169.254.169.254/")


def test_redirect_to_private_address_is_blocked(mock_web):
    with pytest.raises(FetchRejected, match="Blocked"):
        _get("https://redirect.example/")


def test_redirect_loop_is_stopped(mock_web):
    with pytest.raises(FetchRejected, match="Too many redirects"):
        _get("https://loop.example/")


def test_success(mock_web):
    r = _get("dental.example")                       # scheme added automatically
    assert r.url == "https://dental.example/" and r.status == 200 and "Smile Bright" in r.text
    assert r.size > 100 and r.elapsed_s >= 0


@pytest.mark.parametrize("host,msg", [("dead.example", "404"), ("forbidden.example", "refuses automated access"),
                                      ("files.example", "Not an HTML page")])
def test_permanent_failures_are_clear(mock_web, host, msg):
    with pytest.raises(FetchRejected, match=msg):
        _get(f"https://{host}/")


def test_dns_failure_is_clear_and_permanent(monkeypatch):
    import socket

    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(fetch.socket, "getaddrinfo", boom)
    with pytest.raises(FetchRejected, match="DNS lookup failed"):
        _get("https://no-such-company.example/")


@pytest.mark.parametrize("host,msg", [("slow.example", "Timed out|Could not connect"), ("flaky.example", "HTTP 503")])
def test_transient_failures_are_retryable(mock_web, host, msg):
    with pytest.raises(FetchError, match=msg) as e:
        _get(f"https://{host}/")
    assert not isinstance(e.value, PermanentError)    # the orchestrator may retry these


def test_empty_url_rejected():
    with pytest.raises(FetchRejected):
        _get("   ")
