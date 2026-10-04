from sqlalchemy.orm import Session
from app.db.models import Event


def log_event(db: Session, run_id: str, type: str, agent: str | None = None, **payload) -> None:
    db.add(Event(run_id=run_id, type=type, agent=agent, payload_json=payload))
    db.commit()


def emit_event(run_id: str, type: str, agent: str | None = None, **payload) -> None:
    """log_event with its own short session (for agents and background jobs)."""
    from app.db.session import SessionLocal
    with SessionLocal() as db:
        log_event(db, run_id, type, agent, **payload)
