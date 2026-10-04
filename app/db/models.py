from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.session import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    objective: Mapped[str] = mapped_column(Text)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    plan_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    report_md: Mapped[str] = mapped_column(Text, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)      # guard: max_llm_calls_per_run
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tasks = relationship("Task", back_populates="run", cascade="all, delete")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. r1:t_01
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    task_key: Mapped[str] = mapped_column(String)  # t_01
    agent: Mapped[str] = mapped_column(String)
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="pending")
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    run = relationship("Run", back_populates="tasks")


class Event(Base):  # audit trail + live timeline
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    type: Mapped[str] = mapped_column(String)  # task_started, tool_called, approval_requested...
    agent: Mapped[str] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    task_key: Mapped[str] = mapped_column(String)
    action_type: Mapped[str] = mapped_column(String)  # send_email | create_event
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending/approved/rejected
    reviewer_note: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    company: Mapped[str] = mapped_column(String)
    website: Mapped[str] = mapped_column(String, nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=True)
    tier: Mapped[str] = mapped_column(String, nullable=True)
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    audit_json: Mapped[dict] = mapped_column(JSON, nullable=True)


class Message(Base):  # drafted/sent emails
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    to: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft/approved/sent(dry)


class Followup(Base):
    __tablename__ = "followups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    action: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, default="pending")
