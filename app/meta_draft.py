"""A paused Meta ad draft for every approved ad, kept locally.

Nothing here talks to Meta. It records exactly what WOULD be sent (campaign, ad set,
creative), marked PAUSED and "not sent", and lets the owner download an ad pack to
upload by hand in Ads Manager. A real sender can be added later on top of build_meta_draft().
"""
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from app.db import get_conn

router = APIRouter(prefix="/meta", tags=["meta"])

LINK_RE = re.compile(r"^https?://[^\s]+$", re.I)
PLACEMENTS = ["facebook_feed", "instagram_feed"]


def clean_link(value) -> str | None:
    """Landing page link for the ad: empty means none; anything else must be an http(s) address."""
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) > 300 or not LINK_RE.match(s):
        raise HTTPException(400, "The landing link must start with http:// or https:// and have no spaces.")
    return s


def build_meta_draft(ad: dict) -> dict:
    lines = [{"headline": v.get("headline"), "text": v.get("text"), "cta": v.get("cta") or "Shop Now"}
             for v in ad.get("variants", [])]
    name = ad.get("product_name") or "Product"
    missing = []
    if not ad.get("link"):
        missing.append("landing link: add one before sending")
    if not ad.get("image_url"):
        missing.append("image")
    return {
        "status": "PAUSED",
        "sent_to_meta": False,
        "note": "Draft only. Nothing was sent to Meta.",
        "campaign": {"name": f"Marketing Brain | {name}", "objective": "OUTCOME_TRAFFIC", "status": "PAUSED"},
        "ad_set": {"name": f"{name} | daily", "daily_budget": ad.get("daily_budget"),
                   "placements": PLACEMENTS, "status": "PAUSED"},
        "creative": {"image": ad.get("image_url"), "image_source": ad.get("image_source"),
                     "link": ad.get("link"), "lines": lines},
        "missing": missing,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _money(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _image_file(image_url) -> Path | None:
    """Only files the app itself made: generated ads or saved product photos."""
    if not isinstance(image_url, str):
        return None
    for prefix, folder in (("/static/ads/", "static/ads"), ("/photos/", "data/photos")):
        if image_url.startswith(prefix):
            p = Path(folder) / Path(image_url).name
            return p if p.is_file() else None
    return None


def _approved_ads(conn):
    rows = conn.execute(
        "SELECT id, payload, decided_at FROM proposals WHERE kind = 'ad_draft' AND status = 'approved' ORDER BY id DESC"
    ).fetchall()
    out = []
    for r in rows:
        ad = json.loads(r["payload"])
        if ad.get("meta_draft"):
            out.append((r["id"], r["decided_at"], ad))
    return out


@router.get("/drafts")
def list_drafts():
    with get_conn() as conn:
        items = _approved_ads(conn)
    return [{"proposal_id": pid, "approved_at": at, "product_name": ad.get("product_name"),
             "daily_budget": ad.get("daily_budget"), "image_url": ad.get("image_url"),
             "link": ad.get("link"), "meta_draft": ad["meta_draft"]} for pid, at, ad in items]


@router.get("/drafts/{pid}/pack")
def ad_pack(pid: int):
    with get_conn() as conn:
        row = conn.execute("SELECT payload, status, kind FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not row or row["kind"] != "ad_draft" or row["status"] != "approved":
        raise HTTPException(404, "No approved ad with this number")
    ad = json.loads(row["payload"])
    draft = ad.get("meta_draft")
    if not draft:
        raise HTTPException(404, "This ad has no Meta draft")
    img = _image_file(ad.get("image_url"))
    text = [f"Ad for {ad.get('product_name')}", "",
            "DRAFT ONLY. This was not sent to Meta. Upload it yourself in Ads Manager.", "",
            f"Daily budget: Rs {_money(ad.get('daily_budget'))}",
            f"Landing link: {ad.get('link') or '(none yet, add one)'}",
            f"Image: {img.name if img else '(none)'}  [{ad.get('image_source') or 'no source'}]", ""]
    for i, v in enumerate(ad.get("variants", []), 1):
        text += [f"Option {i}", f"  Headline: {v.get('headline')}", f"  Text: {v.get('text')}",
                 f"  Button: {v.get('cta') or 'Shop Now'}", ""]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("copy.txt", "\n".join(text))
        z.writestr("meta_draft.json", json.dumps(draft, indent=2, ensure_ascii=False))
        if img:
            z.write(img, f"image{img.suffix.lower()}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="ad_pack_{pid}.zip"'})
