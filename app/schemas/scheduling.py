"""Meeting proposal (Phase 5, mock calendar)."""
from pydantic import BaseModel, Field


class Slot(BaseModel):
    start: str      # ISO local date-time, e.g. 2026-10-06T10:00
    label: str      # how it is written in the email, e.g. "Tue 6 Oct, 10:00"


class MeetingProposal(BaseModel):
    title: str
    duration_min: int
    timezone: str
    slots: list[Slot] = Field(min_length=3, max_length=3)
    note: str = ""
