from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class RunCreate(BaseModel):
    objective: str = Field(min_length=3)
    input: dict[str, Any] = Field(default_factory=dict)


class RunCreated(BaseModel):
    run_id: str
    status: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    task_key: str
    agent: str
    goal: str
    status: str
    depends_on: list
    attempts: int
    input_json: Optional[dict] = None
    output_json: Optional[dict] = None
    error: Optional[str] = None


class RunDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    objective: str
    status: str
    tokens: int
    llm_calls: int = 0
    created_at: datetime
    finished_at: Optional[datetime] = None
    input_json: dict = {}
    plan_json: Optional[dict] = None
    report_md: Optional[str] = None
    tasks: list[TaskOut] = []


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    objective: str
    status: str
    tokens: int
    llm_calls: int = 0
    created_at: datetime
    finished_at: Optional[datetime] = None
    input_json: dict = {}


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    agent: Optional[str] = None
    payload_json: dict = {}
    ts: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str
    task_key: str
    action_type: str
    payload_json: dict
    status: str
    reviewer_note: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class DecisionIn(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    to: Optional[str] = None            # edit only
    subject: Optional[str] = None       # edit only
    body: Optional[str] = None          # edit only
    note: Optional[str] = None          # reviewer note; for reject it is the feedback for the rewrite


class FollowupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: str
    action: str
    due_at: datetime
    status: str
