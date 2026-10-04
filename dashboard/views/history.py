"""History: every past run."""
from urllib.parse import urlsplit

import streamlit as st

import ui_kit as ui

with ui.page_guard():
    st.title("🗂️ History")
    runs = ui.list_runs()
    if not runs:
        st.info("No runs yet. Your finished runs will be listed here.")
        ui.link("views/new_run.py", "Start your first run", "➕")
        st.stop()

    options = ["All", "Done", "Failed", "Needs your approval", "In progress"]
    pick = st.selectbox("Show", options)
    keep = {"All": None, "Done": ("succeeded",), "Failed": ("failed",), "Needs your approval": ("awaiting_approval",),
            "In progress": ("pending", "planning", "running")}[pick]
    shown = [r for r in runs if keep is None or r["status"] in keep]

    def company(r: dict) -> str:
        i = r.get("input_json") or {}
        return i.get("company") or urlsplit(ui.normalize_url(i.get("url", ""))).hostname or ""

    st.dataframe([{"Started (UTC)": r["created_at"][:16].replace("T", " "), "Run": r["id"], "Company": company(r),
                   "Objective": r["objective"][:70], "Status": f"{ui.STATUS.get(r['status'], ('•',))[0]} {ui.STATUS.get(r['status'], ('', r['status']))[1]}",
                   "Duration": ui.fmt_seconds(ui.elapsed_seconds(r)) if r.get("finished_at") else "running",
                   "AI calls": r.get("llm_calls", 0), "Tokens": r["tokens"]} for r in shown],
                 hide_index=True, width="stretch")
    st.caption(f"{len(shown)} of {len(runs)} runs")

    if shown:
        st.subheader("Open a run")
        chosen = st.selectbox("Run", [r["id"] for r in shown], key="hist_pick",
                              format_func=lambda i: ui.run_title(next(r for r in shown if r["id"] == i)))
        a, b, c = st.columns(3)
        if a.button("📡 Live view"):
            ui.set_run(chosen)
            ui.go("views/live_run.py")
        if b.button("🎯 Lead"):
            ui.set_run(chosen)
            ui.go("views/lead.py")
        if c.button("📄 Report"):
            ui.set_run(chosen)
            ui.go("views/report.py")
