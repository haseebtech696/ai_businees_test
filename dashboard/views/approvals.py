"""Approvals: every action waiting for a human decision. Nothing is sent until you decide."""
import streamlit as st

import ui_kit as ui


def decide(approval_id: str, **body) -> None:
    res = ui.api("POST", f"/approvals/{approval_id}/decision", json=body)
    if res.ok:
        st.session_state["flash"] = {"approve": "Approved. The run is continuing.",
                                     "edit": "Your edits were saved and approved. The run is continuing.",
                                     "reject": "Rejected. A new draft is being written from your feedback."}[body["decision"]]
        st.rerun(scope="app")                     # refresh the sidebar count too
    st.error(res.error)


with ui.page_guard():
    st.title("🔔 Approvals")
    if ui.is_dry_run():
        st.info("Safe mode is on: approving does **not** send a real email. The message is only marked 'sent (dry run)'.")
    else:
        st.error("LIVE MODE: approving sends the email for real.")
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))

    pending = ui.pending_approvals()
    if not pending:
        st.write("✅ Nothing is waiting for you.")
        ui.link("views/new_run.py", "Start a new run", "➕")

    for a in pending:
        p, aid = a["payload_json"], a["id"]
        with st.container(border=True):
            st.caption(f"Run `{a['run_id']}` | draft revision {p.get('revision', 0)}")
            if a["action_type"] != "send_email":                  # a calendar hold (only when safe mode is off)
                st.subheader(f"Calendar hold: {p.get('title', '')}")
                st.write(f"Start {p.get('start')} | {p.get('duration_min')} min | tentative")
                note = st.text_input("Reason (used if you reject)", key=f"note_{aid}")
                c1, c2 = st.columns(2)
                if c1.button("Approve", key=f"approve_{aid}", type="primary"):
                    decide(aid, decision="approve")
                if c2.button("Reject", key=f"reject_{aid}"):
                    decide(aid, decision="reject", note=note)
                continue

            st.subheader(f"Email to {p.get('company', 'the company')}")
            v = p.get("verification") or {}
            if v.get("passed") and not v.get("warnings"):
                st.success("Automatic checks passed: names the company, no placeholder text, has an opt-out line, offers the meeting times.")
            for w in v.get("warnings", []):
                st.warning(f"Check: {w}")
            cl = v.get("claims") or {}
            if cl.get("checked") and not cl.get("unsupported"):
                st.caption("Fact check: every claim in the email matches what was found.")
            elif not cl.get("checked"):
                st.caption(f"Fact check not run: {cl.get('reason', 'not available')}")
            if v.get("regenerated"):
                st.caption("The first draft failed a check, so it was rewritten once automatically.")

            to = st.text_input("To", p.get("to", ""), key=f"to_{aid}")
            if not to:
                st.warning("No address was found on the website. Add one before a real send.")
            subject = st.text_input("Subject", p["subject"], key=f"subject_{aid}")
            body = st.text_area("Email", p["body"], height=300, key=f"body_{aid}")
            if p.get("proposal_md"):
                with st.expander("Proposal (Markdown)"):
                    st.markdown(p["proposal_md"])
            note = st.text_input("Feedback for a rewrite (used if you reject)", key=f"note_{aid}",
                                 placeholder="e.g. shorter, and mention online booking first")
            c1, c2, c3 = st.columns(3)
            c1.markdown("&nbsp;")
            if c1.button("✅ Approve as written", key=f"approve_{aid}", type="primary"):
                decide(aid, decision="approve")
            if c2.button("✏️ Save my edits and approve", key=f"edit_{aid}"):
                if (to, subject, body) == (p.get("to", ""), p["subject"], p["body"]):
                    st.warning("You have not changed anything yet. Edit the fields above, or use Approve.")
                else:
                    decide(aid, decision="edit", to=to, subject=subject, body=body)
            if c3.button("↩️ Reject and rewrite", key=f"reject_{aid}"):
                decide(aid, decision="reject", note=note)

    with st.expander("Recently decided"):
        res = ui.api("GET", "/approvals")
        done = [a for a in (res.data if res.ok else []) if a["status"] != "pending"][:10]
        if done:
            st.dataframe([{"Run": a["run_id"], "Decision": a["status"], "Subject": a["payload_json"].get("subject", ""),
                           "Note": a.get("reviewer_note") or ""} for a in done], hide_index=True, width="stretch")
        else:
            st.caption("Nothing decided yet.")
