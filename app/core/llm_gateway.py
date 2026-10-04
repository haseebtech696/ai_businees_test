"""One function the whole system uses to talk to an LLM:  complete_json(system, user, schema).

 - calls the provider in JSON mode (Groq),
 - parses + validates the reply with Pydantic,
 - on failure retries (up to max_retries) telling the model exactly what was wrong,
 - records token usage on the run.
Tests replace `_call_provider`, so nothing here needs a network or an API key to be tested."""
import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import update

from app.core.config import settings
from app.core.errors import PermanentError
from app.core.errors import PermanentError
from app.core.events import log_event
from app.db.models import Run
from app.db.session import SessionLocal

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM is unavailable or never produced valid output."""


class LLMBudgetExceeded(LLMError, PermanentError):
    """The run used up its LLM call budget (MAX_LLM_CALLS_PER_RUN). Retrying cannot help."""


class LLMConfigError(LLMError, PermanentError):
    """Missing key / unsupported provider: retrying cannot help."""


_client = None


async def _call_provider(messages: list[dict]) -> tuple[str, int]:
    """Returns (raw_text, total_tokens). The only function that touches the network."""
    global _client
    if settings.llm_provider != "groq":
        raise LLMConfigError(f"unsupported LLM_PROVIDER '{settings.llm_provider}' (only 'groq' is implemented)")
    if not settings.llm_configured:
        raise LLMConfigError("GROQ_API_KEY is not set (add it to .env and restart the server)")
    if _client is None:
        from groq import AsyncGroq
        _client = AsyncGroq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_s)
    try:
        resp = await _client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            response_format={"type": "json_object"},   # JSON mode
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001  (auth, rate limit, network; the SDK already retries transients)
        raise LLMError(f"provider error: {type(e).__name__}: {e}") from e
    text = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0
    return text, tokens


def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    return m.group(1) if m else t


def _format_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors()[:8]:
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return str(e)


def _reserve_call(run_id: str | None, agent: str) -> None:
    """Global guard: atomically count one LLM call against the run's budget, or refuse."""
    if not run_id:
        return
    with SessionLocal() as db:
        res = db.execute(update(Run).where(Run.id == run_id, Run.llm_calls < settings.max_llm_calls_per_run)
                         .values(llm_calls=Run.llm_calls + 1))
        db.commit()
        if res.rowcount == 0 and db.get(Run, run_id) is not None:
            log_event(db, run_id, "llm_budget_exceeded", agent, limit=settings.max_llm_calls_per_run)
            raise LLMBudgetExceeded(f"LLM call budget exceeded: this run already made "
                                    f"{settings.max_llm_calls_per_run} calls (MAX_LLM_CALLS_PER_RUN).")


def _record_tokens(run_id: str | None, tokens: int) -> None:
    if not run_id or not tokens:
        return
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run:
            run.tokens = (run.tokens or 0) + tokens
            db.commit()


async def complete_json(system: str, user: str, schema: type[T], *,
                        run_id: str | None = None, agent: str = "manager", max_retries: int = 2) -> T:
    schema_hint = json.dumps(schema.model_json_schema())
    messages = [
        {"role": "system", "content": f"{system}\n\nReply with ONE JSON object that validates against "
                                      f"this JSON Schema, and nothing else:\n{schema_hint}"},
        {"role": "user", "content": user},
    ]
    last_error = "unknown"
    for attempt in range(max_retries + 1):
        _reserve_call(run_id, agent)                 # every attempt counts, retries included
        text, tokens = await _call_provider(messages)
        _record_tokens(run_id, tokens)
        try:
            return schema.model_validate_json(_strip_fences(text))
        except ValueError as e:                    # includes pydantic.ValidationError + bad JSON
            last_error = _format_error(e)
            if run_id:
                with SessionLocal() as db:
                    log_event(db, run_id, "llm_retry", agent, attempt=attempt + 1, error=last_error[:500])
            if attempt == max_retries:
                break
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"Your previous reply was invalid: {last_error}\n"
                                            "Fix these problems and reply again with ONLY the corrected JSON object."},
            ]
    raise LLMError(f"no valid JSON after {max_retries + 1} attempts: {last_error}")
