from sqlalchemy import create_engine, inspect, text

from app.db.session import init_db


def test_an_existing_database_gets_the_new_llm_calls_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:                      # a Phase 0-4 runs table: no llm_calls column
        c.execute(text("CREATE TABLE runs (id VARCHAR PRIMARY KEY, objective TEXT, input_json JSON, status VARCHAR, "
                       "plan_json JSON, report_md TEXT, tokens INTEGER, created_at DATETIME, finished_at DATETIME)"))
        c.execute(text("INSERT INTO runs (id, objective, status, tokens) VALUES ('r_old', 'o', 'succeeded', 5)"))
    init_db(eng)
    init_db(eng)                                # idempotent
    assert "llm_calls" in {c["name"] for c in inspect(eng).get_columns("runs")}
    with eng.begin() as c:
        assert c.execute(text("SELECT llm_calls FROM runs WHERE id='r_old'")).scalar() in (0, None)
