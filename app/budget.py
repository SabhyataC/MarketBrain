import os
import json
from fastapi import HTTPException
from dotenv import load_dotenv

load_dotenv()

MAX_DAILY_BUDGET = float(os.getenv("MAX_DAILY_BUDGET", "500"))
MAX_TOTAL_DAILY_BUDGET = float(os.getenv("MAX_TOTAL_DAILY_BUDGET", "1000"))


def check_budget(payload: dict):
    amount = payload.get("daily_budget")
    if amount is None:
        raise HTTPException(400, "Ad proposals must include daily_budget")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(400, "daily_budget must be a positive number")
    if amount > MAX_DAILY_BUDGET:
        raise HTTPException(
            400, f"daily_budget {amount} exceeds cap {MAX_DAILY_BUDGET}"
        )


def check_total(conn, amount: float, exclude_id: int | None = None):
    rows = conn.execute(
        "SELECT id, payload FROM proposals WHERE kind = 'ad_draft' AND status = 'approved'"
    ).fetchall()
    spent = sum(float(json.loads(r["payload"]).get("daily_budget") or 0) for r in rows if r["id"] != exclude_id)
    if spent + amount > MAX_TOTAL_DAILY_BUDGET:
        raise HTTPException(
            400,
            f"Approved ads already use Rs {spent:g}/day. Adding Rs {amount:g} would pass the total cap of Rs {MAX_TOTAL_DAILY_BUDGET:g}/day.",
        )