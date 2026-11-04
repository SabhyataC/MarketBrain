import json
import re
import time
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import get_conn
from app.audit import log
from app.model_client import ask, generate_image, edit_image
from app.catalog import parse_json_list
from app.budget import check_budget
from app.products import customer_price
from app.policy import check_variants, RULES
from app import shop_config
from app.meta_draft import clean_link
from app.calllog import purpose

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter(prefix="/ads", tags=["ads"])

PROMPT = """Write 3 different Facebook/Instagram ad copy variants for this product.
Product: {name}
Brand: {brand}
Price: Rs {price}
Verified facts (the ONLY facts you may state): {facts}
Each variant must use a different angle (for example: everyday use, gifting,
treat yourself). Be warm and specific, mention the product by name.
Do not describe material, taste, scent, ingredients, texture, size, comfort, quality,
durability or any other physical attribute unless it is in the verified facts.
Allowed: lifestyle language about when, where and for whom it is used.
Not allowed: health or medical claims, guarantees, fake discounts or urgency,
invented reviews, statistics, "best" or "#1".
Return ONLY a JSON array, no other text. Each item:
{{"headline": "max 40 characters", "text": "max 125 characters", "cta": "Shop Now"}}"""


class AdIn(BaseModel):
    product_id: int
    daily_budget: float = 200
    use_real_photo: bool = False


class DraftEdit(BaseModel):
    daily_budget: float | None = None
    variants: list[dict] | None = None
    link: str | None = None


def pretty_name(name: str) -> str:
    words = [w for w in re.split(r"\s+", name) if any(c.isalpha() for c in w)]
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and all(len(re.sub(r"[^A-Za-z]", "", w)) > 3 for w in words):
        return name.title()
    return name


def facts_for(p, attrs: dict) -> dict:
    facts = {"name": pretty_name(p["name"]), "brand": p["brand"], "price": customer_price(p["price"], p["cost"])}
    if p["category"]:
        facts["category"] = p["category"]
    if p["description"]:
        facts["description"] = p["description"]
    facts.update({k: v for k, v in attrs.items() if v})
    return facts


@router.post("/generate", dependencies=[Depends(require_off)])
def generate(body: AdIn):
    check_budget({"daily_budget": body.daily_budget})
    with get_conn() as conn:
        p = conn.execute(
            "SELECT id, name, brand, price, cost, category, description, photo, attrs FROM products WHERE id = ?",
            (body.product_id,),
        ).fetchone()
    if not p:
        raise HTTPException(404, "Product not found")
    sell = customer_price(p["price"], p["cost"])
    if sell is None or sell <= 0:
        raise HTTPException(400, "Set a Cost (the price the customer pays) or an MRP for this product before making an ad.")

    attrs = json.loads(p["attrs"] or "{}")
    facts = facts_for(p, attrs)
    name = facts["name"]

    with purpose("ad_copy"):
        raw = ask(PROMPT.format(name=name, brand=p["brand"] or "n/a", price=sell,
                                facts=json.dumps(facts, ensure_ascii=False)))
    variants = parse_json_list(raw)
    variants = [v for v in variants if v.get("headline") and v.get("text")][:3]
    if not variants:
        raise HTTPException(502, "Model returned no usable ad copy, try again")
    variants = check_variants(variants, facts)

    image_url, image_source = None, "none"
    reference = Path("data") / p["photo"].lstrip("/") if p["photo"] else None
    if body.use_real_photo and p["photo"]:
        image_url, image_source = p["photo"], "real photo"
    else:
        colour = (attrs.get("colour") or "").strip()
        subject = name if colour.lower() in name.lower() else f"{colour} {name}".strip()
        business = shop_config.load()["business_type"]
        scene = f"styled in a natural setting that suits a {business}"
        Path("static/ads").mkdir(parents=True, exist_ok=True)
        fname = f"ad_{p['id']}_{int(time.time())}.png"
        out = f"static/ads/{fname}"
        style = (
            f"{scene}, centered and in sharp focus, styled like a premium brand campaign shot. "
            f"Dramatic golden-hour side light with a soft rim light and gentle haze, "
            f"deep shadows, rich contrast and natural colour grading, "
            f"shot on an 85mm lens at f/1.8 with shallow depth of field and creamy background bokeh. "
            f"Moody and aspirational, film-like grain, ultra detailed, photorealistic. "
            f"Do not invent text, logos or labels. No watermark, no people."
        )
        with purpose("ad_image"):
            if reference and reference.is_file():
                try:
                    edit_image(
                        f"Cinematic commercial product photograph. Keep the product in the reference photo exactly the same: "
                        f"same shape, colour, pattern, material and details. Show only that one product ({subject}), as it is normally sold. "
                        f"Remove any price tag, hanger, other items and background clutter from the reference. "
                        f"Place it {style}",
                        str(reference), out,
                    )
                    image_url, image_source = f"/static/ads/{fname}", "generated from your photo"
                except Exception as e:
                    log("ai", "ad_edit_failed", f"product={p['id']} {str(e)[:120]}")
                    image_url, image_source = p["photo"], "real photo (editing failed)"
            else:
                try:
                    generate_image(
                        f"Cinematic commercial product photograph of this product: {subject}, the single hero subject, "
                        f"{style} Show only the product itself, as it is normally sold. "
                        f"Do not add any extra bag, box or gift packaging.",
                        out,
                    )
                    image_url, image_source = f"/static/ads/{fname}", "generated (no photo, so it may not match your product)"
                except Exception as e:
                    log("ai", "ad_image_failed", f"product={p['id']} {str(e)[:120]}")

    payload = {
        "product_id": p["id"],
        "product_name": name,
        "daily_budget": body.daily_budget,
        "variants": variants,
        "facts": facts,
        "image_url": image_url,
        "image_source": image_source,
        "photo": p["photo"],
    }
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO proposals (kind, payload) VALUES (?, ?)",
            ("ad_draft", json.dumps(payload)),
        )
        pid = cur.lastrowid
    log("ai", "ad_draft_created", f"id={pid} product={p['id']} image={image_source}")
    return {"proposal_id": pid, **payload}


