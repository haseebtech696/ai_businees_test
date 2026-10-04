"""Lead view: how good a lead is this company, and why."""
from urllib.parse import urlsplit

import streamlit as st

import ui_kit as ui

with ui.page_guard():
    st.title("🎯 Lead")
    run_id = ui.run_picker("lead")
    if run_id:
        res = ui.get_run(run_id)
        if not res.ok:
            st.error(res.error)
            st.stop()
        run = res.data
        lead = ui.output_of(run, "scoring")
        profile = (ui.output_of(run, "research") or {}).get("profile") or {}
        site = ui.output_of(run, "website") or {}
        audit = site.get("audit") or {}
        scoring = ui.task_of(run, "scoring")

        if not lead or lead.get("score") is None:
            if run["status"] in ui.FINAL_STATUSES:
                why = ui.friendly_error(scoring["error"]) if scoring and scoring.get("error") else (
                    "The scoring step did not run, usually because the research or website step failed first.")
                st.warning(f"No lead score was produced for this run. {why}")
                ui.link("views/report.py", "Open the report for the full list of problems", "📄")
            else:
                st.info("The lead score appears here as soon as the scoring step finishes. Watch progress on the **Live run** page.")
                ui.link("views/live_run.py", "Go to Live run", "📡")
            st.stop()

        color, word = ui.TIER.get(lead["tier"], ("gray", "Lead"))
        st.header(lead["company"])
        st.caption(lead.get("website", ""))
        c1, c2, c3 = st.columns([1, 1, 3])
        c1.metric("Lead score", f"{lead['score']} / 100")
        c2.markdown("**Tier**")
        c2.markdown(f":{color}-badge[Tier {lead['tier']}: {word}]")
        c3.progress(lead["score"] / 100, text="A is 75 and above, B is 50 to 74, C is below 50")
        st.info(f"**Pitch angle:** {lead['pitch_angle']}")

        a, b = st.columns(2)
        with a:
            st.subheader("Why this score")
            st.markdown("\n".join(f"- {r}" for r in lead.get("reasons", [])) or "No reasons recorded.")
        with b:
            st.subheader("What to do next")
            st.markdown("\n".join(f"- {r}" for r in lead.get("recommended_actions", [])) or "No actions recorded.")

        st.subheader("Website issues")
        issues = audit.get("issues") or []
        if issues:
            if audit.get("summary"):
                st.write(audit["summary"])
            st.dataframe([{"Severity": ui.SEVERITY.get(i.get("severity"), i.get("severity")), "Issue": i.get("title"),
                           "Detail": i.get("detail")} for i in issues], hide_index=True, width="stretch")
        else:
            st.caption("No website audit is available for this run.")

        if profile:
            st.subheader("About the company")
            st.write(profile.get("summary", ""))
            st.markdown(f"**Industry:** {profile.get('industry', 'n/a')}  \n**Size:** {profile.get('size_cues', 'n/a')}  \n"
                        f"**Services:** {', '.join(profile.get('services', [])) or 'n/a'}")
        contact = audit.get("contact") or {}
        if contact:
            st.markdown(f"**Contact:** {', '.join(contact.get('emails', [])) or 'no email found'}  |  "
                        f"{', '.join(contact.get('phones', [])) or 'no phone found'}")

        sources = list(dict.fromkeys((lead.get("evidence") or []) + (site.get("evidence") or [])))
        if sources:
            with st.expander(f"Where this came from ({len(sources)} pages)"):
                st.markdown("\n".join(f"- [{urlsplit(u).netloc}{urlsplit(u).path}]({u})" for u in sources))
