import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


# create_all never alters an existing table, so columns added after Phase 0 are added here.
NEW_COLUMNS = {"runs": {"llm_calls": "INTEGER DEFAULT 0"}}


def init_db(eng=None):
    from app.db import models  # noqa: F401  (register tables)
    eng = eng or engine
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    with eng.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
