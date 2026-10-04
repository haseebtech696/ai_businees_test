"""business_profile.yaml: who the agency is. Trusted input (written by you, not scraped)."""
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.errors import PermanentError

ROOT = Path(__file__).resolve().parents[2]


class MeetingConfig(BaseModel):
    """Mock calendar: three hardcoded free slots. 'Tue 10:00' = the next Tuesday at 10:00; a full ISO date-time also works."""
    title: str = "Intro call"
    duration_min: int = Field(default=30, ge=5, le=240)
    timezone: str = "your local time"          # a label shown in the email; times are not converted
    available_slots: list[str] = Field(default_factory=lambda: ["Tue 10:00", "Wed 14:00", "Thu 11:30"])


class BusinessProfile(BaseModel):
    agency_name: str = Field(min_length=1)
    sender_name: str = Field(min_length=1)
    sender_title: str = ""
    website: str = ""
    tone: str = "friendly, concise and professional"
    services: list[str] = Field(default_factory=list)
    offer_summary: str = ""
    meeting: MeetingConfig = Field(default_factory=MeetingConfig)


def load_business_profile(path: str | Path | None = None) -> BusinessProfile:
    p = Path(path or settings.business_profile_path)
    candidates = [p] if p.is_absolute() else [Path.cwd() / p, ROOT / p]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        raise PermanentError(f"{p} not found. Create it in the project root (see business_profile.yaml) "
                             "with your agency_name, sender_name, tone and services.")
    try:
        data = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
        return BusinessProfile.model_validate(data)
    except (yaml.YAMLError, ValidationError, TypeError) as e:
        raise PermanentError(f"{found} is invalid: {e}") from e
