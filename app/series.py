"""A four-ad series that keeps one look, plus a translated version.

One shared style brief is written once and put into all four image prompts, and every image is
made from the same real product photo (optionally with up to two style-reference photos joined
into one reference board). Headline, price and button are drawn onto the pictures in code, so
the text is always correct and readable, in English or in the translated language.

Colour consistency across the four pictures is measured and shown as a rough score; a series
with a low score is flagged so the owner can regenerate it. The series waits in the approval
queue like every other proposal, and ad copy goes through the same policy checks as single ads.
"""
import html as htmllib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont, features
from app.ads import facts_for
from app.audit import log
from app.calllog import purpose
from app.catalog import parse_json_list
from app.db import get_conn
from app.model_client import ask, edit_image
from app.policy import check_variants
from app.products import customer_price
from app import shop_config

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter(prefix="/series", tags=["series"])

OUT_DIR = Path("static/ads")
REF_DIR = Path("data/series_refs")
SIZE = 1024
MAX_REFS = 2
SCORE_OK = 60

SCENES = [
    ("hero", "Hero shot: the product centred on a clean seamless backdrop with generous empty space around it."),
    ("detail", "Detail shot: a tight close-up of the fabric or surface texture and finish of the product, still clearly the same product."),
    ("lifestyle", "Lifestyle shot: the product placed in a tasteful real-life setting that suits the shop. No people."),
    ("offer", "Offer shot: the product on the left third, with plain empty space on the right two thirds."),
]

LANGUAGES = {
    "hi": ("Hindi", "devanagari"), "mr": ("Marathi", "devanagari"), "gu": ("Gujarati", "gujarati"),
    "ta": ("Tamil", "tamil"), "bn": ("Bengali", "bengali"), "te": ("Telugu", "telugu"),
}
SAMPLE = {"latin": "Ag", "devanagari": "\u0939\u093f", "gujarati": "\u0a97\u0ac1", "tamil": "\u0ba4\u0bae\u0bbf",
          "bengali": "\u09ac\u09be", "telugu": "\u0c24\u0c46"}
WIN = r"C:\Windows\Fonts"
FONTS = {
    "latin": [WIN + r"\segoeuib.ttf", WIN + r"\arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"],
    "indic": [WIN + r"\NirmalaB.ttf", WIN + r"\Nirmala.ttf", WIN + r"\Nirmala.ttc",
              "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
              "/System/Library/Fonts/Supplemental/ITFDevanagari.ttc"],
}

STYLE_PROMPT = """You are the art director for a {business_type}. Write ONE shared visual style for a series of four
ad photos of this product: {name}. All four photos must look like the same campaign.
Return ONLY a JSON object, short phrases only, no brand names, no text in the pictures:
{{"palette": "...", "background": "...", "lighting": "...", "mood": "...", "props": "..."}}"""

COPY_PROMPT = """Write 4 short Facebook/Instagram ad lines for this product, one for each picture:
1 hero shot, 2 close-up detail, 3 lifestyle, 4 offer. Each line takes a different angle.
Product facts (the ONLY verified facts): {facts}
Return ONLY a JSON array of 4 objects: {{"headline": "max 40 characters", "text": "max 125 characters", "cta": "Shop Now"}}
Rules: use only the facts; no guarantees, no "best", no free or percent-off offers, no urgency;
mention a price only if it is exactly {price}; plain words; no emojis."""

TRANSLATE_PROMPT = """Translate this ad headline and button text into {language}. Keep the meaning, keep it short and natural
for a shop in India. Do not add claims. Write them in the {language} script (not in English letters). Return ONLY a JSON object: {{"headline": "...", "cta": "..."}}
Headline: {headline}
Button: {cta}"""

DEFAULT_STYLE = {"palette": "warm neutral tones with one soft accent colour", "background": "plain softly lit studio backdrop",
                 "lighting": "soft natural window light from the left", "mood": "calm, clean and premium",
                 "props": "none, or one small neutral prop"}


