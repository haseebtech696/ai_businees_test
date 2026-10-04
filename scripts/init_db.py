"""Phase 0 checkpoint: create tables, insert a Run + Task, read them back."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, select
from app.db.session import init_db, engine, SessionLocal
from app.db.models import Run, Task


def main():
    init_db()
    tables = sorted(inspect(engine).get_table_names())
    print("Tables:", tables)

    with SessionLocal() as db:
        # clean re-runs
        old = db.get(Run, "r_test")
        if old:
            db.delete(old)
            db.commit()

        run = Run(id="r_test", objective="Smoke test run", input_json={"x": 1})
        run.tasks.append(Task(id="r_test:t_01", run_id="r_test", task_key="t_01",
                              agent="research", goal="Smoke test task",
                              depends_on=[], input_json={"q": "hello"}))
        db.add(run)
        db.commit()

    with SessionLocal() as db:
        run = db.scalars(select(Run).where(Run.id == "r_test")).one()
        task = run.tasks[0]
        print(f"Run : {run.id} | {run.objective} | status={run.status} | created={run.created_at}")
        print(f"Task: {task.id} | agent={task.agent} | status={task.status} | input={task.input_json}")
        assert run.status == "pending" and task.status == "pending"
    print("OK: Phase 0 checkpoint passed")


if __name__ == "__main__":
    main()
