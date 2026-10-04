"""Live run: watch the agents work. Refreshes itself every 2 seconds while the run can still change."""
import streamlit as st

import ui_kit as ui


def dag_dot(tasks: list[dict]) -> str:
    colors = {"pending": "lightgrey", "running": "gold", "succeeded": "palegreen", "failed": "salmon",
              "skipped": "grey", "awaiting_approval": "orange", "approved": "lightblue"}
    out = ["digraph G { rankdir=LR; node [shape=box, style=filled, fontname=Helvetica];"]
    for t in tasks:
        out.append(f'"{t["task_key"]}" [label="{ui.step_label(t["agent"])}\\n{ui.STATUS.get(t["status"], ("", t["status"]))[1]}", '
                   f'fillcolor={colors.get(t["status"], "white")}];')
        out += [f'"{d}" -> "{t["task_key"]}";' for d in t["depends_on"]]
    return "\n".join(out + ["}"])


def body(run_id: str) -> None:
    with ui.page_guard():
        res = ui.get_run(run_id)
        if not res.ok:
            st.error(res.error)
            return
        run, events = res.data, ui.get_events(run_id)
        pending = ui.pending_approvals(run_id)
        seen = (run_id, run["status"], len(ui.pending_approvals()))
        if st.session_state.get("_live_seen") not in (None, seen) and st.session_state["_live_seen"][0] == run_id:
            st.session_state["_live_seen"] = seen
            st.rerun(scope="app")                       # status or approval count changed: refresh sidebar + refresh mode
        st.session_state["_live_seen"] = seen

        tasks = sorted(run["tasks"], key=lambda t: t["task_key"])
        labels = {t["task_key"]: ui.step_label(t["agent"]) for t in tasks}
        done = sum(t["status"] in ("succeeded", "skipped", "failed") for t in tasks)
        inp = run.get("input_json") or {}

        st.subheader(run["objective"])
        st.caption(" | ".join(x for x in [inp.get("company"), inp.get("url"), f"run {run['id']}"] if x))
        m1, m2, m3, m4 = st.columns(4)
        m1.markdown("**Status**")
        m1.markdown(ui.badge(run["status"]))
        m2.metric("Steps done", f"{done} of {len(tasks)}" if tasks else "planning")
        m3.metric("Time", ui.fmt_seconds(ui.elapsed_seconds(run)))
        m4.metric("AI calls", run.get("llm_calls", 0), help=f"{run['tokens']} tokens used")
        st.progress(done / len(tasks) if tasks else 0.0,
                    text=("The manager is planning the steps..." if not tasks else f"{done} of {len(tasks)} steps complete"))

        if run["status"] == "awaiting_approval":
            st.warning("⏸️ **The run is paused and waiting for you.** Nothing is sent until you decide.")
            for a in pending:
                p = a["payload_json"]
                if a["action_type"] == "send_email":
                    with st.expander(f"Email waiting for approval: {p.get('subject', '')}", expanded=True):
                        st.markdown(f"**To:** {p.get('to') or '(no address found)'}")
                        st.text(p.get("body", ""))
            ui.link("views/approvals.py", "Review and approve", "🔔")
        elif run["status"] == "succeeded":
            st.success("🏁 The run finished successfully.")
            ui.link("views/lead.py", "See the lead", "🎯")
            ui.link("views/report.py", "Read the final report", "📄")
        elif run["status"] == "failed":
            st.error("The run finished with problems. The report lists exactly what went wrong.")
            ui.link("views/report.py", "Read the final report", "📄")

        left, right = st.columns([1, 1])
        with left:
            st.markdown("#### Plan")
            dur = ui.task_durations(events)
            if not tasks:
                st.info("🧠 The manager is deciding which steps are needed...")
            for t in tasks:
                icon = ui.STATUS.get(t["status"], ("•",))[0]
                took = f" · {ui.fmt_seconds(dur[t['task_key']])}" if t["task_key"] in dur else ""
                extra = (" · **waiting for you**" if t["status"] == "awaiting_approval" else "")
                st.markdown(f"{icon} **{ui.step_label(t['agent'])}**{took}{extra}  \n<small>{t['goal']}</small>",
                            unsafe_allow_html=True)
                if t["status"] in ("failed", "skipped") and t.get("error"):
                    st.caption("   ↳ " + ui.friendly_error(t["error"]))
            if tasks:
                with st.expander("Show the plan as a diagram"):
                    st.graphviz_chart(dag_dot(tasks))
        with right:
            st.markdown("#### Timeline")
            box = st.container(height=420)
            t0 = ui.parse_ts(events[0]["ts"]) if events else None
            with box:
                if not events:
                    st.caption("Nothing has happened yet.")
                for e in events:
                    icon, text = ui.describe_event(e, labels)
                    ts = ui.parse_ts(e["ts"])
                    st.markdown(f"`+{(ts - t0).total_seconds():5.1f}s` {icon} {text}" if ts and t0 else f"{icon} {text}")

        st.markdown("#### Details for each step")
        st.caption("Open a step to see exactly what went in and what came out.")
        for t in tasks:
            icon, label, _ = ui.STATUS.get(t["status"], ("•", t["status"], ""))
            with st.expander(f"{icon} {ui.step_label(t['agent'])}  ({label.lower()})"):
                if t.get("error"):
                    st.error(ui.friendly_error(t["error"]))
                a, b = st.columns(2)
                a.markdown("**Input**")
                a.json(t.get("input_json") or {}, expanded=1)
                b.markdown("**Output**")
                b.json(t.get("output_json") or {}, expanded=1)


with ui.page_guard():
    st.title("📡 Live run")
    run_id = ui.run_picker("live")
    if run_id:
        first = ui.get_run(run_id)
        if not first.ok:
            st.error(first.error)
        else:
            live = first.data["status"] not in ui.FINAL_STATUSES
            (st.fragment(run_every=2)(body) if live else st.fragment(body))(run_id)
            if live:
                st.caption("This page updates by itself every 2 seconds.")
