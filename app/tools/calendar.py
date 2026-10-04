"""Mock calendar: turn the slot strings from business_profile.yaml into concrete future date-times."""
import re
from datetime import datetime, timedelta

from app.core.business_profile import BusinessProfile
from app.core.errors import PermanentError
from app.schemas.scheduling import MeetingProposal, Slot

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAY_RE = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}):(\d{2})$")
MIN_LEAD = timedelta(hours=24)       # never offer a slot sooner than a day from now


def label_for(dt: datetime) -> str:
    return f"{DAY_NAMES[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}, {dt:%H:%M}"


def parse_slot(spec: str, now: datetime) -> datetime:
    spec = spec.strip()
    m = WEEKDAY_RE.match(spec)
    if m:
        day, hh, mm = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if day not in WEEKDAYS or hh > 23 or mm > 59:
            raise PermanentError(f"Invalid meeting slot '{spec}'. Use e.g. 'Tue 10:00'.")
        earliest = now + MIN_LEAD
        for add in range(0, 8):
            cand = (earliest + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand.weekday() == WEEKDAYS.index(day) and cand >= earliest:
                return cand
    try:
        return datetime.fromisoformat(spec).replace(tzinfo=None)
    except ValueError:
        raise PermanentError(f"Invalid meeting slot '{spec}'. Use 'Tue 10:00' or an ISO date-time.") from None


def build_proposal(biz: BusinessProfile, now: datetime) -> MeetingProposal:
    slots = sorted({parse_slot(s, now) for s in biz.meeting.available_slots})
    future = [s for s in slots if s >= now + MIN_LEAD]
    if len(future) < 3:
        raise PermanentError(f"business_profile.yaml needs 3 distinct future meeting slots under meeting.available_slots; "
                             f"found {len(future)} (slots in the past are ignored).")
    return MeetingProposal(title=biz.meeting.title, duration_min=biz.meeting.duration_min, timezone=biz.meeting.timezone,
                           slots=[Slot(start=s.strftime("%Y-%m-%dT%H:%M"), label=label_for(s)) for s in future[:3]],
                           note="Mock calendar: slots come from business_profile.yaml.")
