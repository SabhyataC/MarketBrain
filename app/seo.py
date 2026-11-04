"""Listing and SEO engine.

For each product the model proposes SEO text; code checks it; the owner sees a before and after
diff and approves, edits or rejects each change; approved changes are written to the chosen shop
and the previous value is kept so every applied change can be rolled back.

Rules enforced in code, not in the prompt: length limits, no keyword stuffing, no prices that are
not in the catalog, no risky claims, no URL or handle changes, and JSON-LD built from the real
catalog price and stock. Search Console data is optional (CSV import).
"""
import csv
import io
import json
import os
import re
import threading
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, ValidationError
import httpx
from app.audit import log
from app.calllog import purpose
from app.db import get_conn
from app.export_csv import load_catalog, image_url
from app.model_client import ask
from app.products import customer_price
from app.seo_adapters import ADAPTERS, FIELDS, LABELS, MOCK_SQL
from app.shop_sync import SYNC_SQL, SyncError, TIMEOUT
from app import shop_config

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter(prefix="/seo", tags=["seo"])

SQL = """
CREATE TABLE IF NOT EXISTS seo_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    shop TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending approved rejected applied rolled_back failed manual
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT, applied_at TEXT, rolled_back_at TEXT
);
CREATE TABLE IF NOT EXISTS seo_state (
    product_id INTEGER NOT NULL, shop TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (product_id, shop, field)
);
CREATE TABLE IF NOT EXISTS seo_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL, page TEXT, clicks INTEGER, impressions INTEGER, position REAL, product_id INTEGER
);
"""

EDITABLE = ("title", "description", "h1", "alt")
_lock = threading.Lock()
CURRENCIES = {"rs": "INR", "rs.": "INR", "₹": "INR", "inr": "INR", "usd": "USD", "$": "USD",
              "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP", "aed": "AED"}
STOP = set("the and for with that this from your you are our all can has have not but its into more than".split())
RISKY = re.compile(r"\b(guarantee[sd]?|#1|number one|best in|world.?s best|miracle|cure[sd]?|risk.?free|"
                   r"100\s?%|lifetime|no\.?\s?1)\b", re.I)
PRICE_IN_TEXT = re.compile(r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)", re.I)
URLISH = re.compile(r"https?://|www\.", re.I)


def limits() -> dict:
    def num(name, default):
        try:
            v = int(os.environ.get(name, default))
            return v if v > 0 else default
        except ValueError:
            return default
    return {"title": num("SEO_TITLE_MAX", 60), "description": num("SEO_DESC_MAX", 155),
            "h1": num("SEO_H1_MAX", 70), "alt": num("SEO_ALT_MAX", 125)}


def ensure():
    with get_conn() as conn:
        conn.executescript(SQL)
        conn.executescript(SYNC_SQL)
        conn.executescript(MOCK_SQL)


# ---------- what the model must return ----------