@router.put("/draft/{pid}")
def edit_ad_draft(pid: int, body: DraftEdit):
    with get_conn() as conn:
        row = conn.execute("SELECT status, kind, payload FROM proposals WHERE id = ?", (pid,)).fetchone()
        if not row or row["kind"] != "ad_draft":
            raise HTTPException(404, "Ad draft not found")
        if row["status"] != "pending":
            raise HTTPException(400, f"Already {row['status']}")
        payload = json.loads(row["payload"])
        if body.daily_budget is not None:
            check_budget({"daily_budget": body.daily_budget})
            payload["daily_budget"] = body.daily_budget
        if body.link is not None:
            payload["link"] = clean_link(body.link)
        if body.variants is not None:
            old = payload["variants"]
            new, to_check = [], []
            for v in body.variants:
                headline = str(v.get("headline", "")).strip()[:40]
                text = str(v.get("text", "")).strip()[:125]
                if not headline or not text:
                    continue
                orig = v.get("orig")
                before = old[orig] if isinstance(orig, int) and 0 <= orig < len(old) else None
                changed = before is None or (headline, text) != (before.get("headline"), before.get("text"))
                entry = {"headline": headline, "text": text, "cta": v.get("cta") or "Shop Now"}
                if changed:
                    entry["confirmed"] = False
                    to_check.append(entry)
                else:
                    entry["flags"] = list(before.get("flags", []))
                    entry["blocked"] = bool(before.get("blocked"))
                    entry["confirmed"] = bool(v.get("confirmed"))
                new.append(entry)
            if to_check:
                facts = payload.get("facts") or {"name": payload.get("product_name")}
                try:
                    check_variants(to_check, facts)
                except Exception as e:
                    log("ai", "ad_recheck_failed", f"id={pid} {str(e)[:120]}")
                    for entry in to_check:
                        flags = [label for pat, label in RULES if re.search(pat, f"{entry['headline']} {entry['text']}", re.I)]
                        entry["flags"] = flags + ["unsupported claim: could not be checked, read this line carefully"]
                        entry["blocked"] = bool(flags)
            payload["variants"] = new
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(payload), pid))
    log("owner", "ad_draft_edited", f"id={pid}")
    return {"proposal_id": pid, **payload}