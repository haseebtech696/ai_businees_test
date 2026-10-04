import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.scheduling import scheduling_agent
from app.core.business_profile import BusinessProfile, MeetingConfig, load_business_profile
from app.core.config import settings
from app.core.errors import PermanentError
from app.db.models import Approval, Event, Task
from app.db.session import SessionLocal
from app.main import app
from app.schemas.contracts import AgentTask
from app.tools.calendar import build_proposal, label_for, parse_slot
from tests.test_tool_guard import setup_run

SAT = datetime(2026, 10, 3, 12, 0)                  # a Saturday
BIZ = BusinessProfile(agency_name="A", sender_name="B")


def test_weekday_slots_become_the_next_future_occurrences():
    p = build_proposal(BIZ, SAT)
    assert [s.label for s in p.slots] == ["Tue 6 Oct, 10:00", "Wed 7 Oct, 14:00", "Thu 8 Oct, 11:30"]
    assert p.slots[0].start == "2026-10-06T10:00" and p.duration_min == 30 and len(p.slots) == 3


def test_a_slot_less_than_a_day_away_moves_to_next_week():
    tue_9am = datetime(2026, 10, 6, 9, 0)
    assert parse_slot("Tue 10:00", tue_9am) == datetime(2026, 10, 13, 10, 0)


def test_iso_slots_work_and_past_ones_are_ignored():
    biz = BusinessProfile(agency_name="A", sender_name="B", meeting=MeetingConfig(available_slots=[
        "2026-10-10T09:00", "2026-10-11T09:00", "2026-09-01T09:00", "2026-10-12T09:00"]))
    assert [s.start for s in build_proposal(biz, SAT).slots] == ["2026-10-10T09:00", "2026-10-11T09:00", "2026-10-12T09:00"]


def test_fewer_than_three_future_slots_is_a_clear_error():
    biz = BusinessProfile(agency_name="A", sender_name="B", meeting=MeetingConfig(available_slots=["Tue 10:00", "2026-01-01T09:00"]))
    with pytest.raises(PermanentError, match="3 distinct future"):
        build_proposal(biz, SAT)


@pytest.mark.parametrize("bad", ["Funday 10:00", "Tue 25:00", "next tuesday", ""])
def test_invalid_slot_text_is_a_clear_error(bad):
    with pytest.raises(PermanentError, match="Invalid meeting slot"):
        parse_slot(bad, SAT)


def test_labels_do_not_depend_on_the_locale():
    assert label_for(datetime(2026, 12, 1, 8, 5)) == "Tue 1 Dec, 08:05"


def test_old_business_profiles_without_a_meeting_block_still_load(tmp_path):
    f = tmp_path / "bp.yaml"
    f.write_text("agency_name: X\nsender_name: Y\n")
    assert len(load_business_profile(f).meeting.available_slots) == 3


def test_shipped_business_profile_has_three_slots():
    assert len(load_business_profile().meeting.available_slots) == 3


def run_agent(rid):
    t = AgentTask(run_id=rid, task_key="t_01", agent="scheduling", goal="Propose meeting", input={})
    return asyncio.run(scheduling_agent(t, {"t_03": {"company": "Acme Dental", "pitch_angle": "x"}}))


def test_dry_run_only_logs_the_calendar_hold(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    rid, _ = setup_run()
    res = run_agent(rid)
    assert res.status == "ok" and len(res.output["proposal"]["slots"]) == 3
    assert res.output["event"]["status"] == "logged (dry run)" and res.output["approval_id"] is None
    with SessionLocal() as db:
        assert db.scalars(select(Approval).where(Approval.run_id == rid)).first() is None      # no approval needed
        ev = [e for e in db.scalars(select(Event).where(Event.run_id == rid)) if e.type == "tool_called"]
        assert ev and ev[0].payload_json["tool"] == "create_event" and ev[0].payload_json["status"] == "logged (dry run)"


def test_live_mode_asks_for_approval_before_creating_an_event(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    rid, _ = setup_run()
    res = run_agent(rid)
    assert res.status == "needs_approval" and res.output["event"] is None
    with SessionLocal() as db:
        a = db.scalars(select(Approval).where(Approval.run_id == rid)).one()
        assert a.action_type == "create_event" and a.status == "pending"
        assert a.payload_json["tentative"] is True and db.get(Task, f"{rid}:t_01").status == "awaiting_approval"


def test_event_approvals_cannot_be_edited_and_rejecting_one_fails_the_task(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    rid, _ = setup_run()
    run_agent(rid)
    with SessionLocal() as db:
        aid = db.scalars(select(Approval).where(Approval.run_id == rid)).one().id
    with TestClient(app) as c:
        r = c.post(f"/api/approvals/{aid}/decision", json={"decision": "edit", "subject": "new subject"})
        assert r.status_code == 422 and "cannot be edited" in r.json()["detail"]
        assert c.post(f"/api/approvals/{aid}/decision", json={"decision": "reject", "note": "wrong slot"}).status_code == 200
    with SessionLocal() as db:
        t = db.get(Task, f"{rid}:t_01")
        assert t.status == "failed" and "wrong slot" in t.error