class SeoOut(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    title: str
    description: str
    h1: str
    alt: str
    reasons: dict[str, str]


def parse_obj(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in STOP]


def stuffing(text: str, max_repeat: int) -> str | None:
    counts: dict[str, int] = {}
    for w in tokens(text):
        counts[w] = counts.get(w, 0) + 1
    for w, n in counts.items():
        if n > max_repeat:
            return w
    return None


def problems_in(out: dict, facts: dict, lim: dict) -> list[str]:
    """Everything wrong with what the model returned. Empty means it can be proposed."""
    try:
        o = SeoOut.model_validate(out)
    except ValidationError:
        return ["the answer must be a JSON object with title, description, h1, alt and reasons"]
    bad = []
    extra = {k.lower() for k in (o.model_extra or {})}
    if extra & {"url", "handle", "slug", "link", "permalink"}:
        bad.append("do not propose URL, handle or slug changes")
    allowed = {f"{x:g}" for x in facts.get("prices", [])}
    for field in EDITABLE:
        value = getattr(o, field)
        if not value:
            bad.append(f"{field} is empty")
            continue
        if len(value) > lim[field]:
            bad.append(f"{field} is {len(value)} characters; the limit is {lim[field]}")
        if URLISH.search(value):
            bad.append(f"{field} must not contain web addresses")
        if RISKY.search(value) and not RISKY.search(facts.get("text", "")):
            bad.append(f"{field} makes a claim the product facts do not support")
        for amount in PRICE_IN_TEXT.findall(value):
            try:
                if f"{float(amount.replace(',', '')):g}" not in allowed:
                    bad.append(f"{field} shows a price ({amount}) that is not in the catalog")
            except ValueError:
                pass
        if not o.reasons.get(field, "").strip():
            bad.append(f"a one-line reason for {field} is missing")
    for field, max_repeat in (("title", 2), ("description", 3)):
        word = stuffing(getattr(o, field), max_repeat)
        if word:
            bad.append(f"{field} repeats '{word}' too often (keyword stuffing)")
    return bad


PROMPT = """You write search listing text for ONE product page of a {business_type}.
Use ONLY these product facts. Never invent features, materials, prices, offers, shipping or results.
Facts: {facts}
{queries}
Return ONLY a JSON object, no other text:
{{"title": "...", "description": "...", "h1": "...", "alt": "...",
  "reasons": {{"title": "one line why", "description": "one line why", "h1": "one line why", "alt": "one line why"}}}}
Rules:
- title: at most {title} characters; product name first, add the brand if given.
- description: at most {description} characters; one or two plain sentences that make a shopper want to click.
- h1: at most {h1} characters; the heading shown on the product page.
- alt: at most {alt} characters; say literally what the photo shows, no marketing words.
- No keyword stuffing, no all-caps, no emojis, no web addresses.
- Mention a price only if it is exactly {price_hint}. Do not mention discounts, shipping or guarantees.
- Do not change or suggest the product's URL, handle or slug.
{fix}"""


def currency_code(cfg_currency: str) -> str | None:
    c = (cfg_currency or "").strip()
    if c.lower() in CURRENCIES:
        return CURRENCIES[c.lower()]
    return c.upper() if re.fullmatch(r"[A-Za-z]{3}", c) else None


def build_json_ld(entry: dict, description: str, currency: str | None, base: str, url: str = "") -> str | None:
    """Product schema from real catalog data: price the customer pays and live stock."""
    p = entry["product"]
    prices = [customer_price(v["price"], v["cost"]) for _, v in entry["variants"]]
    prices = [x for x in prices if x is not None and x > 0]
    if not prices or not currency:
        return None
    stock = sum(v["stock"] for _, v in entry["variants"])
    offer = {"@type": "Offer", "price": f"{min(prices):.2f}", "priceCurrency": currency,
             "availability": "https://schema.org/InStock" if stock > 0 else "https://schema.org/OutOfStock"}
    if url:
        offer["url"] = url
    data = {"@context": "https://schema.org", "@type": "Product", "name": p["name"],
            "description": description, "sku": f"MB-{p['id']}"}
    if p["brand"]:
        data["brand"] = {"@type": "Brand", "name": p["brand"]}
    if p["category"]:
        data["category"] = p["category"]
    img = image_url(p["photo"], base)
    if img:
        data["image"] = [img]
    data["offers"] = offer
    return json.dumps(data, ensure_ascii=False, indent=2)


def facts_for(entry: dict, cfg) -> dict:
    p = entry["product"]
    attrs = json.loads(p["attrs"] or "{}")
    prices = sorted({x for x in (customer_price(v["price"], v["cost"]) for _, v in entry["variants"]) if x})
    mrps = {v["price"] for _, v in entry["variants"] if v["price"]}
    facts = {"name": p["name"], "brand": p["brand"], "category": p["category"], "description": p["description"]}
    facts.update({k: v for k, v in attrs.items() if v})
    colours = sorted({v["options"].get("colour") or v["options"].get("color") for _, v in entry["variants"]
                      if v["options"].get("colour") or v["options"].get("color")})
    if colours:
        facts["colours"] = colours
    facts["in_stock"] = sum(v["stock"] for _, v in entry["variants"]) > 0
    if prices:
        facts["price"] = f"{cfg['currency']} {prices[0]:g}"
    facts = {k: v for k, v in facts.items() if v not in (None, "", [])}
    return {"facts": facts, "prices": sorted(set(prices) | mrps), "text": json.dumps(facts, ensure_ascii=False)}


def seo_text(entry, cfg, queries: list[dict], lim: dict) -> dict:
    """Ask the model; validate; retry once with the problems; otherwise fail visibly."""
    f = facts_for(entry, cfg)
    q = ""
    if queries:
        q = "Real searches people made that led to this product (use a phrase only if it truly describes it, never stuff):\n" + \
            "\n".join(f'- "{x["query"]}" ({x["impressions"] or 0} impressions, position {x["position"] or "?"})' for x in queries)
    price_hint = f.get("facts", {}).get("price", "not given, so mention no price")
    fix = ""
    last = ["no usable answer"]
    for attempt in range(2):
        prompt = PROMPT.format(business_type=cfg["business_type"], facts=json.dumps(f["facts"], ensure_ascii=False),
                               queries=q, price_hint=price_hint, fix=fix, **lim)
        with purpose("seo"):
            raw = ask(prompt)
        out = parse_obj(raw)
        last = problems_in(out, f, lim) if out is not None else ["the answer was not valid JSON"]
        if not last:
            return out
        fix = "Your previous answer had these problems, fix all of them: " + "; ".join(last)
    raise SyncError("the model's text broke the rules twice: " + "; ".join(last[:3]))


# ---------- helpers ----------

def _shop(name: str):
    a = ADAPTERS.get(name)
    if not a:
        raise HTTPException(404, "Unknown shop. Use mock, webhook, shopify or woocommerce.")
    if not a.configured():
        raise HTTPException(400, f"{a.label} is not set up. Add its settings to .env and restart.")
    return a


def _ctx(conn, adapter, product_id: int) -> dict:
    row = conn.execute("SELECT remote_id, remote_url FROM sync_map WHERE product_id = ? AND shop = ?",
                       (product_id, adapter.name)).fetchone()
    return {"product_id": product_id, "sku": f"MB-{product_id}",
            "remote_id": row["remote_id"] if row else None, "remote_url": (row["remote_url"] if row else "") or ""}


def _client():
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False)