# ---------- fonts and text ----------

def has_raqm() -> bool:
    return bool(features.check("raqm"))


def _covers(font, script: str) -> bool:
    try:
        missing = bytes(font.getmask("\U0010ffff"))
        return all(bytes(font.getmask(ch)) != missing for ch in SAMPLE[script])
    except Exception:
        return False


def find_font(script: str, px: int = 40) -> str | None:
    env = os.environ.get("SERIES_FONT", "").strip()
    paths = ([env] if env else []) + FONTS["latin" if script == "latin" else "indic"]
    for p in paths:
        if os.path.isfile(p):
            try:
                font = ImageFont.truetype(p, px)
            except OSError:
                continue
            if _covers(font, script):
                return p
    return None


def _load(path: str, px: int):
    engine = ImageFont.Layout.RAQM if has_raqm() else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, px, layout_engine=engine)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _browser_block(text: str, max_w: int, px: int, color: str) -> Image.Image:
    """Text drawn by a real browser, for scripts that Pillow cannot shape without libraqm."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise HTTPException(
            400, "Translated text in this language needs either Pillow with libraqm or Playwright "
                 "(pip install playwright, then playwright install chromium).")
    page = ("<html><body style='margin:0;background:transparent'><div id=t style='display:inline-block;"
            f"max-width:{max_w}px;font:700 {px}px/1.3 \"Nirmala UI\",\"Noto Sans Devanagari\",\"Noto Sans\",FreeSans,sans-serif;"
            f"color:{color}'>{htmllib.escape(text)}</div></body></html>")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(page)
        png = pg.locator("#t").screenshot(omit_background=True)
        b.close()
    return Image.open(io.BytesIO(png)).convert("RGBA")


def text_block(text: str, max_w: int, px: int, color: str, script: str) -> Image.Image:
    """The text as a transparent picture, wrapped to max_w."""
    if script != "latin" and not has_raqm():
        return _browser_block(text, max_w, px, color)
    path = find_font(script, px)
    if not path:
        raise HTTPException(400, f"No font that can draw {script} text was found. Set SERIES_FONT in .env to a font file that supports it.")
    font = _load(path, px)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = _wrap(probe, text, font, max_w)
    line_h = int(px * 1.3)
    img = Image.new("RGBA", (max_w, line_h * len(lines) + 6), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((0, i * line_h), line, font=font, fill=color)
    return img


def overlay(raw_path: str, out_path: str, headline: str, price: str, cta: str, script: str = "latin") -> None:
    """Dark fade at the bottom, headline, price and a button, drawn in code."""
    img = Image.open(raw_path).convert("RGB").resize((SIZE, SIZE))
    band = int(SIZE * 0.42)
    fade = Image.new("RGBA", (SIZE, band), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for y in range(band):
        fd.line([(0, y), (SIZE, y)], fill=(10, 10, 14, int(225 * (y / band) ** 1.2)))
    base = img.convert("RGBA")
    base.alpha_composite(fade, (0, SIZE - band))
    margin, max_w = 56, SIZE - 112
    px = 64
    block = text_block(headline, max_w, px, "#ffffff", script)
    while block.height > px * 1.3 * 2 + 8 and px > 36:
        px -= 6
        block = text_block(headline, max_w, px, "#ffffff", script)
    cta_block = text_block(cta, 400, 30, "#1a1a1a", script)
    pill_w, pill_h = cta_block.width + 56, cta_block.height + 22
    y_pill = SIZE - 56 - pill_h
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([margin, y_pill, margin + pill_w, y_pill + pill_h], radius=pill_h // 2, fill=(245, 179, 1, 255))
    base.alpha_composite(cta_block, (margin + 28, y_pill + 11))
    if price:
        price_block = text_block(price, 360, 34, "#ffffff", "latin")
        base.alpha_composite(price_block, (margin + pill_w + 28, y_pill + (pill_h - price_block.height) // 2))
    base.alpha_composite(block, (margin, y_pill - 26 - block.height))
    base.convert("RGB").save(out_path, "PNG")


# ---------- consistency ----------

def _signature(path: str) -> tuple[list[float], list[float]]:
    """Colour make-up: a hue histogram (plus a 'grey' bin) and a brightness histogram, each summing to 1."""
    img = Image.open(path).convert("RGB").resize((64, 64)).convert("HSV")
    hue, val = [0] * 17, [0] * 4
    px = list(img.get_flattened_data()) if hasattr(img, "get_flattened_data") else list(img.getdata())
    for h, s, v in px:
        val[min(3, v // 64)] += 1
        hue[min(15, h // 16) if s > 40 and v > 40 else 16] += 1
    n = float(len(px))
    return [x / n for x in hue], [x / n for x in val]


def consistency(paths: list[str]) -> dict:
    """Average overlap of the pictures' colour and brightness make-up, 0 to 100. A rough measure, not a judgement of style."""
    sigs = [_signature(p) for p in paths]
    pairs = []
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            hue = sum(min(a, b) for a, b in zip(sigs[i][0], sigs[j][0]))
            val = sum(min(a, b) for a, b in zip(sigs[i][1], sigs[j][1]))
            pairs.append((hue + val) / 2 * 100)
    if not pairs:
        return {"score": 0, "lowest_pair": 0}
    return {"score": round(sum(pairs) / len(pairs)), "lowest_pair": round(min(pairs))}


