"""Run the stub pipeline end to end without the API."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.core.orchestrator import create_run, run_workflow
from app.db.models import Event, Run
from app.db.session import SessionLocal, init_db

init_db()
with SessionLocal() as db:
    run_id = create_run(db, "Find 2 dental clinics and draft outreach", {"industry": "dental"}).id
asyncio.run(run_workflow(run_id))

with SessionLocal() as db:
    run = db.get(Run, run_id)
    print(f"Run {run.id}: {run.status}")
    for t in run.tasks:
        print(f"  {t.task_key} {t.agent:<14} {t.status}")
    print("\nEvents (note t_01 and t_02 start together):")
    for e in db.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id)):
        print(f"  {e.id:>2} {e.ts:%H:%M:%S.%f} {e.type:<18} {e.payload_json.get('task_key', '')}")
    print("\n" + (run.report_md or ""))
