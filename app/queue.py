import json
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import get_conn
from app.audit import log
from app.budget import check_budget, check_total
from app.products import create_product
from app.meta_draft import build_meta_draft

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter()

FRAMES_DIR = Path("data/frames")
PHOTOS_DIR = Path("data/photos")


def _num(v) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and n >= 0 and n != float("inf") else None


def _save_photo(frame_path, product_id: int) -> str | None:
    if not isinstance(frame_path, str) or not frame_path:
        return None
    try:
        src = Path(frame_path).resolve()
        if not src.is_file() or FRAMES_DIR.resolve() not in src.parents:
            return None
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, PHOTOS_DIR / f"{product_id}.jpg")
        return f"/photos/{product_id}.jpg"
    except OSError:
        return None


class ProposalIn(BaseModel):
    kind: str
    payload: dict


@router.post("/proposals")
def create_proposal(p: ProposalIn):
    if p.kind == "ad_draft":
        check_budget(p.payload)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO proposals (kind, payload) VALUES (?, ?)",
            (p.kind, json.dumps(p.payload)),
        )
        pid = cur.lastrowid
    log("ai", "proposal_created", f"id={pid} kind={p.kind}")
    return {"id": pid, "status": "pending"}


@router.get("/proposals")
def list_proposals(status: str = "pending"):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


def _decide(pid: int, status: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, kind, payload FROM proposals WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        if status == "approved" and row["kind"] == "ad_draft":
            ad = json.loads(row["payload"])
            check_budget(ad)
            check_total(conn, float(ad.get("daily_budget") or 0), exclude_id=pid)
            variants = ad.get("variants", [])
            if not variants:
                raise HTTPException(400, "This ad has no copy left. Add or regenerate it.")
            if any(v.get("blocked") for v in variants):
                raise HTTPException(400, "A line breaks the ad rules. Edit or remove it first.")
            open_lines = [i + 1 for i, v in enumerate(variants)
                          if any(f.startswith("unsupported claim") for f in v.get("flags", []))
                          and not v.get("confirmed")]
            if open_lines:
                raise HTTPException(
                    400, f"Lines {open_lines} contain claims we cannot verify. Edit them, remove them, or confirm they are true."
                )
        if status == "approved" and row["kind"] == "ad_series":
            from app.series import gate_error
            why = gate_error(json.loads(row["payload"]))
            if why:
                raise HTTPException(400, why)
        if status == "approved" and row["kind"] == "landing_page":
            from app.landing import gate_error as landing_gate_error
            why = landing_gate_error(json.loads(row["payload"]))
            if why:
                raise HTTPException(400, why)
        if row["status"] != "pending":
            raise HTTPException(400, f"Already {row['status']}")
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, pid),
        )
        if status == "approved" and row["kind"] == "ad_draft":
            ad["meta_draft"] = build_meta_draft(ad)       # what would be sent, kept as a PAUSED draft
            conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(ad), pid))
        if status == "approved" and row["kind"] == "catalog_draft":
            items = json.loads(row["payload"]).get("items", [])
            added = skipped = 0
            for it in items:
                name = (it.get("name") or "").strip() if isinstance(it.get("name"), str) else ""
                if not name:
                    continue
                brand = (it.get("brand") or "").strip() if isinstance(it.get("brand"), str) else ""
                if conn.execute(
                    "SELECT 1 FROM products WHERE lower(name) = lower(?) AND lower(COALESCE(brand, '')) = lower(?) AND price IS ?",
                    (name, brand, _num(it.get("price"))),
                ).fetchone():
                    skipped += 1
                    continue
                colour = (it.get("colour") or "").strip() if isinstance(it.get("colour"), str) else ""
                raw_attrs = it.get("attrs") if isinstance(it.get("attrs"), dict) else {}
                attrs = {str(k).strip(): str(v).strip() for k, v in raw_attrs.items()
                         if str(k).strip() and v is not None and str(v).strip()}
                if colour:
                    attrs["colour"] = colour
                description = it.get("description").strip() if isinstance(it.get("description"), str) else ""
                new_id = create_product(
                    conn, name, brand=brand or None, category=it.get("category"),
                    description=description or None,
                    price=_num(it.get("price")), cost=_num(it.get("cost")),
                    stock=int(_num(it.get("stock")) or 0),
                    attrs=attrs or None,
                )
                photo = _save_photo(it.get("thumb") or it.get("best_frame"), new_id)
                if photo:
                    conn.execute("UPDATE products SET photo = ? WHERE id = ?", (photo, new_id))
                added += 1
    log("owner", f"proposal_{status}", f"id={pid}")
    if status == "approved" and row["kind"] == "catalog_draft":
        log("owner", "catalog_imported", f"proposal={pid} products={added} skipped={skipped}")
        return {"id": pid, "status": status, "added": added, "skipped": skipped}
    if status == "approved" and row["kind"] == "ad_draft":
        log("ai", "meta_draft_saved", f"id={pid} PAUSED, not sent to Meta")
        return {"id": pid, "status": status, "meta_draft": "saved as a paused draft, not sent to Meta"}
    return {"id": pid, "status": status}


@router.post("/proposals/{pid}/approve", dependencies=[Depends(require_off)])
def approve(pid: int):
    return _decide(pid, "approved")


@router.post("/proposals/{pid}/reject")
def reject(pid: int):
    return _decide(pid, "rejected")


@router.get("/audit")
def get_audit(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]