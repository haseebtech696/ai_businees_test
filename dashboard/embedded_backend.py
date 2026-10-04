"""Single-process deployment: run the FastAPI backend inside the Streamlit process.

Streamlit Community Cloud starts ONE script (dashboard/app.py). The dashboard talks to the API over HTTP, so this
module starts the API on a free local port in a background thread the first time the dashboard loads, and points
the dashboard at it (API_URL). Nothing else is needed: no Render, no second terminal.

  - Secrets from Streamlit Cloud (App settings > Secrets) are copied into the environment first, so the backend's
    normal settings (GROQ_API_KEY, DRY_RUN, ...) work unchanged. Locally, .env is used as before.
  - EMBED_BACKEND=false turns this off (the test-suite does; so does anyone who runs uvicorn separately).
  - If API_URL points at a backend that already answers, that one is used and nothing is started.
Call start_if_needed() BEFORE importing ui_kit, because ui_kit reads API_URL when it is imported."""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_thread: threading.Thread | None = None
_port: int | None = None


def _export_secrets() -> None:
    """Streamlit Cloud secrets -> environment variables (only simple values, never overriding a real env var)."""
    try:
        import streamlit as st
        for key, value in st.secrets.items():
            if isinstance(value, (str, int, float, bool)) and key not in os.environ:
                os.environ[key] = str(value).lower() if isinstance(value, bool) else str(value)
    except Exception:  # noqa: BLE001  no secrets file (local run): .env is used instead
        pass


def _healthy(base: str, timeout: float = 1.0) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_if_needed() -> str | None:
    """Make sure a backend is answering and API_URL points at it. Returns the base URL (None when disabled)."""
    global _thread, _port
    if os.getenv("EMBED_BACKEND", "true").lower() == "false":
        return None
    with _lock:
        if _thread is not None and _thread.is_alive() and _port:
            return f"http://127.0.0.1:{_port}"                      # fast path: every Streamlit rerun comes here

        _export_secrets()
        explicit = os.getenv("API_URL")
        if explicit and _healthy(explicit):
            return explicit                                         # someone runs uvicorn separately: use it

        os.chdir(ROOT)                                              # data/ and business_profile.yaml are relative to it
        if sys.path[0] != str(ROOT):
            sys.path.insert(0, str(ROOT))                           # so `app` is the package, not dashboard/app.py
        import uvicorn
        from app.main import app as api

        _port = _port or _free_port()
        server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=_port, log_level="warning"))
        server.install_signal_handlers = lambda: None               # signals only work on the main thread
        _thread = threading.Thread(target=server.run, name="embedded-api", daemon=True)
        _thread.start()

        base = f"http://127.0.0.1:{_port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _healthy(base):
                os.environ["API_URL"] = base
                return base
            if not _thread.is_alive():
                break
            time.sleep(0.2)
        raise RuntimeError("The embedded backend did not start. Check the app logs (Manage app > Logs).")
