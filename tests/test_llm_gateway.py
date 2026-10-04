import asyncio
import pytest
from pydantic import BaseModel

from app.core.llm_gateway import LLMError, complete_json
from app.core.orchestrator import create_run
from app.db.models import Event, Run
from app.db.session import SessionLocal
from sqlalchemy import select
from tests.conftest import FakeLLM


class Thing(BaseModel):
    name: str
    count: int


def _call(run_id=None, **kw):
    return asyncio.run(complete_json("sys", "user", Thing, run_id=run_id, **kw))


def test_valid_json_first_try(monkeypatch):
    llm = FakeLLM(monkeypatch, [{"name": "a", "count": 2}])
    assert _call() == Thing(name="a", count=2)
    assert len(llm.calls) == 1
    assert "JSON Schema" in llm.calls[0][0]["content"]          # schema is sent to the model


def test_code_fences_are_tolerated(monkeypatch):
    FakeLLM(monkeypatch, ['```json\n{"name": "a", "count": 1}\n```'])
    assert _call().count == 1


def test_invalid_json_is_retried_with_the_error(monkeypatch):
    llm = FakeLLM(monkeypatch, ["this is not json", {"name": "a", "count": "oops"}, {"name": "ok", "count": 3}])
    assert _call().name == "ok"
    assert len(llm.calls) == 3
    feedback = llm.calls[1][-1]["content"]                       # 2nd call carries the 1st error
    assert "invalid" in feedback and llm.calls[1][-2]["content"] == "this is not json"
    assert "count" in llm.calls[2][-1]["content"]                # 3rd call carries the schema error


def test_gives_up_after_two_retries(monkeypatch):
    llm = FakeLLM(monkeypatch, ["bad", "bad", "bad", "never used"])
    with pytest.raises(LLMError):
        _call()
    assert len(llm.calls) == 3                                   # 1 try + 2 retries


def test_provider_failure_is_not_retried(monkeypatch):
    llm = FakeLLM(monkeypatch, [LLMError("401 bad key"), {"name": "a", "count": 1}])
    with pytest.raises(LLMError):
        _call()
    assert len(llm.calls) == 1


def test_tokens_recorded_on_run_and_retries_logged(monkeypatch):
    FakeLLM(monkeypatch, ["bad", {"name": "a", "count": 1}], tokens=50)
    with SessionLocal() as db:
        rid = create_run(db, "obj").id
    _call(run_id=rid)
    with SessionLocal() as db:
        assert db.get(Run, rid).tokens == 100                    # both calls counted
        types = [e.type for e in db.scalars(select(Event).where(Event.run_id == rid))]
        assert types.count("llm_retry") == 1
