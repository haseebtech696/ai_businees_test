"""Report: the final written summary of a run."""
import streamlit as st

import ui_kit as ui

with ui.page_guard():
    st.title("📄 Report")
    run_id = ui.run_picker("report")
    if run_id:
        res = ui.get_run(run_id)
        if not res.ok:
            st.error(res.error)
            st.stop()
        run = res.data
        if run.get("report_md"):
            if run["status"] == "failed":
                st.warning("This run had problems. They are listed under **Failures** at the end of the report.")
            st.download_button("⬇ Download report (.md)", run["report_md"], file_name=f"report_{run['id']}.md",
                               mime="text/markdown", type="primary")
            st.divider()
            st.markdown(run["report_md"])
        elif run["status"] == "awaiting_approval":
            st.info("The report is written after you decide on the email. Open the **Approvals** page to continue.")
            ui.link("views/approvals.py", "Go to Approvals", "🔔")
        elif run["status"] not in ui.FINAL_STATUSES:
            st.info("The report is written at the very end of the run. Watch progress on the **Live run** page.")
            ui.link("views/live_run.py", "Go to Live run", "📡")
        else:
            st.warning("This run ended before a report could be written.")
