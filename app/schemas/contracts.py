"""Frozen contracts (Phase 1). Agents can change freely behind these."""
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, model_validator


class Status(str, Enum):
    """Lifecycle for both Task.status and Run.status (stored as plain strings in the DB)."""
    PENDING = "pending"
    PLANNING = "planning"                  # Manager is building the plan (Run only)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"                    # a dependency failed
    AWAITING_APPROVAL = "awaiting_approval"  # paused: a human must decide (not done, not failed)
    APPROVED = "approved"                    # human said yes; the guarded action has not run yet

    __hash__ = str.__hash__  # so members and plain strings match in sets/dicts


class PlanTask(BaseModel):
    task_key: str                          # t_01
    agent: str                             # research | website | scoring | ...
    goal: str = ""
    depends_on: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    objective: str
    tasks: list[PlanTask]

    @model_validator(mode="after")
    def _validate_graph(self):
        keys = [t.task_key for t in self.tasks]
        if not keys:
            raise ValueError("plan has no tasks")
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate task_key in plan")
        known = set(keys)
        for t in self.tasks:
            for d in t.depends_on:
                if d not in known:
                    raise ValueError(f"{t.task_key} depends on unknown task {d}")
                if d == t.task_key:
                    raise ValueError(f"{t.task_key} depends on itself")
        indeg = {t.task_key: len(t.depends_on) for t in self.tasks}   # cycle check (Kahn)
        queue = [k for k, n in indeg.items() if n == 0]
        seen = 0
        while queue:
            k = queue.pop()
            seen += 1
            for t in self.tasks:
                if k in t.depends_on:
                    indeg[t.task_key] -= 1
                    if indeg[t.task_key] == 0:
                        queue.append(t.task_key)
        if seen != len(keys):
            raise ValueError("plan contains a dependency cycle")
        return self


class AgentTask(BaseModel):
    """What an agent receives as its first argument."""
    run_id: str
    task_key: str
    agent: str
    goal: str = ""
    input: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """What an agent returns."""
    status: Literal["ok", "needs_approval", "error"] = "ok"
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    tokens: int = 0
