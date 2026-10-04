"""AI Business OS dashboard.  Start with:  streamlit run dashboard/app.py
The backend starts by itself inside this process (see embedded_backend.py), so this one command is all you need."""
import streamlit as st

st.set_page_config(page_title="AI Business OS", page_icon="🤖", layout="wide")      # must be the first Streamlit call

import embedded_backend

with st.spinner("Starting the AI Business OS backend..."):
    embedded_backend.start_if_needed()       # one process: the API runs inside this app (needed on Streamlit Cloud)

import ui_kit as ui                          # imported AFTER the backend start: it reads API_URL at import time

with ui.page_guard():
    if not ui.api("GET", "/config").ok:
        st.title("🤖 AI Business OS")
        st.error("The dashboard cannot reach the backend.")
        st.markdown("Open a second terminal in the project folder and start it:\n\n```\nuvicorn app.main:app --reload\n```")
        st.button("Try again")
        st.stop()

    waiting = len(ui.pending_approvals())
    pages = [
        st.Page("views/new_run.py", title="New run", icon="➕", url_path="new-run", default=True),
        st.Page("views/live_run.py", title="Live run", icon="📡", url_path="live-run"),
        st.Page("views/lead.py", title="Lead", icon="🎯", url_path="lead"),
        st.Page("views/approvals.py", title=f"Approvals ({waiting})" if waiting else "Approvals", icon="🔔",
                url_path="approvals"),
        st.Page("views/report.py", title="Report", icon="📄", url_path="report"),
        st.Page("views/history.py", title="History", icon="🗂️", url_path="history"),
    ]
    nav = st.navigation(pages)

    with st.sidebar:
        st.divider()
        if waiting:
            st.warning(f"🔔 **{waiting}** waiting for your approval")
        st.markdown(":green-badge[🧪 Safe mode: nothing is sent]" if ui.is_dry_run()
                    else ":red-badge[⚠️ LIVE: emails are really sent]")
        if ui.current_run_id():
            st.caption(f"Current run: `{ui.current_run_id()}`")

    nav.run()
