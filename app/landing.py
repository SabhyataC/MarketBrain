import html as htmllib
import io
import json
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from PIL import Image
from app import shop_config
from app.ads import facts_for, pretty_name
from app.audit import log
from app.calllog import purpose
from app.db import get_conn
from app.export_csv import load_catalog
from app.meta_draft import clean_link
from app.model_client import ask, edit_image
from app.policy import RULES, check_variants
from app.products import customer_price
from app.seo import build_json_ld, currency_code

try:
    from app.killswitch import require_off
    GUARD = [Depends(require_off)]
except ImportError:
    GUARD = []

router = APIRouter(prefix="/landing", tags=["landing"])

LANDING_DIR = Path("data/landing")
PHOTOS_DIR = Path("data/photos")
VIEWPORTS = {"phone": (390, 844), "desktop": (1280, 800)}
PAGE_TIMEOUT_MS = 20000
LIMITS = {"headline": 60, "subheading": 120, "bullet": 80, "description": 500, "cta": 20}
MIN_BULLETS = 3
MAX_BULLETS = 5
MIN_CONTRAST = 4.5
MIN_TAP = 44
LAYOUT_CHECKS = ("hscroll", "image", "above_fold", "overlap")
PACK_FILES = ("index.html", "photo.jpg", "phone.png", "desktop.png", "checks.json")
INSTALL_MSG = ("The page check needs a browser, and it is not installed here. Run: pip install playwright, "
               "then: playwright install chromium. Then try again.")
PLACEHOLDER = "Order on WhatsApp / shop"

CHECK_LABELS = {
    "load": "Page loads with no errors",
    "hscroll": "No sideways scrolling",
    "image": "Product photo loads and is visible at the top",
    "above_fold": "Headline, price and button are visible without scrolling",
    "contrast": "Text is easy to read (contrast at least 4.5 to 1)",
    "tap": "Button is big enough to tap (at least 44 by 44 px)",
    "overlap": "Nothing covers the price or the button",
}

FORBIDDEN = re.compile(
    r"(?:rs\.?|₹|inr|\$|usd)\s*\d|\b\d[\d,.]*\s*(?:rs|rupees)\b|"
    r"\b(delivery|deliveries|shipping|refunds?|warranty|cash on delivery|reviews?|testimonials?|"
    r"bestsellers?|best-selling|free returns?|easy returns?|return policy|\d(?:\.\d)?\s*stars?)\b", re.I)

PROMPT = """Write the text for a single-page product landing page for ONE product sold by a {business_type}.
Verified facts (the ONLY facts you may state): {facts}
Return ONLY a JSON object, no other text:
{{"headline": "max 60 characters", "subheading": "max 120 characters", "bullets": ["3 to 5 short points, each max 80 characters"], "description": "max 500 characters, plain sentences", "cta": "button text, max 20 characters"}}
Rules:
- Use only the verified facts. Do not invent features, materials, sizes, reviews, ratings, prices, discounts, delivery, shipping, returns or guarantees.
- Do not mention any price or offer. The page shows the price itself.
- Do not describe material, scent, taste, texture, comfort, quality or durability unless it is in the verified facts.
- Lifestyle language about who the product is for and when to use it is allowed.
- Plain words, no emojis, no all-caps, no HTML.
{fix}"""

BASE_CSS = """
:root{--ink:#1a1a1a;--muted:#4b5563;--line:#e5e7eb;--accent:#1d4ed8}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:12px 16px 40px}
.brand{margin:0 0 8px;font-size:.8rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.hero{margin:0;background:#f3f4f6;border-radius:14px;overflow:hidden}
.hero-img{display:block;width:100%;height:auto;max-height:34vh;object-fit:contain}
h1{margin:16px 0 6px;font-size:1.65rem;line-height:1.2}
.sub{margin:0 0 12px;color:#374151;font-size:1.02rem}
.price-row{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px;margin:0 0 12px}
.price{font-size:1.6rem;font-weight:700}
.mrp{font-size:1rem;color:#6b7280;text-decoration:line-through}
.buy{display:flex;align-items:center;justify-content:center;min-height:52px;padding:0 24px;border-radius:12px;background:var(--accent);color:#fff;font-weight:600;font-size:1.05rem;text-decoration:none}
.buy.off{background:#4b5563;border:2px dashed #d1d5db;cursor:not-allowed}
.points{margin:22px 0 0;padding-left:1.2rem}
.points li{margin:6px 0}
.desc{margin:18px 0 0}
.facts{margin:22px 0 0;border-top:1px solid var(--line)}
.facts div{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.facts dt{width:110px;flex:none;color:var(--muted)}
.facts dd{margin:0;min-width:0}
@media (min-width:800px){
.wrap{padding-top:28px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:40px;align-items:start}
.hero-img{max-height:70vh}
h1{margin-top:0;font-size:2.1rem}
}
"""