# ---------- model steps ----------

def _json_obj(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        v = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def style_brief(name: str, business: str) -> tuple[dict, str]:
    """One shared style. If the model fails twice, the default style is used and the owner is told."""
    for _ in range(2):
        with purpose("series_style"):
            raw = ask(STYLE_PROMPT.format(business_type=business, name=name))
        o = _json_obj(raw) or {}
        got = {k: str(o.get(k, "")).strip() for k in DEFAULT_STYLE}
        if all(0 < len(v) <= 120 for v in got.values()):
            return got, "model"
    return dict(DEFAULT_STYLE), "default (the model's answer was unusable)"


def write_copy(facts: dict, price: str) -> list[dict]:
    last = "no usable answer"
    for _ in range(2):
        with purpose("series_copy"):
            raw = ask(COPY_PROMPT.format(facts=json.dumps(facts, ensure_ascii=False), price=price or "not given, so mention no price"))
        items = parse_json_list(raw)
        ok = [i for i in items if isinstance(i, dict) and str(i.get("headline", "")).strip() and str(i.get("text", "")).strip()]
        if len(ok) >= 4 and all(len(str(i["headline"])) <= 40 and len(str(i["text"])) <= 125 for i in ok[:4]):
            return [{"headline": str(i["headline"]).strip(), "text": str(i["text"]).strip(),
                     "cta": (str(i.get("cta") or "Shop Now").strip() or "Shop Now")[:20]} for i in ok[:4]]
        last = "needs 4 lines, headline up to 40 and text up to 125 characters"
    raise HTTPException(502, f"The model did not return usable ad lines ({last}). Try again.")


SCRIPT_RANGES = {"devanagari": (0x0900, 0x097F), "gujarati": (0x0A80, 0x0AFF), "tamil": (0x0B80, 0x0BFF),
                 "bengali": (0x0980, 0x09FF), "telugu": (0x0C00, 0x0C7F)}


def in_script(text: str, script: str) -> bool:
    """True when most of the letters in the text are in the target script (so an English answer is caught)."""
    lo, hi = SCRIPT_RANGES[script]
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(lo <= ord(ch) <= hi for ch in letters) >= 0.6 * len(letters)


def translate(headline: str, cta: str, language: str) -> dict:
    name, script = LANGUAGES[language]
    problem = ""
    for _ in range(2):
        extra = (f"\nYour last answer was wrong: {problem}. Write the words in the {name} script, not in English letters.\n"
                 if problem else "")
        with purpose("series_translate"):
            raw = ask(TRANSLATE_PROMPT.format(language=name, headline=headline, cta=cta) + extra)
        o = _json_obj(raw) or {}
        h, c = str(o.get("headline", "")).strip(), str(o.get("cta", "")).strip()
        if not (h and c and len(h) <= 80 and len(c) <= 30):
            problem = "it was not a JSON object with a short headline and button"
        elif not in_script(h, script) or not in_script(c, script):
            problem = f"it was not written in {name}"
        else:
            return {"headline": h, "cta": c}
    raise HTTPException(502, f"The model did not return a {name} translation ({problem}). Try again.")


def make_board(product_id: int, photo: Path) -> tuple[Path, int]:
    """The product photo, with up to two style-reference photos beside it as one reference picture."""
    extras = sorted(REF_DIR.glob(f"{product_id}_*.jpg"))[:MAX_REFS]
    if not extras:
        return photo, 0
    imgs = [Image.open(photo).convert("RGB")] + [Image.open(p).convert("RGB") for p in extras]
    h = 768
    imgs = [i.resize((max(1, int(i.width * h / i.height)), h)) for i in imgs]
    board = Image.new("RGB", (sum(i.width for i in imgs) + 12 * (len(imgs) - 1), h), (255, 255, 255))
    x = 0
    for i in imgs:
        board.paste(i, (x, 0))
        x += i.width + 12
    REF_DIR.mkdir(parents=True, exist_ok=True)
    out = REF_DIR / f"board_{product_id}.jpg"
    board.save(out, "JPEG", quality=90)
    return out, len(extras)


def image_prompt(scene: str, style: dict, name: str, board_used: bool) -> str:
    ref = ("The reference picture holds the product photo on the left; the other pictures only show the preferred style and mood, "
           "never copy their products. " if board_used else "")
    return (f"Commercial product photograph for an ad campaign. {ref}Keep the product ({name}) exactly the same as in the reference: "
            f"same shape, colour, pattern, material and details. Show only that one product, as it is normally sold. "
            f"Remove any price tag, hanger or clutter from the reference. {scene} "
            f"Shared visual style, identical in every picture of the series: palette: {style['palette']}; background: {style['background']}; "
            f"lighting: {style['lighting']}; mood: {style['mood']}; props: {style['props']}. "
            f"Do not add any text, logos, watermarks or people.")


# ---------- routes ----------

def _product(pid: int):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Product not found")
    return p


def _photo_file(p) -> Path | None:
    url = p["photo"]
    if isinstance(url, str) and url.startswith("/photos/"):
        f = Path("data/photos") / Path(url).name
        return f if f.is_file() else None
    return None


def _price_text(p) -> str:
    sell = customer_price(p["price"], p["cost"])
    cur = shop_config.load()["currency"]
    return f"{cur} {sell:g}" if sell else ""


@router.get("/config")
def series_config():
    langs = {}
    for code, (name, script) in LANGUAGES.items():
        font = find_font(script)
        langs[code] = {"name": name, "font": bool(font),
                       "ready": bool(font) and (has_raqm() or _playwright_ok())}
    return {"languages": langs, "shaping": has_raqm(), "latin_font": bool(find_font("latin")), "max_refs": MAX_REFS}


def _playwright_ok() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


@router.post("/references/{product_id}")
async def add_reference(product_id: int, file: UploadFile = File(...)):
    _product(product_id)
    raw = await file.read(6_000_001)
    if len(raw) > 6_000_000:
        raise HTTPException(400, "That picture is too large (6 MB at most).")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "That file is not a picture we can read (use JPG or PNG).")
    REF_DIR.mkdir(parents=True, exist_ok=True)
    have = sorted(REF_DIR.glob(f"{product_id}_*.jpg"))
    if len(have) >= MAX_REFS:
        raise HTTPException(400, f"Only {MAX_REFS} style pictures per product. Remove one first.")
    n = 1 + max([int(p.stem.split("_")[1]) for p in have] + [0])
    img.convert("RGB").save(REF_DIR / f"{product_id}_{n}.jpg", "JPEG", quality=90)
    log("owner", "series_reference_added", f"product {product_id}")
    return {"count": len(have) + 1}


