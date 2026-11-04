"""Cost and latency log: one row per model call.

Logging must never break the app, so every failure here is swallowed.
Only sizes and timings are stored: never the API key, images or full prompts.
"""
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from app.db import get_conn, MODEL_CALLS_SQL

_purpose: ContextVar[str] = ContextVar("model_purpose", default="")


@contextmanager
def purpose(name: str):
    """Tag every model call made inside the block, e.g. with purpose("ad_copy"): ..."""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def record(kind: str, model: str, seconds: float, ok: bool, error: str | None = None, usage=None):
    try:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        with get_conn() as conn:
            conn.executescript(MODEL_CALLS_SQL)          # also works before init_db has run
            conn.execute(
                "INSERT INTO model_calls (kind, purpose, model, seconds, ok, error, prompt_tokens, completion_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, _purpose.get(), model, round(seconds, 3), 1 if ok else 0,
                 (error or None) and error[:200], prompt_tokens, completion_tokens),
            )
    except Exception:
        pass


@contextmanager
def timed(kind: str, model: str):
    """Time one model call. Put the API call inside and set box["resp"] to its response."""
    box = {"resp": None}
    start = time.perf_counter()
    try:
        yield box
    except Exception as e:
        record(kind, model, time.perf_counter() - start, False, f"{type(e).__name__}: {e}")
        raise
    record(kind, model, time.perf_counter() - start, True, None, getattr(box["resp"], "usage", None))


def unit_prices() -> dict:
    """Credits (or money) per call by kind, from .env. Unset means unknown, shown as 0."""
    def f(name):
        try:
            return float(os.getenv(name, "0") or 0)
        except ValueError:
            return 0.0
    return {"text": f("COST_TEXT"), "vision": f("COST_VISION"),
            "image_generate": f("COST_IMAGE"), "image_edit": f("COST_IMAGE")}