def _current(conn, client, adapter, ctx, product_id: int) -> dict:
    """What the shop holds now: read live when possible, else what we last wrote, else nothing."""
    state = {r["field"]: r["value"] for r in conn.execute(
        "SELECT field, value FROM seo_state WHERE product_id = ? AND shop = ?", (product_id, adapter.name))}
    live = {}
    try:
        live = adapter.read(client, ctx) or {}
    except SyncError:
        live = {}
    return {f: live[f] if live.get(f) is not None else state.get(f, "") for f in FIELDS}, live


def _row(r) -> dict:
    d = dict(r)
    d["label"] = LABELS.get(d["field"], d["field"])
    d["editable"] = d["field"] in EDITABLE and d["status"] == "pending"
    return d


# ---------- routes ----------

@router.get("/config")
def seo_config():
    ensure()
    return {"limits": limits(), "plugin": os.environ.get("SEO_PLUGIN", ""),
            "shops": [{"name": a.name, "label": a.label, "ready": a.configured(),
                       "writes": [f for f in FIELDS if a.writable(f)]} for a in ADAPTERS.values()]}


class GenerateIn(BaseModel):
    shop: str = "mock"
    product_ids: list[int] | None = None


@router.post("/generate", dependencies=[Depends(require_off)])
def generate(body: GenerateIn):
    ensure()
    adapter = _shop(body.shop)
    catalog = load_catalog()
    if body.product_ids is not None:
        catalog = [e for e in catalog if e["product"]["id"] in body.product_ids]
    if not catalog:
        raise HTTPException(404, "No products to write SEO for yet.")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "SEO text is already being generated.")
    try:
        cfg, lim = shop_config.load(), limits()
        base = os.environ.get("SYNC_IMAGE_BASE", "").strip().rstrip("/")
        currency = currency_code(cfg["currency"])
        results = []
        with _client() as client:
            for entry in catalog:
                pid, name = entry["product"]["id"], entry["product"]["name"]
                res = {"product_id": pid, "name": name}
                try:
                    with get_conn() as conn:
                        ctx = _ctx(conn, adapter, pid)
                        current, _ = _current(conn, client, adapter, ctx, pid)
                        qs = [dict(r) for r in conn.execute(
                            "SELECT query, impressions, clicks, position FROM seo_queries WHERE product_id = ? "
                            "ORDER BY impressions DESC LIMIT 5", (pid,))]
                    out = seo_text(entry, cfg, qs, lim)
                except SyncError as e:
                    res.update(status="failed", reason=str(e))
                    results.append(res)
                    continue
                proposals = {f: (out[f].strip() if isinstance(out[f], str) else str(out[f])) for f in EDITABLE}
                reasons = {f: out["reasons"][f].strip() for f in EDITABLE}
                ld = build_json_ld(entry, proposals["description"], currency, base, ctx["remote_url"])
                if ld:
                    proposals["json_ld"] = ld
                    reasons["json_ld"] = "Built from the catalog price and stock, so it always matches the shop."
                created, same = 0, 0
                with get_conn() as conn:
                    conn.execute("UPDATE seo_proposals SET status='rejected', note='replaced by a newer proposal', "
                                 "decided_at=CURRENT_TIMESTAMP WHERE product_id=? AND shop=? AND status='pending'",
                                 (pid, adapter.name))
                    for f, value in proposals.items():
                        if value.strip() == (current.get(f) or "").strip():
                            same += 1
                            continue
                        conn.execute(
                            "INSERT INTO seo_proposals (product_id, shop, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                            (pid, adapter.name, f, current.get(f) or "", value, reasons[f]))
                        created += 1
                res.update(status="proposed" if created else "unchanged", created=created, unchanged=same)
                if not ld:
                    res["note"] = "No JSON-LD: needs a price and a currency in Shop setup."
                results.append(res)
        counts = {k: sum(1 for r in results if r["status"] == k) for k in ("proposed", "unchanged", "failed")}
        log("seo", "seo_generated", f"shop={adapter.name} " + " ".join(f"{k}={v}" for k, v in counts.items()))
        return {"shop": adapter.name, "counts": counts, "results": results}
    finally:
        _lock.release()


@router.get("/proposals")
def list_proposals(status: str | None = None, product_id: int | None = None, shop: str | None = None):
    ensure()
    sql, args = "SELECT * FROM seo_proposals WHERE 1=1", []
    for col, val in (("status", status), ("product_id", product_id), ("shop", shop)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY product_id, id", args).fetchall()
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM products")}
    return [dict(_row(r), product=names.get(r["product_id"], "")) for r in rows]


class EditIn(BaseModel):
    new_value: str


def _get(conn, pid: int):
    r = conn.execute("SELECT * FROM seo_proposals WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "SEO proposal not found")
    return r


@router.put("/proposals/{pid}")
def edit(pid: int, body: EditIn):
    ensure()
    lim = limits()
    with get_conn() as conn:
        r = _get(conn, pid)
        if r["status"] != "pending":
            raise HTTPException(400, f"Already {r['status']}")
        if r["field"] not in EDITABLE:
            raise HTTPException(400, "This field is built from your catalog and cannot be edited here.")
        value = body.new_value.strip()
        if not value:
            raise HTTPException(400, "The text cannot be empty.")
        if len(value) > lim[r["field"]]:
            raise HTTPException(400, f"{LABELS[r['field']]} can be at most {lim[r['field']]} characters (this is {len(value)}).")
        if URLISH.search(value):
            raise HTTPException(400, "Web addresses are not allowed in this field.")
        word = stuffing(value, 2 if r["field"] == "title" else 3)
        if word:
            raise HTTPException(400, f"'{word}' is repeated too often (keyword stuffing).")
        conn.execute("UPDATE seo_proposals SET new_value = ? WHERE id = ?", (value, pid))
    log("owner", "seo_edited", f"proposal {pid}")
    return {"id": pid}


def _decide(pid: int, status: str):
    ensure()
    with get_conn() as conn:
        r = _get(conn, pid)
        if r["status"] != "pending":
            raise HTTPException(400, f"Already {r['status']}")
        conn.execute("UPDATE seo_proposals SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?", (status, pid))
    log("owner", f"seo_{status}", f"proposal {pid} {r['field']} product {r['product_id']}")
    return {"id": pid, "status": status}


@router.post("/proposals/{pid}/approve", dependencies=[Depends(require_off)])
def approve(pid: int):
    return _decide(pid, "approved")


@router.post("/proposals/{pid}/reject")
def reject(pid: int):
    return _decide(pid, "rejected")


class ScopeIn(BaseModel):
    product_id: int | None = None
    shop: str | None = None


@router.post("/approve_all", dependencies=[Depends(require_off)])
def approve_all(body: ScopeIn):
    ensure()
    sql, args = "UPDATE seo_proposals SET status='approved', decided_at=CURRENT_TIMESTAMP WHERE status='pending'", []
    for col, val in (("product_id", body.product_id), ("shop", body.shop)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    with get_conn() as conn:
        n = conn.execute(sql, args).rowcount
    log("owner", "seo_approved_all", f"{n} proposals product={body.product_id}")
    return {"approved": n}


def _write_state(conn, pid, shop, values: dict):
    for f, v in values.items():
        conn.execute("INSERT INTO seo_state (product_id, shop, field, value) VALUES (?,?,?,?) "
                     "ON CONFLICT(product_id, shop, field) DO UPDATE SET value = excluded.value", (pid, shop, f, v))


def _same(a, b) -> bool:
    return (a or "").strip() == (b or "").strip()


def _apply_product(client, pid: int, shop: str) -> list[dict]:
    adapter = ADAPTERS[shop]
    out = []
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM seo_proposals WHERE product_id=? AND shop=? AND status='approved' ORDER BY id",
                            (pid, shop)).fetchall()
        if not rows:
            return out
        ctx = _ctx(conn, adapter, pid)
    entry = next((e for e in load_catalog() if e["product"]["id"] == pid), None)
    try:
        live = adapter.read(client, ctx)
    except SyncError as e:
        for r in rows:
            _set(r, "failed", str(e), out)
        return out
    to_write, writable_rows = {}, []
    desc_new = next((r["new_value"] for r in rows if r["field"] == "description"), None)
    for r in rows:
        f = r["field"]
        if not adapter.writable(f):
            _set(r, "manual", adapter.manual_reason(f), out)
            continue
        if live is not None and live.get(f) is not None and not _same(live[f], r["old_value"]):
            _set(r, "failed", "Changed in the shop since this was proposed. Generate new SEO text to start from the current value.", out)
            continue
        value = r["new_value"]
        if f == "json_ld" and entry:
            cfg, base = shop_config.load(), os.environ.get("SYNC_IMAGE_BASE", "").strip().rstrip("/")
            fresh = build_json_ld(entry, desc_new or json.loads(value).get("description", ""), currency_code(cfg["currency"]), base, ctx["remote_url"])
            if fresh:
                value = fresh
        to_write[f] = value
        writable_rows.append((r, value))
    if not to_write:
        return out
    try:
        adapter.write(client, ctx, to_write)
    except SyncError as e:
        for r, _ in writable_rows:
            _set(r, "failed", str(e), out)
        return out
    except Exception:
        for r, _ in writable_rows:
            _set(r, "failed", "unexpected error while writing to the shop", out)
        return out
    with get_conn() as conn:
        for r, value in writable_rows:
            conn.execute("UPDATE seo_proposals SET status='applied', new_value=?, note='', applied_at=CURRENT_TIMESTAMP WHERE id=?",
                         (value, r["id"]))
            out.append({"id": r["id"], "field": r["field"], "status": "applied"})
        _write_state(conn, pid, shop, to_write)
    log("owner", "seo_applied", f"product {pid} shop {shop} fields " + ",".join(sorted(to_write)))
    return out


def _set(r, status, note, out):
    with get_conn() as conn:
        conn.execute("UPDATE seo_proposals SET status=?, note=? WHERE id=?", (status, note, r["id"]))
    out.append({"id": r["id"], "field": r["field"], "status": status, "note": note})
    return out[-1]


@router.post("/apply", dependencies=[Depends(require_off)])
def apply(body: ScopeIn):
    ensure()
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "An SEO change is already being applied.")
    try:
        with get_conn() as conn:
            sql, args = "SELECT DISTINCT product_id, shop FROM seo_proposals WHERE status='approved'", []
            for col, val in (("product_id", body.product_id), ("shop", body.shop)):
                if val is not None:
                    sql += f" AND {col} = ?"
                    args.append(val)
            pairs = [(r["product_id"], r["shop"]) for r in conn.execute(sql, args)]
        results = []
        with _client() as client:
            for pid, shop in pairs:
                if shop in ADAPTERS and ADAPTERS[shop].configured():
                    results += _apply_product(client, pid, shop)
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"counts": counts, "results": results}
    finally:
        _lock.release()


def _rollback_rows(rows) -> list[dict]:
    out = []
    by = {}
    for r in rows:
        by.setdefault((r["product_id"], r["shop"]), []).append(r)
    with _client() as client:
        for (pid, shop), group in by.items():
            adapter = ADAPTERS[shop]
            with get_conn() as conn:
                ctx = _ctx(conn, adapter, pid)
            try:
                live = adapter.read(client, ctx)
            except SyncError as e:
                for r in group:
                    _set(r, "applied", f"Rollback failed: {e}", out)
                continue
            todo = {}
            for r in group:
                f = r["field"]
                if live is not None and live.get(f) is not None and not _same(live[f], r["new_value"]):
                    _set(r, "applied", "Not rolled back: the shop value was changed after this was applied.", out)
                    continue
                todo[f] = r["old_value"]
            if not todo:
                continue
            try:
                adapter.write(client, ctx, todo)
            except SyncError as e:
                for r in group:
                    if r["field"] in todo:
                        _set(r, "applied", f"Rollback failed: {e}", out)
                continue
            with get_conn() as conn:
                for r in group:
                    if r["field"] in todo:
                        conn.execute("UPDATE seo_proposals SET status='rolled_back', note='', rolled_back_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
                        out.append({"id": r["id"], "field": r["field"], "status": "rolled_back"})
                _write_state(conn, pid, shop, todo)
            log("owner", "seo_rolled_back", f"product {pid} shop {shop} fields " + ",".join(sorted(todo)))
    return out


@router.post("/proposals/{pid}/rollback", dependencies=[Depends(require_off)])
def rollback_one(pid: int):
    ensure()
    with get_conn() as conn:
        r = _get(conn, pid)
    if r["status"] != "applied":
        raise HTTPException(400, f"Only applied changes can be rolled back (this one is {r['status']}).")
    res = _rollback_rows([r])
    if res and res[0]["status"] != "rolled_back":
        raise HTTPException(409, res[0].get("note", "Could not roll back."))
    return {"id": pid, "status": "rolled_back"}


@router.post("/rollback", dependencies=[Depends(require_off)])
def rollback(body: ScopeIn):
    ensure()
    sql, args = "SELECT * FROM seo_proposals WHERE status='applied'", []
    for col, val in (("product_id", body.product_id), ("shop", body.shop)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    res = _rollback_rows(rows)
    counts = {}
    for r in res:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"counts": counts, "results": res}


# ---------- Search Console data ----------

def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _col(header: list[str], names: tuple) -> int | None:
    for i, h in enumerate(header):
        if h.strip().lower() in names:
            return i
    return None


def _product_for(row_query: str, row_page: str, products: list[dict]) -> int | None:
    page = (row_page or "").lower()
    if page:
        hits = [p["id"] for p in products if p["slug"] and p["slug"] in page]
        if len(hits) == 1:
            return hits[0]
    q = set(tokens(row_query))
    best, best_score, tie = None, 0, False
    for p in products:
        score = len(q & p["tokens"])
        if score and score >= max(1, min(2, len(p["tokens"]))):
            if score > best_score:
                best, best_score, tie = p["id"], score, False
            elif score == best_score:
                tie = True
    return None if tie else best


@router.post("/queries/import")
async def import_queries(file: UploadFile = File(...)):
    ensure()
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(400, "That file is too large (2 MB at most).")
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise HTTPException(400, "The file has no rows.")
    header = rows[0]
    qi = _col(header, ("query", "queries", "top queries", "search query"))
    if qi is None:
        raise HTTPException(400, "Could not find a 'Query' or 'Top queries' column. Use the Queries export from Search Console.")
    pi = _col(header, ("page", "pages", "top pages", "url", "landing page"))
    ci = _col(header, ("clicks",))
    ii = _col(header, ("impressions",))
    oi = _col(header, ("position", "average position"))
    products = []
    for e in load_catalog():
        p = e["product"]
        slug = re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-")
        products.append({"id": p["id"], "slug": slug, "tokens": set(tokens(f"{p['name']} {p['brand'] or ''}"))})
    saved, matched = [], 0
    for r in rows[1:5001]:
        if len(r) <= qi or not r[qi].strip():
            continue
        get = lambda i: r[i] if i is not None and i < len(r) else ""
        pid = _product_for(r[qi], get(pi), products)
        matched += pid is not None
        clicks, imps, pos = _num(get(ci)), _num(get(ii)), _num(get(oi))
        saved.append((r[qi].strip()[:200], get(pi)[:300], int(clicks) if clicks is not None else None,
                      int(imps) if imps is not None else None, pos, pid))
    with get_conn() as conn:
        conn.execute("DELETE FROM seo_queries")
        conn.executemany("INSERT INTO seo_queries (query, page, clicks, impressions, position, product_id) VALUES (?,?,?,?,?,?)", saved)
    log("owner", "seo_queries_imported", f"{len(saved)} rows, {matched} matched to a product")
    return {"rows": len(saved), "matched": matched, "unmatched": len(saved) - matched}


@router.get("/queries/summary")
def queries_summary():
    ensure()
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM seo_queries").fetchone()[0]
        matched = conn.execute("SELECT COUNT(*) FROM seo_queries WHERE product_id IS NOT NULL").fetchone()[0]
    return {"rows": total, "matched": matched}
