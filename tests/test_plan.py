import pytest
from app.schemas.contracts import Plan, PlanTask, Status
from app.core.planner import stub_plan


def test_stub_plan_has_8_tasks():
    assert len(stub_plan("x").tasks) == 8


def test_research_and_website_have_no_dependencies():
    roots = [t.agent for t in stub_plan("x").tasks if not t.depends_on]
    assert sorted(roots) == ["research", "website"]


def test_unknown_dependency():
    with pytest.raises(ValueError):
        Plan(objective="x", tasks=[PlanTask(task_key="a", agent="research", depends_on=["zz"])])


def test_cycle():
    with pytest.raises(ValueError):
        Plan(objective="x", tasks=[PlanTask(task_key="a", agent="r", depends_on=["b"]),
                                   PlanTask(task_key="b", agent="r", depends_on=["a"])])


def test_status_matches_plain_strings():
    assert Status.SUCCEEDED == "succeeded" and "failed" in {Status.FAILED}
