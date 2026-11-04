"""Measure how good the camera-to-catalog step is on a shelf you labelled by hand.

Run from the repo root, with MODEL_API_KEY in .env:

    python eval/run_eval.py --video samples/shelf.mp4 --labels eval/labels.csv --manual-seconds-per-product 120

labels.csv has one row per distinct physical product on the shelf, copied from the
real tags by you: name, brand, price (and optionally category). See labels.example.csv.

Optional:
    --review-seconds-per-product 25   time the owner needs to correct the draft, measured once in the app
    --runs 3                          repeat to see how consistent the model is (costs 3x the credits)

Real model calls are made, so this uses credits. The calls are logged in eval/eval_calls.db,
not in the app's own database, so the app's report stays clean.
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_MIN = 0.75          # how alike a draft name and a label name must be to be the same product


# ---------- labels ----------

def _norm(s) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _price(v):
    import re
    m = re.search(r"\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return float(m.group()) if m else None


def load_labels(path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"Labels file not found: {p}")
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        if "name" not in cols:
            raise SystemExit(f"{p} needs a header row with at least a 'name' column (found: {reader.fieldnames}).")
        labels = []
        for n, row in enumerate(reader, 2):
            if None in row:                      # more values than headers, e.g. a price typed as 1,499
                raise SystemExit(f"{p} line {n} has more values than columns. Write prices without commas (1499), "
                                 f"or put the value in quotes (\"1,499\").")
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            if not row.get("name"):
                raise SystemExit(f"{p} line {n}: name is empty.")
            if row.get("price") and _price(row["price"]) is None:
                raise SystemExit(f"{p} line {n}: price '{row['price']}' is not a number.")
            labels.append({"name": row["name"], "brand": row.get("brand") or None,
                           "price": _price(row.get("price")), "category": row.get("category") or None})
    if not labels:
        raise SystemExit(f"{p} has no products in it.")
    return labels


# ---------- matching and scoring ----------

def _sim(a, b) -> float:
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _pair_score(item: dict, label: dict) -> float | None:
    name = _sim(item.get("name"), label["name"])
    if name < NAME_MIN:
        return None
    if item.get("brand") and label["brand"]:
        brand = _sim(item["brand"], label["brand"])
        if brand < 0.6:
            return None
    else:
        brand = 0.5
    price = 0.0
    ip = _price(item.get("price"))
    if ip is not None and label["price"] is not None and abs(ip - label["price"]) <= 0.5:
        price = 1.0
    return name + 0.3 * brand + 0.3 * price


def match(items: list[dict], labels: list[dict]):
    """One-to-one, best pairs first. Returns {item index: label index}."""
    pairs = []
    for i, it in enumerate(items):
        for j, lb in enumerate(labels):
            s = _pair_score(it, lb)
            if s is not None:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    used_i, used_j, out = set(), set(), {}
    for _, i, j in pairs:
        if i not in used_i and j not in used_j:
            out[i] = j; used_i.add(i); used_j.add(j)
    return out


def evaluate(items: list[dict], labels: list[dict]) -> dict:
    m = match(items, labels)
    matched_labels = set(m.values())
    detail, wrong, wrong_flagged = [], 0, 0
    name_ok = brand_ok = price_ok = full_ok = flagged_ok = 0
    for i, j in sorted(m.items(), key=lambda t: t[1]):
        it, lb = items[i], labels[j]
        n_ok = _norm(it.get("name")) == _norm(lb["name"])
        b_ok = _norm(it.get("brand")) == _norm(lb["brand"])
        ip = _price(it.get("price"))
        p_ok = (ip is None and lb["price"] is None) or (ip is not None and lb["price"] is not None and abs(ip - lb["price"]) <= 0.5)
        flagged = bool(it.get("check"))
        ok = n_ok and b_ok and p_ok
        name_ok += n_ok; brand_ok += b_ok; price_ok += p_ok; full_ok += ok
        if ok:
            flagged_ok += flagged
        else:
            wrong += 1; wrong_flagged += flagged
        detail.append({"label": lb["name"], "status": "correct" if ok else "wrong",
                       "wrong_fields": [k for k, good in (("name", n_ok), ("brand", b_ok), ("price", p_ok)) if not good],
                       "read_as": {"name": it.get("name"), "brand": it.get("brand"), "price": ip},
                       "expected": {"name": lb["name"], "brand": lb["brand"], "price": lb["price"]},
                       "flagged": flagged, "flags": it.get("check", [])})
    extras = []
    for i, it in enumerate(items):
        if i in m:
            continue
        dup = any(_pair_score(it, labels[j]) is not None for j in matched_labels)
        extras.append({"kind": "duplicate of a product already found" if dup else "not on the shelf",
                       "read_as": {"name": it.get("name"), "brand": it.get("brand"), "price": _price(it.get("price"))},
                       "flagged": bool(it.get("check")), "flags": it.get("check", [])})
    missed = [labels[j] for j in range(len(labels)) if j not in matched_labels]
    n_lab, n_found = len(labels), len(m)
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None
    return {
        "labels": n_lab, "items_drafted": len(items), "found": n_found, "missed": len(missed),
        "extras": len(extras), "duplicates": sum(e["kind"].startswith("duplicate") for e in extras),
        "recall_pct": pct(n_found, n_lab), "precision_pct": pct(n_found, len(items)),
        "name_correct_pct": pct(name_ok, n_found), "brand_correct_pct": pct(brand_ok, n_found),
        "price_correct_pct": pct(price_ok, n_found), "fully_correct_pct": pct(full_ok, n_found),
        "wrong_items": wrong, "wrong_and_flagged": wrong_flagged, "wrong_and_not_flagged": wrong - wrong_flagged,
        "flag_catch_pct": pct(wrong_flagged, wrong),
        "correct_but_flagged": flagged_ok,
        "flagged_items": sum(1 for it in items if it.get("check")),
        "detail": detail, "extras_detail": extras, "missed_detail": missed,
    }


def time_saved(manual_per_product: float, n_products: int, pipeline_seconds: float, review_per_product: float | None):
    manual = manual_per_product * n_products
    review = None if review_per_product is None else review_per_product * n_products
    with_tool = pipeline_seconds + (review or 0.0)
    return {"manual_seconds": round(manual, 1), "pipeline_seconds": round(pipeline_seconds, 1),
            "review_seconds": None if review is None else round(review, 1),
            "with_tool_seconds": round(with_tool, 1), "saved_seconds": round(manual - with_tool, 1),
            "saved_pct": round(100.0 * (manual - with_tool) / manual, 1) if manual else None,
            "includes_review": review_per_product is not None}


# ---------- one run ----------

def call_stats(db_path: Path, since_id: int) -> dict:
    import sqlite3
    from app.calllog import unit_prices
    if not db_path.exists():
        return {"calls": 0, "failed": 0, "seconds": 0.0, "estimated_cost": 0.0, "by_kind": {}}
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT kind, seconds, ok FROM model_calls WHERE id > ?", (since_id,)).fetchall()
    conn.close()
    prices = unit_prices()
    by_kind: dict[str, int] = {}
    for k, _, _ in rows:
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"calls": len(rows), "failed": sum(1 for r in rows if not r[2]),
            "seconds": round(sum(r[1] or 0 for r in rows), 2),
            "estimated_cost": round(sum(prices.get(k, 0.0) for k, _, _ in rows), 4), "by_kind": by_kind,
            "unit_prices_set": any(prices.values())}


def last_call_id(db_path: Path) -> int:
    import sqlite3
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM model_calls").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def run_once(video: str, labels: list[dict], notes: str, db_path: Path, frames_dir: str) -> dict:
    from app.frames import sample_frames
    from app.catalog import build_catalog
    before = last_call_id(db_path)
    start = time.perf_counter()
    frames = sample_frames(video, frames_dir)
    if not frames:
        raise SystemExit("No frames could be taken from the video (is ffmpeg installed and the file a video?).")
    errors: list = []
    items = build_catalog(frames, errors, notes)
    seconds = time.perf_counter() - start
    if len(errors) == len(frames):
        raise SystemExit(f"The model could not read any frame. First error: {errors[0][1]}")
    res = evaluate(items, labels)
    res["frames"] = len(frames); res["frames_failed"] = len(errors); res["pipeline_seconds"] = round(seconds, 1)
    res["calls"] = call_stats(db_path, before)
    return res


# ---------- report ----------

def summarise(runs: list[dict], n_labels: int, manual: float, review: float | None) -> dict:
    mean = lambda k: round(statistics.mean(r[k] for r in runs), 1) if all(r[k] is not None for r in runs) else None
    spread = lambda k: [min(r[k] for r in runs), max(r[k] for r in runs)] if all(r[k] is not None for r in runs) else None
    secs = statistics.mean(r["pipeline_seconds"] for r in runs)
    cost = statistics.mean(r["calls"]["estimated_cost"] for r in runs)
    calls = statistics.mean(r["calls"]["calls"] for r in runs)
    found = statistics.mean(r["found"] for r in runs)
    return {
        "runs": len(runs),
        "recall_pct": mean("recall_pct"), "precision_pct": mean("precision_pct"),
        "fully_correct_pct": mean("fully_correct_pct"), "price_correct_pct": mean("price_correct_pct"),
        "flag_catch_pct": mean("flag_catch_pct"), "wrong_and_not_flagged_mean": round(statistics.mean(r["wrong_and_not_flagged"] for r in runs), 2),
        "recall_range": spread("recall_pct"), "fully_correct_range": spread("fully_correct_pct"),
        "calls_per_run": round(calls, 1), "seconds_per_run": round(secs, 1),
        "calls_per_product": round(calls / n_labels, 2),
        "estimated_cost_per_run": round(cost, 4),
        "estimated_cost_per_product": round(cost / n_labels, 4),
        "estimated_cost_per_product_found": round(cost / found, 4) if found else None,
        "unit_prices_set": runs[0]["calls"].get("unit_prices_set", False),
        "time": time_saved(manual, n_labels, secs, review),
    }


def to_markdown(summary: dict, runs: list[dict], meta: dict) -> str:
    s, t = summary, summary["time"]
    f = lambda v, suf="": "n/a" if v is None else f"{v}{suf}"
    L = [f"# Catalog evaluation", "",
         f"Video: `{meta['video']}`  |  products on the shelf (hand-labelled): **{meta['labels']}**  |  runs: **{s['runs']}**  |  business type: {meta['business_type']}", "",
         "| Measure | Result |", "|---|---|",
         f"| Products found (recall) | {f(s['recall_pct'], '%')} |",
         f"| Draft items that are real products (precision) | {f(s['precision_pct'], '%')} |",
         f"| Found products with name, brand and price all correct | {f(s['fully_correct_pct'], '%')} |",
         f"| Price correct | {f(s['price_correct_pct'], '%')} |",
         f"| Wrong items the app flagged for the owner to check | {f(s['flag_catch_pct'], '%')} |",
         f"| Wrong items with NO flag (average per run) | {s['wrong_and_not_flagged_mean']} |",
         f"| Model calls per run | {s['calls_per_run']} ({s['calls_per_product']} per product) |",
         f"| Estimated cost per product (in the units set in .env) | {f(s['estimated_cost_per_product']) if s['unit_prices_set'] else 'unknown: set COST_VISION in .env'} |",
         f"| Time for the app to build the draft | {s['seconds_per_run']} s |",
         f"| Time to list them by hand | {t['manual_seconds']} s |",
         (f"| Time with the app, including owner review | {t['with_tool_seconds']} s |" if t["includes_review"]
          else "| Time with the app, including owner review | unknown: pass --review-seconds-per-product |"),
         (f"| **Time saved** | **{t['saved_seconds']} s ({t['saved_pct']}%)** |" if t["includes_review"]
          else f"| Time saved before owner review | {t['saved_seconds']} s ({t['saved_pct']}%) |"), ""]
    if s["runs"] > 1:
        L += [f"Across {s['runs']} runs, recall ranged {s['recall_range'][0]}% to {s['recall_range'][1]}% and fully-correct ranged "
              f"{s['fully_correct_range'][0]}% to {s['fully_correct_range'][1]}%.", ""]
    r = runs[0]
    wrong = [d for d in r["detail"] if d["status"] == "wrong"]
    L += ["## What went wrong (first run)", ""]
    if not (wrong or r["extras_detail"] or r["missed_detail"]):
        L += ["Nothing: every labelled product was found exactly.", ""]
    for d in wrong:
        L.append(f"- **{d['label']}**: wrong {', '.join(d['wrong_fields'])}. Read as {d['read_as']}, expected {d['expected']}. "
                 + ("Flagged: " + "; ".join(d["flags"]) if d["flagged"] else "**Not flagged.**"))
    for e in r["extras_detail"]:
        L.append(f"- Extra item ({e['kind']}): {e['read_as']}. " + ("Flagged: " + "; ".join(e["flags"]) if e["flagged"] else "**Not flagged.**"))
    for m in r["missed_detail"]:
        L.append(f"- Missed: {m['name']} ({m['brand'] or 'no brand'}, {m['price']})")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--manual-seconds-per-product", type=float, required=True)
    ap.add_argument("--review-seconds-per-product", type=float, default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", default=str(ROOT / "eval"))
    a = ap.parse_args(argv)
    if a.runs < 1 or a.runs > 10:
        raise SystemExit("--runs must be between 1 and 10 (each run uses credits).")
    if a.manual_seconds_per_product <= 0:
        raise SystemExit("--manual-seconds-per-product must be more than 0: time yourself listing 3 products by hand.")
    a.video, a.labels, a.out = (str(Path(x).resolve()) for x in (a.video, a.labels, a.out))
    if not Path(a.video).is_file():
        raise SystemExit(f"Video not found: {a.video}")
    labels = load_labels(a.labels)
    os.chdir(ROOT)                              # the app keeps its data/ folder relative to the repo root

    sys.path.insert(0, str(ROOT))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    import app.db as db
    db_path = out / "eval_calls.db"
    db.DB_PATH = db_path                        # keep the evaluation's calls out of the app's own report
    from app import shop_config
    business = shop_config.load()["business_type"]

    runs = []
    for n in range(1, a.runs + 1):
        print(f"Run {n}/{a.runs} ...", flush=True)
        r = run_once(a.video, labels, a.notes, db_path, str(ROOT / "data" / "eval_frames"))
        print(f"  {r['items_drafted']} items, found {r['found']}/{r['labels']}, fully correct {r['fully_correct_pct']}%, "
              f"{r['calls']['calls']} calls, {r['pipeline_seconds']} s")
        runs.append(r)
    meta = {"video": a.video, "labels": len(labels), "business_type": business}
    summary = summarise(runs, len(labels), a.manual_seconds_per_product, a.review_seconds_per_product)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "runs": runs}, indent=2, ensure_ascii=False), encoding="utf-8")
    md = to_markdown(summary, runs, meta)
    (out / "results.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"Saved {out / 'results.md'} and {out / 'results.json'}")


if __name__ == "__main__":
    main()