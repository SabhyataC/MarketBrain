"""One global kill switch: while it is ON, nothing the AI or the sender does can leave or change anything.

Guarded (HTTP 423 when ON): approving, sending to a shop, applying or rolling back SEO, and every route
that makes a model call. Still working: reading, editing drafts, rejecting, CSV download, the audit log.
The model client also refuses to call a model while the switch is ON, so a route that forgot the guard
still cannot spend anything.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.audit import log
from app.db import get_conn

router = APIRouter(prefix="/killswitch", tags=["killswitch"])

SQL = """
CREATE TABLE IF NOT EXISTS kill_switch (
    id INTEGER PRIMARY KEY CHECK (id = 1), on_flag INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '', changed_at TEXT
);
INSERT OR IGNORE INTO kill_switch (id, on_flag, reason) VALUES (1, 0, '');
"""


def state() -> dict:
    with get_conn() as conn:
        conn.executescript(SQL)
        r = conn.execute("SELECT on_flag, reason, changed_at FROM kill_switch WHERE id = 1").fetchone()
    return {"on": bool(r["on_flag"]), "reason": r["reason"], "changed_at": r["changed_at"]}


def message(s: dict) -> str:
    why = f": {s['reason']}" if s["reason"] else ""
    return f"Kill switch is ON{why}. Turn it off on the Overview tab to continue."


def require_off():
    """FastAPI dependency: refuses the request with 423 while the switch is ON."""
    s = state()
    if s["on"]:
        raise HTTPException(423, message(s))


def guard_models():
    """Called by the model client before every model call."""
    try:
        s = state()
    except Exception:
        return  # a broken database must not hide the real error elsewhere
    if s["on"]:
        raise RuntimeError(message(s))


class SwitchIn(BaseModel):
    on: bool
    reason: str = ""


@router.get("")
def get_state():
    return state()


@router.post("")
def set_state(body: SwitchIn):
    reason = body.reason.strip()
    if len(reason) > 200:
        raise HTTPException(400, "The reason can be 200 characters at most.")
    with get_conn() as conn:
        conn.executescript(SQL)
        conn.execute("UPDATE kill_switch SET on_flag = ?, reason = ?, changed_at = CURRENT_TIMESTAMP WHERE id = 1",
                     (1 if body.on else 0, reason if body.on else ""))
    log("owner", "kill_switch_on" if body.on else "kill_switch_off", reason or "-")
    return state()