@router.get("/references/{product_id}")
def list_references(product_id: int):
    return {"count": len(list(REF_DIR.glob(f"{product_id}_*.jpg"))) if REF_DIR.exists() else 0}


@router.delete("/references/{product_id}")
def clear_references(product_id: int):
    n = 0
    for p in REF_DIR.glob(f"{product_id}_*.jpg") if REF_DIR.exists() else []:
        p.unlink()
        n += 1
    return {"removed": n}


class SeriesIn(BaseModel):
    product_id: int
    language: str | None = None
    localize_slot: int = 0


def _render_slot(raw: str, headline: str, price: str, cta: str, out: str):
    overlay(raw, out, headline, price, cta)


def _make_localized(slots: list[dict], slot: int, language: str, price: str, stamp: str, pid: int) -> dict:
    if language not in LANGUAGES:
        raise HTTPException(400, "Choose one of: " + ", ".join(f"{c} ({v[0]})" for c, v in LANGUAGES.items()))
    if not 0 <= slot < len(slots) or not slots[slot].get("raw_path"):
        raise HTTPException(400, "That picture is not available to translate.")
    s = slots[slot]
    t = translate(s["headline"], s["cta"], language)
    out = OUT_DIR / f"series_{pid}_{stamp}_{language}.png"
    overlay(s["raw_path"], str(out), t["headline"], price, t["cta"], script=LANGUAGES[language][1])
    return {"language": language, "label": LANGUAGES[language][0], "slot": slot, "headline": t["headline"],
            "cta": t["cta"], "url": f"/static/ads/{out.name}"}


