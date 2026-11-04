"""Camera-to-catalog by reading the price tags.

The model reads every fully visible tag in each frame. The same physical
product shows up in several frames, so readings are merged by what the tag
says (brand + item + price), not by how the garment looks. Agreement across
frames is the free check: a product read the same way in 2+ frames is trusted,
a product seen once or read inconsistently is flagged for the owner.
"""
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from app import shop_config
from app.model_client import ask_with_image
from app.calllog import purpose

PROMPT = """This is one frame from a video of a shelf in a {business_type}.
Products are shown with price tags or labels.

List every product whose tag or label is FULLY visible and readable.
Skip any product whose tag is cut off by the frame edge, hidden, or too blurry to read.
Copy the tag text exactly as printed. Never guess or invent text.
If a product has no tag at all but is fully in frame, list it with brand null,
price null and a short visual description as the name.

Return ONLY a JSON array, no other text. Each item:
{{"brand": "brand printed on the tag, or null",
  "name": "what the item is, e.g. Cotton Kurta",
  "price": number with digits only, or null,
  "category": one of {categories},
  "colour": "main colour of the product",
  "x": horizontal centre of the product as a number from 0 (left edge) to 1 (right edge)}}"""

NAME_MATCH = 0.82    # how alike two item names must be to be the same product (tolerates one-letter misreads)
BRAND_MATCH = 0.80


def parse_json_list(text: str) -> list[dict]:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


# ---------- cleaning what the model returned ----------

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def parse_price(v) -> float | None:
    """'₹1,499', 'Rs. 499/-', 899, '899.50' -> a number. Anything else -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _clean_x(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0.0 <= x <= 1.0 else None


def _text(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s if s and s.lower() not in ("null", "none", "n/a") else None


def _valid_category(value, categories: list[str]) -> str:
    for c in categories:
        if _norm(c) == _norm(value):
            return c
    return "other" if "other" in categories else categories[0]


def _to_sighting(raw, frame: str, categories: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"))
    if not name:
        return None
    return {
        "name": name,
        "brand": _text(raw.get("brand")),
        "price": parse_price(raw.get("price")),
        "category": _valid_category(raw.get("category"), categories),
        "colour": _text(raw.get("colour")),
        "x": _clean_x(raw.get("x")),
        "frame": frame,
    }


# ---------- merging readings into products ----------

def _match_score(s: dict, p: dict) -> float | None:
    """How well a new reading fits an existing product, or None if it cannot be the same one."""
    name_sim = max(_sim(_norm(s["name"]), _norm(n)) for n in p["names"])
    if name_sim < NAME_MATCH:
        return None
    brand_sim = 0.5                                    # unknown brand on one side: neutral
    if s["brand"] and p["brands"]:
        brand_sim = max(_sim(_norm(s["brand"]), _norm(b)) for b in p["brands"])
        if brand_sim < BRAND_MATCH:
            return None                                # two different brands are two products
    if s["price"] is not None and p["prices"]:
        if all(abs(s["price"] - pr) > 0.5 for pr in p["prices"]):
            return None                                # same name and brand but another price: another product
    return name_sim + brand_sim


def _vote(values: list):
    """The most common value (compared ignoring case and punctuation); ties go to the first seen."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    key = lambda v: _norm(v) if isinstance(v, str) else v
    counts = Counter(key(v) for v in values)
    top = max(counts.values())
    winner = next(k for k in (key(v) for v in values) if counts[k] == top)
    return next(v for v in values if key(v) == winner)


def merge_sightings(sightings: list[dict]) -> list[dict]:
    products: list[dict] = []
    for s in sightings:
        best, best_score = None, 0.0
        for p in products:
            score = _match_score(s, p)
            if score is not None and score > best_score:
                best, best_score = p, score
        if best is None:
            best = {"sightings": [], "names": [], "brands": [], "prices": []}
            products.append(best)
        best["sightings"].append(s)
        best["names"].append(s["name"])
        if s["brand"]:
            best["brands"].append(s["brand"])
        if s["price"] is not None:
            best["prices"].append(s["price"])

    items = []
    for p in products:
        ss = p["sightings"]
        frames = list(dict.fromkeys(s["frame"] for s in ss))
        names = [s["name"] for s in ss]
        price = _vote([s["price"] for s in ss])
        centred = [s for s in ss if s["x"] is not None]
        best_view = min(centred, key=lambda s: abs(s["x"] - 0.5)) if centred else ss[0]

        check = []
        if len(frames) == 1:
            check.append("seen in only one frame")
        if len({_norm(n) for n in names}) > 1:
            check.append("name read differently: " + " / ".join(dict.fromkeys(names)))
        if len(set(p["prices"])) > 1:
            check.append("price read differently: " + " / ".join(f"{x:g}" for x in dict.fromkeys(p["prices"])))
        if price is None:
            check.append("no price found")
        if not any(s["brand"] for s in ss):
            check.append("no brand found")

        items.append({
            "name": _vote(names),
            "brand": _vote([s["brand"] for s in ss]),
            "category": _vote([s["category"] for s in ss]),
            "price": price,
            "colour": _vote([s["colour"] for s in ss]),
            "frames": frames,
            "best_frame": best_view["frame"],
            "best_x": best_view["x"],
            "seen": len(frames),
            "check": check,
        })
    return items


# ---------- reading the frames ----------

def build_prompt(notes: str = "") -> str:
    cfg = shop_config.load()
    prompt = PROMPT.format(business_type=cfg["business_type"], categories=json.dumps(cfg["categories"]))
    notes = (notes or "").strip()[:500]
    if notes:
        prompt += ("\n\nNotes from the shop owner about this shelf. Use them to help read the tags; "
                   "if a note contradicts a tag, trust the tag:\n" + notes)
    return prompt


def build_catalog(frame_paths: list[str], errors: list | None = None, notes: str = "") -> list[dict]:
    """Read every frame, then merge. A frame that fails is recorded in `errors` and skipped."""
    prompt = build_prompt(notes)
    categories = shop_config.load()["categories"]
    sightings: list[dict] = []
    for path in frame_paths:
        try:
            with purpose("catalog_read"):
                raw = ask_with_image(prompt, path)
        except Exception as e:                       # one bad call must not lose the whole video
            if errors is not None:
                errors.append((path, str(e)[:200]))
            continue
        for entry in parse_json_list(raw):
            s = _to_sighting(entry, path, categories)
            if s:
                sightings.append(s)
    return merge_sightings(sightings)