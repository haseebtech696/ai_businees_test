"""Email shapes (Phase 4)."""
import re

from pydantic import BaseModel, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,40}\]|\{\{.*?\}\}|<(?:name|company|your)[^>]*>", re.I)


def _no_newlines(v: str, field: str) -> str:
    v = v.strip()
    if "\n" in v or "\r" in v:
        raise ValueError(f"{field} must be a single line")
    return v


class EmailDraft(BaseModel):
    """What a human approves and what gets sent."""
    to: str = ""
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=20)

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        v = _no_newlines(v, "to")
        if v and not EMAIL_RE.match(v):
            raise ValueError(f"'{v}' is not a valid email address")
        return v

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        v = _no_newlines(v, "subject")
        if PLACEHOLDER_RE.search(v):
            raise ValueError("subject contains placeholder text such as [Name]; write the real text")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = v.strip()
        if PLACEHOLDER_RE.search(v):
            raise ValueError("body contains placeholder text such as [Name]; write the real text before approving")
        return v


class OutreachDraft(BaseModel):
    """What the Communication agent's LLM call must return."""
    subject: str
    body: str
    proposal_md: str

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        v = _no_newlines(v, "subject")
        if not 3 <= len(v) <= 120:
            raise ValueError("subject must be 3-120 characters")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 30:
            raise ValueError("body is too short")
        if PLACEHOLDER_RE.search(v):
            raise ValueError("body contains a placeholder like [Name]; write the real text instead")
        if len(v.split()) > 220:
            raise ValueError("body must be at most 220 words")
        return v

    @field_validator("proposal_md")
    @classmethod
    def _proposal(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 20:
            raise ValueError("proposal_md is too short")
        if PLACEHOLDER_RE.search(v):
            raise ValueError("proposal_md contains a placeholder; write the real text instead")
        return v
