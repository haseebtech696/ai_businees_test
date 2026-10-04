from contextlib import asynccontextmanager

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.approvals import ApprovalConflict, ApprovalNotFound, decide, revise_after_reject
from app.core.config import settings
from app.core.orchestrator import create_run, resume_workflow, run_workflow
from app.db.models import Approval, Event, Followup, Run
from app.db.session import SessionLocal, init_db
from app.schemas.api import (ApprovalOut, DecisionIn, EventOut, FollowupOut, RunCreate, RunCreated, RunDetail, RunSummary)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Business OS", lifespan=lifespan)
router = APIRouter(prefix="/api")


def get_db():
    with SessionLocal() as db:
        yield db


@app.get("/health")
def health():
    return {"status": "ok"}


@router.post("/runs", response_model=RunCreated, status_code=202)
def post_run(body: RunCreate, bg: BackgroundTasks, db: Session = Depends(get_db)):
    run = create_run(db, body.objective, body.input)
    bg.add_task(run_workflow, run.id)      # async workflow runs on the server's event loop
    return RunCreated(run_id=run.id, status=run.status)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(db: Session = Depends(get_db)):
    return db.scalars(select(Run).order_by(Run.created_at.desc())).all()


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
def get_events(run_id: str, after_id: int = 0, db: Session = Depends(get_db)):
    if not db.get(Run, run_id):
        raise HTTPException(404, "run not found")
    q = select(Event).where(Event.run_id == run_id, Event.id > after_id).order_by(Event.id)
    return db.scalars(q).all()


@router.get("/runs/{run_id}/followups", response_model=list[FollowupOut])
def get_followups(run_id: str, db: Session = Depends(get_db)):
    if not db.get(Run, run_id):
        raise HTTPException(404, "run not found")
    return db.scalars(select(Followup).where(Followup.run_id == run_id).order_by(Followup.due_at)).all()


@router.get("/config")
def get_config():
    """Safe, read-only settings the dashboard needs (never secrets)."""
    return {"dry_run": settings.dry_run}


@router.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(status: str | None = None, run_id: str | None = None, db: Session = Depends(get_db)):
    q = select(Approval).order_by(Approval.created_at.desc())
    if status:
        q = q.where(Approval.status == status)
    if run_id:
        q = q.where(Approval.run_id == run_id)
    return db.scalars(q).all()


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: str, db: Session = Depends(get_db)):
    a = db.get(Approval, approval_id)
    if not a:
        raise HTTPException(404, "approval not found")
    return a


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
def post_decision(approval_id: str, body: DecisionIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    """approve | edit -> the run resumes and the (dry-run) send happens.  reject -> one revised draft."""
    try:
        approval, nxt = decide(db, approval_id, body.decision, to=body.to, subject=body.subject,
                               body=body.body, note=body.note)
    except ApprovalNotFound as e:
        raise HTTPException(404, str(e)) from None
    except ApprovalConflict as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:                    # includes pydantic validation errors
        raise HTTPException(422, str(e)) from None
    if nxt == "resume":
        bg.add_task(resume_workflow, approval.run_id)
    else:
        bg.add_task(revise_after_reject, approval.id)
    return approval


app.include_router(router)
