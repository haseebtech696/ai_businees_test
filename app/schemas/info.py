"""LLM output shapes for the information agents (Phase 3)."""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Issue(BaseModel):
    title: str
    severity: Literal["low", "medium", "high"]
    detail: str = ""


class ContactInfo(BaseModel):
    emails: list[str] = Field(default_factory=list)     # filled from scraped pages, never by the LLM
    phones: list[str] = Field(default_factory=list)     # filled from scraped pages, never by the LLM
    contact_page: Optional[str] = None
    notes: str = ""


class SiteAudit(BaseModel):
    summary: str
    issues: list[Issue] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    contact: ContactInfo = Field(default_factory=ContactInfo)


class CompanyProfile(BaseModel):
    company_name: str
    industry: str
    size_cues: str = "unknown"
    services: list[str] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    summary: str = ""


class LeadAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    tier: Literal["A", "B", "C"]
    reasons: list[str] = Field(min_length=1)
    recommended_actions: list[str] = Field(min_length=1)
    pitch_angle: str

    @field_validator("score", mode="before")
    @classmethod
    def _round(cls, v):
        return round(v) if isinstance(v, float) else v


def tier_for(score: int) -> str:
    return "A" if score >= 75 else "B" if score >= 50 else "C"