@router.post("/generate", dependencies=[Depends(require_off)])
def generate(body: SeriesIn):
    p = _product(body.product_id)
    photo = _photo_file(p)
    if not photo:
        raise HTTPException(400, "This product has no photo. A series is built from the real product photo, so add one first.")
    sell = customer_price(p["price"], p["cost"])
    if not sell or sell <= 0:
        raise HTTPException(400, "Set a Cost (the price the customer pays) or an MRP for this product first.")
    if body.language is not None and body.language not in LANGUAGES:
        raise HTTPException(400, "Choose one of: " + ", ".join(f"{c} ({v[0]})" for c, v in LANGUAGES.items()))
    if not find_font("latin"):
        raise HTTPException(400, "No font was found to draw text on the pictures. Set SERIES_FONT in .env to a .ttf file.")
    cfg = shop_config.load()
    attrs = json.loads(p["attrs"] or "{}")
    facts = facts_for(p, attrs)
    price = _price_text(p)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time() * 1000))

    style, style_source = style_brief(facts["name"], cfg["business_type"])
    lines = write_copy(facts, price)
    lines = check_variants([dict(l) for l in lines], facts)
    ref, extras = make_board(p["id"], photo)

    slots = []
    for i, ((key, scene), line) in enumerate(zip(SCENES, lines)):
        slot = {"key": key, "headline": line["headline"], "text": line["text"], "cta": line["cta"],
                "flags": line["flags"], "blocked": line["blocked"], "confirmed": False, "raw_path": "", "url": "", "error": ""}
        raw = OUT_DIR / f"series_{p['id']}_{stamp}_{key}_raw.png"
        err = ""
        for attempt in range(2):
            try:
                with purpose("series_image"):
                    edit_image(image_prompt(scene, style, facts["name"], extras > 0), str(ref), str(raw))
                err = ""
                break
            except Exception as e:
                err = str(e)[:120]
        if err or not raw.is_file():
            slot["error"] = "The image could not be made. " + (err or "")
            log("ai", "series_image_failed", f"product={p['id']} slot={key} {err}")
        else:
            final = OUT_DIR / f"series_{p['id']}_{stamp}_{key}.png"
            overlay(str(raw), str(final), slot["headline"], price, slot["cta"])
            slot.update(raw_path=str(raw), url=f"/static/ads/{final.name}", raw_url=f"/static/ads/{raw.name}")
        slots.append(slot)

    made = [s["raw_path"] for s in slots if s["raw_path"]]
    cons = consistency(made) if len(made) >= 2 else {"score": 0, "lowest_pair": 0}
    cons["note"] = ("Colours match well across the pictures." if cons["score"] >= SCORE_OK
                    else "The pictures differ a lot in colour. Regenerate for a more consistent look.")
    localized, loc_err = None, ""
    if body.language:
        try:
            localized = _make_localized(slots, body.localize_slot, body.language, price, stamp, p["id"])
        except HTTPException as e:
            loc_err = str(e.detail)
    payload = {"product_id": p["id"], "product_name": facts["name"], "price_text": price, "style": style,
               "style_source": style_source, "reference": {"extra_photos": extras},
               "slots": slots, "consistency": cons, "localized": localized, "localized_error": loc_err,
               "stamp": stamp, "image_source": "generated from your photo"}
    with get_conn() as conn:
        pid = conn.execute("INSERT INTO proposals (kind, payload) VALUES (?, ?)", ("ad_series", json.dumps(payload))).lastrowid
    log("ai", "ad_series_created", f"id={pid} product={p['id']} images={len(made)}/4 consistency={cons['score']}")
    return {"proposal_id": pid, "images": len(made), "consistency": cons, "localized": bool(localized), "localized_error": loc_err}