REPAIR_CSS = """
*{overflow-wrap:anywhere;word-break:break-word}
h1{font-size:1.3rem}
.sub{font-size:.95rem}
.hero-img{max-height:24vh}
.wrap{padding-top:8px}
"""

MEASURE_JS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const q = s => document.querySelector(s);
  const nums = c => { const m = String(c).match(/[\\d.]+/g); return m ? m.map(Number) : null; };
  const parse = c => { const n = String(c).startsWith("rgb") ? nums(c) : null; return n && n.length >= 3 ? {r: n[0], g: n[1], b: n[2], a: n.length > 3 ? n[3] : 1} : null; };
  const mix = (f, b) => ({r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1});
  const bgOf = el => {
    const stack = [];
    for (let n = el; n; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a >= 1) break; }
    }
    let base = {r: 255, g: 255, b: 255, a: 1};
    for (let i = stack.length - 1; i >= 0; i--) base = mix(stack[i], base);
    return base;
  };
  const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const shown = el => {
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return cs.display !== "none" && cs.visibility !== "hidden" && parseFloat(cs.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  const info = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height, shown: shown(el)};
  };
  const img = q(".hero-img"), h1 = q("h1"), price = q(".price"), buy = q(".buy"), priceRow = q(".price-row") || price;
  const out = {vw, vh, scrollW: document.documentElement.scrollWidth, bodyW: document.body ? document.body.scrollWidth : 0};
  out.img = img ? Object.assign(info(img), {complete: img.complete, nw: img.naturalWidth}) : null;
  out.h1 = info(h1); out.price = info(price); out.buy = info(buy);
  out.contrast = [];
  for (const sel of ["h1", ".sub", ".price", ".mrp", ".points li", ".desc", ".facts dt", ".facts dd", ".brand", ".buy"]) {
    for (const el of document.querySelectorAll(sel)) {
      if (!shown(el)) continue;
      const cs = getComputedStyle(el), fg = parse(cs.color);
      if (!fg) { out.contrast.push({sel, ratio: null}); continue; }
      const bg = bgOf(el);
      out.contrast.push({sel, ratio: ratio(mix(fg, bg), bg), text: (el.textContent || "").trim().slice(0, 30)});
    }
  }
  const related = (a, b) => a === b || a.contains(b) || b.contains(a);
  const painted = el => {
    if (["IMG", "SVG", "BUTTON", "INPUT", "VIDEO", "CANVAS"].includes(el.tagName.toUpperCase())) return true;
    for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) return true;
    const c = parse(getComputedStyle(el).backgroundColor);
    return !!(c && c.a > 0);
  };
  out.overlaps = [];
  for (const [name, target] of [["price", priceRow], ["button", buy]]) {
    if (!target) continue;
    const t = target.getBoundingClientRect();
    for (const el of document.body.querySelectorAll("*")) {
      if (related(el, target) || !shown(el) || !painted(el)) continue;
      const r = el.getBoundingClientRect();
      const w = Math.min(t.right, r.right) - Math.max(t.left, r.left), h = Math.min(t.bottom, r.bottom) - Math.max(t.top, r.top);
      if (w > 1 && h > 1) out.overlaps.push({target: name, tag: el.tagName.toLowerCase(), text: (el.textContent || "").trim().slice(0, 30)});
    }
  }
  out.hits = [];
  for (const [name, target] of [["price", price], ["button", buy]]) {
    if (!target) continue;
    target.scrollIntoView({block: "center"});
    const r = target.getBoundingClientRect(), top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    out.hits.push({target: name, ok: !!top && (top === target || target.contains(top))});
  }
  window.scrollTo(0, 0);
  return out;
}
"""


def _json_obj(text):
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        v = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def _clean(v):
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else ""


def lines_of(c):
    out = [("headline", "Headline", c["headline"]), ("subheading", "Subheading", c["subheading"])]
    out += [(f"bullet_{i}", f"Point {i}", b) for i, b in enumerate(c["bullets"], 1)]
    out += [("description", "Description", c["description"]), ("cta", "Button text", c["cta"])]
    return out


def content_problems(c, strict):
    if not isinstance(c, dict):
        return ["the answer must be a JSON object with headline, subheading, bullets, description and cta"]
    bad = []
    for key in ("headline", "subheading", "description", "cta"):
        v = c.get(key)
        if not isinstance(v, str) or not _clean(v):
            bad.append(f"{key} is missing or empty")
        elif len(_clean(v)) > LIMITS[key]:
            bad.append(f"{key} is {len(_clean(v))} characters; the limit is {LIMITS[key]}")
    b = c.get("bullets")
    if not isinstance(b, list) or not all(isinstance(x, str) for x in b):
        bad.append("bullets must be a list of 3 to 5 short texts")
    else:
        if not MIN_BULLETS <= len(b) <= MAX_BULLETS:
            bad.append(f"bullets has {len(b)} items; it needs 3 to 5")
        for i, x in enumerate(b, 1):
            if not _clean(x):
                bad.append(f"bullet {i} is empty")
            elif len(_clean(x)) > LIMITS["bullet"]:
                bad.append(f"bullet {i} is {len(_clean(x))} characters; the limit is {LIMITS['bullet']}")
    if strict and not bad:
        for key, label, text in lines_of(clean_content(c)):
            m = FORBIDDEN.search(text)
            if m:
                bad.append(f"{label} mentions '{m.group(0)}', which is not allowed (no prices, delivery, returns, reviews or guarantees)")
    return bad


def clean_content(c):
    return {"headline": _clean(c.get("headline")), "subheading": _clean(c.get("subheading")),
            "bullets": [_clean(x) for x in (c.get("bullets") or []) if _clean(x)],
            "description": _clean(c.get("description")), "cta": _clean(c.get("cta"))}


def write_content(business, facts):
    fix = ""
    last = ["no usable answer"]
    for _ in range(2):
        try:
            with purpose("landing_copy"):
                raw = ask(PROMPT.format(business_type=business, facts=json.dumps(facts, ensure_ascii=False), fix=fix))
        except HTTPException:
            raise
        except Exception as e:
            log("ai", "landing_copy_failed", str(e)[:120])
            raise HTTPException(502, "The model could not write the page text. " + str(e)[:120])
        obj = _json_obj(raw)
        last = content_problems(obj, True) if obj is not None else ["the answer was not valid JSON"]
        if not last:
            return clean_content(obj)
        fix = "Your previous answer had these problems, fix all of them: " + "; ".join(last)
    raise HTTPException(502, "The model did not return usable page text (" + "; ".join(last[:2]) + "). Try again.")


def policy_lines(lines, facts):
    items = [{"headline": t, "text": ""} for _, _, t in lines]
    try:
        check_variants(items, facts)
    except Exception as e:
        log("ai", "landing_check_failed", str(e)[:120])
        for it in items:
            flags = [label for pat, label in RULES if re.search(pat, it["headline"], re.I)]
            it["flags"] = flags + ["unsupported claim: could not be checked, read this line carefully"]
            it["blocked"] = bool(flags)
    return [{"key": k, "label": lab, "text": t, "flags": it["flags"], "blocked": it["blocked"], "confirmed": False}
            for (k, lab, t), it in zip(lines, items)]


def _product(pid):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Product not found")
    return p


def _photo_file(p):
    url = p["photo"]
    if isinstance(url, str) and url.startswith("/photos/"):
        f = PHOTOS_DIR / Path(url).name
        return f if f.is_file() else None
    return None


def _entry(pid):
    for e in load_catalog():
        if e["product"]["id"] == pid:
            return e
    raise HTTPException(404, "Product not found")


def variant_prices(entry):
    out = []
    for _, v in entry["variants"]:
        sell = customer_price(v["price"], v["cost"])
        if sell is not None and sell > 0:
            mrp = v["price"] if v["price"] and v["price"] > 0 else None
            out.append((sell, mrp, v["stock"]))
    return out


def colours_of(entry, attrs):
    found = []
    for _, v in entry["variants"]:
        c = v["options"].get("colour") or v["options"].get("color")
        if c and c not in found:
            found.append(c)
    if not found and (attrs.get("colour") or attrs.get("color")):
        found.append(attrs.get("colour") or attrs.get("color"))
    return found


def model_facts(p, entry, attrs):
    facts = facts_for(p, attrs)
    facts.pop("price", None)
    cols = colours_of(entry, attrs)
    if cols:
        facts["colours"] = cols
    facts["in_stock"] = sum(v["stock"] for _, v in entry["variants"]) > 0
    return facts


def build_page_data(p, entry, attrs, cfg):
    prices = variant_prices(entry)
    sell, mrp, _ = min(prices, key=lambda x: x[0])
    varies = len({x[0] for x in prices}) > 1
    cur = cfg["currency"]
    name = pretty_name(p["name"])
    cols = colours_of(entry, attrs)
    details = []
    if p["brand"]:
        details.append(["Brand", p["brand"]])
    if p["category"]:
        details.append(["Category", p["category"]])
    for k, v in attrs.items():
        if v and k.lower() not in ("colour", "color"):
            details.append([str(k).replace("_", " ").capitalize(), str(v)])
    if cols:
        details.append(["Colours", ", ".join(cols)])
    in_stock = sum(v["stock"] for _, v in entry["variants"]) > 0
    details.append(["Availability", "In stock" if in_stock else "Out of stock"])
    alt = f"Image of {name}" + (f" by {p['brand']}" if p["brand"] else "") + (f" in {cols[0]}" if len(cols) == 1 else "")
    ld = build_json_ld(entry, p["description"] or "", currency_code(cur), _image_base(), "")
    json_ld = ""
    if ld:
        data = json.loads(ld)
        if not data.get("description"):
            data.pop("description", None)
        json_ld = json.dumps(data, ensure_ascii=False, indent=2)
    return {"title": name + (f" | {p['brand']}" if p["brand"] else ""), "brand": p["brand"] or "", "alt": alt,
            "details": details, "price_text": ("From " if varies else "") + f"{cur} {sell:g}",
            "mrp_text": "" if varies or not mrp or mrp <= sell else f"{cur} {mrp:g}",
            "in_stock": in_stock, "json_ld": json_ld, "photo_w": 0, "photo_h": 0}


def _image_base():
    import os
    return os.environ.get("SYNC_IMAGE_BASE", "").strip().rstrip("/")


def image_prompt(name, business):
    return ("Cinematic commercial product photograph for a web page. Keep the product in the reference photo exactly the same: "
            "same shape, colour, pattern, material and details. "
            f"Show only that one product ({name}), as it is normally sold. "
            "Remove any price tag, hanger, other items and clutter from the reference. "
            f"Place it centred in a clean, softly lit setting that suits a {business}, with soft natural light, gentle shadows "
            "and a shallow depth of field. Do not add any text, logos, watermarks or people.")


def prepare_photo(src, dest):
    try:
        img = Image.open(src)
        img.load()
        img = img.convert("RGB")
    except Exception:
        raise HTTPException(400, "The product photo could not be read. Upload it again and retry.")
    img.thumbnail((1200, 1200))
    img.save(dest, "JPEG", quality=88)
    return img.width, img.height


def render_html(content, page, link, compact=False):
    e = htmllib.escape
    desc = e(content["subheading"])
    bullets = "".join(f"<li>{e(b)}</li>" for b in content["bullets"])
    facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in page["details"])
    mrp = f'<s class="mrp">{e(page["mrp_text"])}</s>' if page["mrp_text"] else ""
    if link:
        buy = f'<a class="buy" href="{e(link)}" rel="noopener">{e(content["cta"])}</a>'
    else:
        buy = f'<span class="buy off" role="button" aria-disabled="true">{PLACEHOLDER}</span>'
    ld = ""
    if page.get("json_ld"):
        ld = '<script type="application/ld+json">' + page["json_ld"].replace("<", "\\u003c") + "</script>"
    brand = f'<p class="brand">{e(page["brand"])}</p>' if page["brand"] else ""
    size = f' width="{page["photo_w"]}" height="{page["photo_h"]}"' if page.get("photo_w") else ""
    css = BASE_CSS + (REPAIR_CSS if compact else "")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{e(page['title'])}</title>\n"
        f'<meta name="description" content="{desc}">\n'
        f"<style>{css}</style>\n{ld}\n</head>\n<body>\n<div class=\"wrap\">\n{brand}\n<div class=\"cols\">\n"
        f'<figure class="hero"><img class="hero-img" src="photo.jpg" alt="{e(page["alt"])}"{size}></figure>\n'
        f'<div class="info">\n<h1>{e(content["headline"])}</h1>\n<p class="sub">{e(content["subheading"])}</p>\n'
        f'<div class="price-row"><span class="price">{e(page["price_text"])}</span>{mrp}</div>\n{buy}\n'
        f'<ul class="points">{bullets}</ul>\n<p class="desc">{e(content["description"])}</p>\n'
        f'<dl class="facts">{facts}</dl>\n</div>\n</div>\n</div>\n</body>\n</html>\n')


def interpret(vp, m, problems):
    out = []

    def add(id_, ok, msg):
        out.append({"id": id_, "label": CHECK_LABELS[id_], "viewport": vp, "pass": bool(ok), "message": msg})

    add("load", not problems, "No errors, and nothing failed to load." if not problems
        else "Problems while loading: " + "; ".join(problems[:2]))
    wide = max(m["scrollW"], m["bodyW"])
    add("hscroll", wide <= m["vw"], f"The page is {wide:g} px wide in a {m['vw']} px screen"
        + ("." if wide <= m["vw"] else ", so it scrolls sideways."))
    img = m["img"]
    if not img:
        add("image", False, "No product photo was found on the page.")
    elif not img["complete"] or img["nw"] <= 0:
        add("image", False, "The product photo did not load.")
    else:
        seen = max(0.0, min(img["bottom"], m["vh"]) - max(img["top"], 0))
        ok = img["shown"] and img["top"] < m["vh"] and seen >= 0.5 * img["height"]
        add("image", ok, f"The photo loaded ({img['nw']} px wide) and {round(100 * seen / max(img['height'], 1))}% of it is visible at the top."
            if ok else f"The photo loaded but only {round(100 * seen / max(img['height'], 1))}% of it is visible without scrolling.")
    missing = [n for n, k in (("headline", m["h1"]), ("price", m["price"]), ("button", m["buy"])) if not k or not k["shown"]]
    if missing:
        add("above_fold", False, "Not found or hidden: " + ", ".join(missing) + ".")
    else:
        low = [(n, k) for n, k in (("headline", m["h1"]), ("price", m["price"]), ("button", m["buy"]))
               if k["top"] < 0 or k["bottom"] > m["vh"]]
        if low:
            n, k = max(low, key=lambda x: x[1]["bottom"])
            add("above_fold", False, f"The {n} ends at {k['bottom']:.0f} px but the screen is {m['vh']} px tall, so you must scroll.")
        else:
            end = max(m["h1"]["bottom"], m["price"]["bottom"], m["buy"]["bottom"])
            add("above_fold", True, f"Headline, price and button all end by {end:.0f} px of a {m['vh']} px screen.")
    samples = m["contrast"]
    if not samples or not m["buy"]:
        add("contrast", False, "Could not find the text and the button to measure.")
    elif any(s["ratio"] is None for s in samples):
        add("contrast", False, "Some text colours could not be read, so the contrast was not measured.")
    else:
        worst = min(samples, key=lambda s: s["ratio"])
        add("contrast", worst["ratio"] >= MIN_CONTRAST,
            f"Lowest contrast is {worst['ratio']:.1f} to 1 ({worst['sel']}: \"{worst.get('text', '')}\"); the minimum is {MIN_CONTRAST:g}.")
    if not m["buy"]:
        add("tap", False, "No button was found on the page.")
    else:
        b = m["buy"]
        add("tap", b["width"] >= MIN_TAP and b["height"] >= MIN_TAP,
            f"The button is {b['width']:.0f} by {b['height']:.0f} px; it needs at least {MIN_TAP} by {MIN_TAP}.")
    bad = [f"{o['target']} is covered by {o['tag']} \"{o['text']}\"" for o in m["overlaps"]]
    bad += [f"the {h['target']} cannot be reached because something sits on top of it" for h in m["hits"] if not h["ok"]]
    add("overlap", not bad and bool(m["hits"]), "Nothing overlaps the price or the button." if not bad and m["hits"]
        else ("; ".join(bad[:2]) if bad else "The price and the button were not found."))
    return out


def run_checks(html_path, out_dir):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise HTTPException(400, INSTALL_MSG)
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for vp, (w, h) in VIEWPORTS.items():
                    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
                    try:
                        page = ctx.new_page()
                        page.set_default_timeout(PAGE_TIMEOUT_MS)
                        problems = []
                        page.on("console", lambda msg, pr=problems: pr.append("console error: " + msg.text[:80]) if msg.type == "error" else None)
                        page.on("pageerror", lambda err, pr=problems: pr.append("script error: " + str(err)[:80]))
                        page.on("requestfailed", lambda req, pr=problems: pr.append("failed to load " + req.url.rsplit("/", 1)[-1][:60]))
                        page.goto(Path(html_path).resolve().as_uri(), wait_until="load")
                        page.wait_for_function("[...document.images].every(i => i.complete)")
                        m = page.evaluate(MEASURE_JS)
                        page.screenshot(path=str(Path(out_dir) / f"{vp}.png"), full_page=True)
                        results += interpret(vp, m, problems)
                    finally:
                        ctx.close()
            finally:
                browser.close()
    except HTTPException:
        raise
    except PlaywrightTimeout:
        raise HTTPException(502, "The page check took too long. Try again.")
    except PlaywrightError as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise HTTPException(400, INSTALL_MSG)
        raise HTTPException(500, "The page check could not run: " + msg.splitlines()[0][:150])
    return results


def browser_ready():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise HTTPException(400, INSTALL_MSG)
    try:
        with sync_playwright() as p:
            ok = Path(p.chromium.executable_path).exists()
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(400, INSTALL_MSG)


def _brief(c):
    return {"id": c["id"], "label": c["label"], "viewport": c["viewport"], "message": c["message"]}


def render_and_check(content, page, link, work):
    html_path = Path(work) / "index.html"
    html_path.write_text(render_html(content, page, link, False), encoding="utf-8")
    checks = run_checks(html_path, work)
    repair = {"applied": False, "before": [], "fixed": [], "still_failing": []}
    failing = [c for c in checks if not c["pass"] and c["id"] in LAYOUT_CHECKS]
    if failing and REPAIR_CSS.strip():
        html_path.write_text(render_html(content, page, link, True), encoding="utf-8")
        after = run_checks(html_path, work)
        now_bad = {(c["id"], c["viewport"]) for c in after if not c["pass"]}
        repair = {"applied": True, "before": [_brief(c) for c in failing],
                  "fixed": [_brief(c) for c in failing if (c["id"], c["viewport"]) not in now_bad],
                  "still_failing": [_brief(c) for c in failing if (c["id"], c["viewport"]) in now_bad]}
        checks = after
    return checks, repair


def write_checks_file(work, checks, repair):
    data = {"all_passed": all(c["pass"] for c in checks), "screens": {k: f"{w}x{h}" for k, (w, h) in VIEWPORTS.items()},
            "checks": checks, "repair": repair, "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (Path(work) / "checks.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _missing(link, page):
    out = []
    if not link:
        out.append("order link: add one so the button works")
    if not page.get("json_ld"):
        out.append("product schema: needs a price and a currency in Shop setup")
    return out


class LandingIn(BaseModel):
    product_id: int
    link: str | None = None
    use_real_photo: bool = False


@router.post("/generate", dependencies=GUARD)
def generate(body: LandingIn):
    p = _product(body.product_id)
    photo = _photo_file(p)
    if not photo:
        raise HTTPException(400, "This product has no photo. The page uses the real product photo, so add one first.")
    entry = _entry(p["id"])
    if not variant_prices(entry):
        raise HTTPException(400, "Set a Cost (the price the customer pays) or an MRP for this product first.")
    link = clean_link(body.link)
    browser_ready()
    cfg = shop_config.load()
    attrs = json.loads(p["attrs"] or "{}")
    facts = model_facts(p, entry, attrs)
    content = write_content(cfg["business_type"], facts)
    lines = policy_lines(lines_of(content), facts_for(p, attrs))
    page = build_page_data(p, entry, attrs, cfg)
    stamp = str(int(time.time() * 1000))
    work = LANDING_DIR / f"_new_{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        source_file, source = photo, "your real photo"
        if not body.use_real_photo:
            ai = work / "ai.png"
            try:
                with purpose("landing_image"):
                    edit_image(image_prompt(pretty_name(p["name"]), cfg["business_type"]), str(photo), str(ai))
                source_file, source = ai, "AI image made from your photo"
            except Exception as e:
                log("ai", "landing_image_failed", f"product={p['id']} {str(e)[:120]}")
                source = "your real photo (the AI image could not be made)"
        page["photo_w"], page["photo_h"] = prepare_photo(source_file, work / "photo.jpg")
        (work / "ai.png").unlink(missing_ok=True)
        checks, repair = render_and_check(content, page, link, work)
        write_checks_file(work, checks, repair)
        payload = {"product_id": p["id"], "product_name": pretty_name(p["name"]), "content": content, "lines": lines,
                   "link": link or "", "page": page, "missing": _missing(link, page), "checks": checks,
                   "checks_ok": all(c["pass"] for c in checks), "repair": repair, "checked": True, "stamp": stamp,
                   "image_source": source}
        with get_conn() as conn:
            pid = conn.execute("INSERT INTO proposals (kind, payload) VALUES (?, ?)", ("landing_page", json.dumps(payload))).lastrowid
            payload["id"] = pid
            conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(payload), pid))
        dest = LANDING_DIR / str(pid)
        if dest.exists():
            shutil.rmtree(dest)
        work.rename(dest)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    log("ai", "landing_page_created", f"id={pid} product={p['id']} checks_ok={payload['checks_ok']} repaired={repair['applied']}")
    return {"proposal_id": pid, "checks_ok": payload["checks_ok"], "failed": [_brief(c) for c in checks if not c["pass"]],
            "repaired": repair["applied"]}


def _load(conn, pid):
    row = conn.execute("SELECT status, kind, payload FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not row or row["kind"] != "landing_page":
        raise HTTPException(404, "Landing page not found")
    return row, json.loads(row["payload"])


class LandingEdit(BaseModel):
    headline: str | None = None
    subheading: str | None = None
    bullets: list[str] | None = None
    description: str | None = None
    cta: str | None = None
    link: str | None = None
    confirmed: list[str] | None = None


@router.put("/{pid}", dependencies=GUARD)
def edit_page(pid: int, body: LandingEdit):
    with get_conn() as conn:
        row, pay = _load(conn, pid)
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pay["product_id"],)).fetchone()
    if row["status"] != "pending":
        raise HTTPException(400, f"Already {row['status']}")
    old_content = pay["content"]
    new = dict(old_content)
    for key in ("headline", "subheading", "description", "cta"):
        v = getattr(body, key)
        if v is not None:
            new[key] = v
    if body.bullets is not None:
        new["bullets"] = [b for b in body.bullets if _clean(b)]
    problems = content_problems(new, False)
    if problems:
        raise HTTPException(400, "Cannot save: " + problems[0] + ".")
    new = clean_content(new)
    old_link = pay.get("link") or ""
    link = old_link
    if body.link is not None:
        link = clean_link(body.link) or ""
    old_lines = {l["key"]: l for l in pay["lines"]}
    new_lines = lines_of(new)
    changed = [(k, lab, t) for k, lab, t in new_lines if k not in old_lines or old_lines[k]["text"] != t]
    rerender = new != old_content or link != old_link
    if rerender:
        browser_ready()
    facts = facts_for(p, json.loads(p["attrs"] or "{}")) if p else {"name": pay["product_name"]}
    fresh = {l["key"]: l for l in policy_lines(changed, facts)} if changed else {}
    lines = []
    for k, lab, t in new_lines:
        if k in fresh:
            lines.append(fresh[k])
            continue
        keep = dict(old_lines[k])
        keep["label"] = lab
        if body.confirmed is not None:
            keep["confirmed"] = k in body.confirmed
        lines.append(keep)
    pay.update(content=new, lines=lines, link=link)
    if rerender:
        stamp = str(int(time.time() * 1000))
        dest = LANDING_DIR / str(pid)
        work = LANDING_DIR / f"_edit_{pid}_{stamp}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(dest / "photo.jpg", work / "photo.jpg")
            checks, repair = render_and_check(new, pay["page"], link, work)
            write_checks_file(work, checks, repair)
            for name in ("index.html", "phone.png", "desktop.png", "checks.json"):
                (work / name).replace(dest / name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        pay.update(checks=checks, checks_ok=all(c["pass"] for c in checks), repair=repair, checked=True, stamp=stamp)
    pay["missing"] = _missing(link, pay["page"])
    with get_conn() as conn:
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(pay), pid))
    log("owner", "landing_page_edited", f"id={pid} lines={[k for k, _, _ in changed]}")
    return {"proposal_id": pid, "changed": [k for k, _, _ in changed], "rechecked": rerender, "checks_ok": pay["checks_ok"]}


def gate_error(pay):
    if not pay.get("checked") or not pay.get("checks"):
        return "This page has not been checked in a browser yet. Save it again to run the check."
    folder = LANDING_DIR / str(pay.get("id", ""))
    for name in ("index.html", "phone.png", "desktop.png"):
        if not (folder / name).is_file():
            return "A file of this page is missing. Make the page again."
    lines = pay.get("lines", [])
    if any(l.get("blocked") for l in lines):
        return "A line breaks the ad rules. Edit it first."
    open_lines = [l["label"] for l in lines
                  if any(f.startswith("unsupported claim") for f in l.get("flags", [])) and not l.get("confirmed")]
    if open_lines:
        return f"These lines contain claims we cannot verify: {', '.join(open_lines)}. Edit them or confirm they are true."
    return None


@router.get("/approved")
def approved():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, payload, decided_at FROM proposals WHERE kind='landing_page' AND status='approved' ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        pay = json.loads(r["payload"])
        out.append({"id": r["id"], "product_name": pay["product_name"], "approved_at": r["decided_at"],
                    "checks_ok": pay.get("checks_ok"), "stamp": pay.get("stamp")})
    return out


def _file(pid, name):
    f = LANDING_DIR / str(pid) / name
    if not f.is_file():
        raise HTTPException(404, "That file of the page is missing. Make the page again.")
    return f


@router.get("/{pid}/page")
def page(pid: int):
    with get_conn() as conn:
        _load(conn, pid)
    return HTMLResponse(_file(pid, "index.html").read_text(encoding="utf-8"), headers={
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff"})


@router.get("/{pid}/photo.jpg")
def photo(pid: int):
    with get_conn() as conn:
        _load(conn, pid)
    return FileResponse(_file(pid, "photo.jpg"), media_type="image/jpeg")


@router.get("/{pid}/shot/{which}")
def shot(pid: int, which: str):
    if which not in VIEWPORTS:
        raise HTTPException(404, "Use phone or desktop.")
    with get_conn() as conn:
        _load(conn, pid)
    return FileResponse(_file(pid, f"{which}.png"), media_type="image/png")


@router.get("/{pid}/pack")
def pack(pid: int):
    with get_conn() as conn:
        row, _ = _load(conn, pid)
    if row["status"] != "approved":
        raise HTTPException(400, "Approve the page first.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PACK_FILES:
            z.write(_file(pid, name), name)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="landing_page_{pid}.zip"'})
