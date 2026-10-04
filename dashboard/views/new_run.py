"""New run: tell the system which company to look at."""
import streamlit as st

import ui_kit as ui

st.session_state.setdefault("nr_objective", ui.DEMO["objective"])
for k in ("nr_company", "nr_url", "nr_email"):
    st.session_state.setdefault(k, "")


def load_demo() -> None:
    st.session_state.update({"nr_objective": ui.DEMO["objective"], "nr_company": ui.DEMO["company"],
                             "nr_url": ui.DEMO["url"], "nr_email": ui.DEMO["email"]})


with ui.page_guard():
    st.title("➕ New run")
    st.write("Give the system a company website. It researches the company, audits the site, scores the lead, "
             "writes an outreach email and a proposal, and finishes with a report.")
    if ui.is_dry_run():
        st.success("Safe mode is on: **no real email is ever sent.** You approve every email, and even then it is only marked "
                   "'sent (dry run)'.")
    else:
        st.error("LIVE MODE: approving an email will really send it.")

    with st.expander("How it works", expanded=not ui.list_runs()):
        st.markdown(
            "1. **Research and audit** run side by side: who the company is, and what is weak on its website.\n"
            "2. The lead is **scored**, and three **meeting times** are proposed.\n"
            "3. An **email** is drafted. **The run pauses and waits for you.** Read it, edit it, approve it, or reject it with feedback.\n"
            "4. After your decision the system plans **follow-ups**, **checks its own work**, and writes the **report**.")

    st.button("✨ Load demo scenario", on_click=load_demo, help="Fills in a ready-made example you can run straight away.")

    st.text_area("What should the system do?", key="nr_objective", height=80)
    c1, c2 = st.columns(2)
    c1.text_input("Company name (optional)", key="nr_company", placeholder="Acme Dental",
                  help="Helps the research step. If you leave it empty, the name is read from the website.")
    c2.text_input("Company website", key="nr_url", placeholder="https://example.com",
                  help="Required. The system reads the home page and a few other pages of this site.")
    st.text_input("Contact email (optional)", key="nr_email", placeholder="name@company.com",
                  help="Where the email is addressed. Leave it empty to use an address found on the website.")

    if st.button("▶ Start run", type="primary"):
        problems = ui.validate_new_run(st.session_state.nr_objective, st.session_state.nr_url, st.session_state.nr_email)
        if problems:
            for p in problems:
                st.error(p)
        else:
            payload = {"url": ui.normalize_url(st.session_state.nr_url)}
            if st.session_state.nr_company.strip():
                payload["company"] = st.session_state.nr_company.strip()
            if st.session_state.nr_email.strip():
                payload["recipient_email"] = st.session_state.nr_email.strip()
            res = ui.api("POST", "/runs", json={"objective": st.session_state.nr_objective.strip(), "input": payload})
            if res.ok:
                ui.set_run(res.data["run_id"])
                st.success("Run started. Opening the live view...")
                ui.go("views/live_run.py")
            else:
                st.error(res.error)
