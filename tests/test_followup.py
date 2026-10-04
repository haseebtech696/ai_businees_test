import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agents.followup import followup_agent
from app.agents.verification import check_followup
from app.db.models import Followup, now
from app.db.session import SessionLocal
from app.schemas.contracts import AgentTask
from tests.test_tool_guard import setup_run

SLOTS = [{"start": "2026-10-06T10:00", "label": "Tue 6 Oct, 10:00"}, {"start": "2026-10-07T14:00", "label": "Wed 7 Oct, 14:00"},
         {"start": "2026-10-08T11:30", "label": "Thu 8 Oct, 11:30"}]


def run(rid, tier, with_slots=True):
    ctx = {"t_03": {"company": "Acme Dental", "tier": tier, "pitch_angle": "Add online booking."}}
    if with_slots:
        ctx["t_04"] = {"proposal": {"slots": SLOTS}}
    t = AgentTask(run_id=rid, task_key="t_06", agent="followup", goal="Plan follow-ups", input={})
    return asyncio.run(followup_agent(t, ctx)).output


def rows(rid):
    with SessionLocal() as db:
        return list(db.scalars(select(Followup).where(Followup.run_id == rid).order_by(Followup.due_at)))


@pytest.mark.parametrize("tier,days", [("A", [3, 7, 14]), ("B", [3, 7]), ("C", [7, 14])])
def test_two_or_three_dated_actions_are_saved(tier, days):
    rid, _ = setup_run()
    out = run(rid, tier)
    assert check_followup(out) == [] and len(out["followups"]) == len(days)
    saved = rows(rid)
    assert [f.status for f in saved] == ["pending"] * len(days)
    for f, d in zip(saved, days):
        assert abs((f.due_at - (now() + timedelta(days=d))).total_seconds()) < 120
        assert "Acme Dental" in f.action


def test_check_in_refers_to_the_offered_slot_and_nudge_repeats_the_pitch():
    rid, _ = setup_run()
    texts = [i["action"] for i in run(rid, "A")["followups"]]
    assert "Add online booking." in texts[0] and "Tue 6 Oct, 10:00" in texts[1]


def test_rerunning_does_not_duplicate_rows():
    rid, _ = setup_run()
    run(rid, "A")
    run(rid, "A")
    assert len(rows(rid)) == 3


def test_missing_slots_and_unknown_tier_still_work():
    rid, _ = setup_run()
    out = run(rid, "Z", with_slots=False)
    assert len(out["followups"]) == 2                     # unknown tier is treated as B