def _load_series(conn, pid: int):
    row = conn.execute("SELECT status, kind, payload FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not row or row["kind"] != "ad_series":
        raise HTTPException(404, "Ad series not found")
    return row, json.loads(row["payload"])


class SeriesEdit(BaseModel):
    slots: list[dict]


@router.put("/{pid}")
def edit_series(pid: int, body: SeriesEdit):
    with get_conn() as conn:
        row, pay = _load_series(conn, pid)
        if row["status"] != "pending":
            raise HTTPException(400, f"Already {row['status']}")
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pay["product_id"],)).fetchone()
    facts = facts_for(p, json.loads(p["attrs"] or "{}")) if p else {"name": pay["product_name"]}
    changed = []
    for e in body.slots:
        i = e.get("slot")
        if not isinstance(i, int) or not 0 <= i < len(pay["slots"]):
            raise HTTPException(400, "Unknown picture number.")
        s = pay["slots"][i]
        h, t = str(e.get("headline", s["headline"])).strip(), str(e.get("text", s["text"])).strip()
        c = (str(e.get("cta", s["cta"])).strip() or "Shop Now")[:20]
        if not h or not t:
            raise HTTPException(400, "Headline and text cannot be empty.")
        if len(h) > 40 or len(t) > 125:
            raise HTTPException(400, "Headline can be 40 characters and text 125 at most.")
        s["confirmed"] = bool(e.get("confirmed", s.get("confirmed"))) if (h, t) == (s["headline"], s["text"]) else False
        if (h, t, c) != (s["headline"], s["text"], s["cta"]):
            s.update(headline=h, text=t, cta=c)
            changed.append(i)
    if changed:
        checked = check_variants([{"headline": pay["slots"][i]["headline"], "text": pay["slots"][i]["text"]} for i in changed], facts)
        for i, v in zip(changed, checked):
            pay["slots"][i]["flags"], pay["slots"][i]["blocked"] = v["flags"], v["blocked"]
            s = pay["slots"][i]
            if s["raw_path"]:
                final = OUT_DIR / f"series_{pay['product_id']}_{pay['stamp']}_{s['key']}.png"
                overlay(s["raw_path"], str(final), s["headline"], pay["price_text"], s["cta"])
    with get_conn() as conn:
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(pay), pid))
    log("owner", "ad_series_edited", f"id={pid} slots={changed}")
    return {"proposal_id": pid, "changed": changed}


