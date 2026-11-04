"""Report built only from what is in the database. Nothing here is estimated."""
import json
import os
import re
from fastapi import APIRouter
from app.db import get_conn
from app.calllog import unit_prices
from app.budget import MAX_DAILY_BUDGET, MAX_TOTAL_DAILY_BUDGET

router = APIRouter(tags=["report"])

CHECKED_FIELDS = ("name", "brand", "category", "price")     # what the model read from the tag


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{float(v):g}"
    s = str(v).strip()
    try:
        return f"{float(s):g}"
    except ValueError:
        return s.lower()


def _key(it: dict):
    return (it.get("best_frame"), it.get("best_x"))


def corrections(original: list[dict], final: list[dict]) -> dict:
    """How much the owner had to fix in the draft the model produced."""
    finals = {_key(it): it for it in final}
    removed = changed = 0
    for o in original:
        f = finals.get(_key(o))
        if f is None:
            removed += 1
            continue
        changed += sum(1 for k in CHECKED_FIELDS if _norm(o.get(k)) != _norm(f.get(k)))
    kept = len(original) - removed
    return {"items": len(original), "removed": removed, "fields_changed": changed,
            "fields_total": kept * len(CHECKED_FIELDS)}


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def _since(days: int) -> str:
    return f"-{max(1, min(int(days), 365))} days"


@router.get("/report")
def report(days: int = 7):
    win = _since(days)
    prices = unit_prices()
    with get_conn() as conn:
        # products now
        prods = conn.execute(
            "SELECT p.price, p.cost, COALESCE(SUM(v.stock), 0) AS stock FROM products p "
            "LEFT JOIN variants v ON v.product_id = p.id GROUP BY p.id").fetchall()
        products = {
            "total": len(prods),
            "ready_for_planner": sum(1 for r in prods if (r["price"] or r["cost"]) and r["stock"] > 0),
            "missing_price": sum(1 for r in prods if not r["price"]),
            "missing_cost": sum(1 for r in prods if r["cost"] is None),
        }

        # proposals in the window
        props = conn.execute(
            "SELECT id, kind, status, payload FROM proposals WHERE created_at >= datetime('now', ?)", (win,)).fetchall()
        by = lambda kind: [r for r in props if r["kind"] == kind]
        count = lambda rows, st: sum(1 for r in rows if r["status"] == st)

        drafts = by("catalog_draft")
        imported = skipped = 0
        for r in conn.execute("SELECT detail FROM audit_log WHERE action = 'catalog_imported' AND ts >= datetime('now', ?)", (win,)):
            m = re.search(r"products=(\d+)(?: skipped=(\d+))?", r["detail"] or "")
            if m:
                imported += int(m.group(1)); skipped += int(m.group(2) or 0)
        fixes = {"items": 0, "removed": 0, "fields_changed": 0, "fields_total": 0, "drafts_measured": 0}
        for r in drafts:
            if r["status"] != "approved":
                continue
            pl = json.loads(r["payload"])
            if isinstance(pl.get("original"), list):
                c = corrections(pl["original"], pl.get("items", []))
                for k in ("items", "removed", "fields_changed", "fields_total"):
                    fixes[k] += c[k]
                fixes["drafts_measured"] += 1
        catalog = {
            "drafts": len(drafts), "approved": count(drafts, "approved"), "rejected": count(drafts, "rejected"),
            "pending": count(drafts, "pending"), "products_imported": imported, "already_in_catalog": skipped,
            "owner_corrections": fixes,
        }

        ads_rows = by("ad_draft")
        lines = flagged = blocked = meta_drafts = 0
        sources: dict[str, int] = {}
        for r in ads_rows:
            pl = json.loads(r["payload"])
            src = (pl.get("image_source") or "none").split(" (")[0]
            sources[src] = sources.get(src, 0) + 1
            if r["status"] == "approved" and pl.get("meta_draft"):
                meta_drafts += 1
            for v in pl.get("variants", []):
                lines += 1
                flagged += 1 if v.get("flags") else 0
                blocked += 1 if v.get("blocked") else 0
        ads = {"drafts": len(ads_rows), "approved": count(ads_rows, "approved"), "rejected": count(ads_rows, "rejected"),
               "pending": count(ads_rows, "pending"), "copy_lines": lines, "lines_flagged": flagged,
               "lines_blocked": blocked, "image_sources": sources, "meta_drafts": meta_drafts}

        # standing commitment: every ad that is approved right now
        approved = conn.execute("SELECT payload FROM proposals WHERE kind = 'ad_draft' AND status = 'approved'").fetchall()
        committed = sum(float(json.loads(r["payload"]).get("daily_budget") or 0) for r in approved)
        spend = {"approved_ads": len(approved), "committed_per_day": committed,
                 "cap_per_ad": MAX_DAILY_BUDGET, "cap_total_per_day": MAX_TOTAL_DAILY_BUDGET,
                 "note": "Budget committed in the approval queue. Nothing is sent to Meta, so this is not real ad spend."}

        planner = {"runs": len(by("promotion"))}

        # model calls in the window
        calls = conn.execute(
            "SELECT kind, purpose, seconds, ok, prompt_tokens, completion_tokens FROM model_calls "
            "WHERE ts >= datetime('now', ?)", (win,)).fetchall() if _has_table(conn, "model_calls") else []
        groups: dict[str, list] = {}
        for c in calls:
            groups.setdefault(c["purpose"] or "other", []).append(c)
        def summarise(rows):
            secs = [r["seconds"] for r in rows if r["seconds"] is not None]
            cost = sum(prices.get(r["kind"], 0.0) for r in rows)
            return {"calls": len(rows), "failed": sum(1 for r in rows if not r["ok"]),
                    "avg_seconds": round(sum(secs) / len(secs), 2) if secs else None,
                    "p95_seconds": round(_p95(secs), 2) if secs else None,
                    "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
                    "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
                    "estimated_cost": round(cost, 4)}
        model_calls = {"total": summarise(calls),
                       "by_purpose": {k: summarise(v) for k, v in sorted(groups.items())},
                       "unit_prices_set": any(prices.values())}

        seo = {}
        if _has_table(conn, "seo_proposals"):
            seo = {x["status"]: x["n"] for x in conn.execute(
                "SELECT status, COUNT(*) AS n FROM seo_proposals GROUP BY status")}

        actions = conn.execute(
            "SELECT action, COUNT(*) AS n FROM audit_log WHERE ts >= datetime('now', ?) GROUP BY action ORDER BY n DESC",
            (win,)).fetchall()

    from app.killswitch import state as _ks
    return {"kill_switch": _ks(), "days": max(1, min(int(days), 365)), "products": products, "catalog": catalog, "ads": ads,
            "spend": spend, "planner": planner, "seo": seo, "model_calls": model_calls,
            "audit_events": {r["action"]: r["n"] for r in actions}}


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())