class LocalizeIn(BaseModel):
    language: str
    slot: int = 0


@router.post("/{pid}/localize", dependencies=[Depends(require_off)])
def localize(pid: int, body: LocalizeIn):
    with get_conn() as conn:
        row, pay = _load_series(conn, pid)
    if row["status"] != "pending":
        raise HTTPException(400, f"Already {row['status']}")
    pay["localized"] = _make_localized(pay["slots"], body.slot, body.language, pay["price_text"], pay["stamp"], pay["product_id"])
    pay["localized_error"] = ""
    with get_conn() as conn:
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(pay), pid))
    log("ai", "ad_series_localized", f"id={pid} language={body.language}")
    return {"proposal_id": pid, "localized": pay["localized"]}


def gate_error(pay: dict) -> str | None:
    """Why this series cannot be approved yet, or None."""
    slots = pay.get("slots", [])
    bad = [i + 1 for i, s in enumerate(slots) if not s.get("url")]
    if len(slots) != 4 or bad:
        return f"Picture(s) {bad or 'all'} could not be made. Generate the series again."
    if any(s.get("blocked") for s in slots):
        return "A line breaks the ad rules. Edit it first."
    open_lines = [i + 1 for i, s in enumerate(slots)
                  if any(f.startswith("unsupported claim") for f in s.get("flags", [])) and not s.get("confirmed")]
    if open_lines:
        return f"Lines {open_lines} contain claims we cannot verify. Edit them or confirm they are true."
    return None


def _file(url: str | None) -> Path | None:
    if isinstance(url, str) and url.startswith("/static/ads/"):
        f = OUT_DIR / Path(url).name
        return f if f.is_file() else None
    return None


@router.get("/approved")
def approved():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, payload, decided_at FROM proposals WHERE kind='ad_series' AND status='approved' ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        pay = json.loads(r["payload"])
        out.append({"id": r["id"], "product_name": pay["product_name"], "approved_at": r["decided_at"],
                    "consistency": pay["consistency"]["score"], "localized": (pay.get("localized") or {}).get("label"),
                    "thumb": pay["slots"][0]["url"] if pay["slots"] else ""})
    return out


@router.get("/{pid}/pack")
def pack(pid: int):
    with get_conn() as conn:
        row, pay = _load_series(conn, pid)
    if row["status"] != "approved":
        raise HTTPException(400, "Approve the series first.")
    buf = io.BytesIO()
    text = [f"Ad series for {pay['product_name']}", f"Price shown: {pay['price_text']}", ""]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, s in enumerate(pay["slots"], 1):
            f = _file(s["url"])
            if f:
                z.write(f, f"ad_{i}_{s['key']}.png")
            text += [f"Ad {i} ({s['key']})", f"Headline: {s['headline']}", f"Text: {s['text']}", f"Button: {s['cta']}", ""]
        loc = pay.get("localized")
        if loc and _file(loc["url"]):
            z.write(_file(loc["url"]), f"ad_translated_{loc['language']}.png")
            text += [f"Translated ({loc['label']}) from ad {loc['slot'] + 1}", f"Headline: {loc['headline']}", f"Button: {loc['cta']}", ""]
        text += ["Style used for all pictures:"] + [f"- {k}: {v}" for k, v in pay["style"].items()]
        z.writestr("copy.txt", "\n".join(text))
        z.writestr("series.json", json.dumps({k: v for k, v in pay.items() if k != "slots"} | {
            "slots": [{k: v for k, v in s.items() if k not in ("raw_path",)} for s in pay["slots"]]}, ensure_ascii=False, indent=2))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="ad_series_{pid}.zip"'})
