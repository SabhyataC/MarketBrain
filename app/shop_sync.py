"""Send catalog products to a shop: a custom site (signed webhook), Shopify or WooCommerce.

The owner presses the button; nothing is sent on its own. Products always arrive as
DRAFTS (unless live=true), so the owner publishes in the shop. Keys come from the
environment, are never logged and never returned by the API. A small table remembers
which remote product each local product became, so pressing the button twice updates
instead of duplicating.

Shopify and WooCommerce calls follow their public REST docs but have only been tested
here against local mock servers, not against a real shop.
"""
import hashlib
import hmac
import html
import json
import os
import threading
import time
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, HTTPException, Query
from app.audit import log
from app.db import get_conn
from app.export_csv import load_catalog, option_names, image_url, money, price_pair, MAX_OPTIONS

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter(prefix="/sync", tags=["sync"])

SHOPS = ("webhook", "shopify", "woocommerce")
TIMEOUT = 20
RETRIES = 3
_locks = {s: threading.Lock() for s in SHOPS}

SYNC_SQL = """
CREATE TABLE IF NOT EXISTS sync_map (
    product_id INTEGER NOT NULL,
    shop TEXT NOT NULL,
    remote_id TEXT,
    remote_url TEXT,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (product_id, shop)
);
"""


class SyncError(Exception):
    """A failure with a message that is safe to show (never contains keys)."""


# ---------- configuration ----------

def config(shop: str) -> dict:
    e = os.environ.get
    if shop == "webhook":
        return {"url": e("WEBHOOK_URL", "").strip(), "secret": e("WEBHOOK_SECRET", "")}
    if shop == "shopify":
        return {"store": e("SHOPIFY_STORE", "").strip(), "token": e("SHOPIFY_TOKEN", ""),
                "version": e("SHOPIFY_API_VERSION", "2025-01").strip()}
    return {"url": e("WOO_URL", "").strip().rstrip("/"), "key": e("WOO_KEY", ""), "secret": e("WOO_SECRET", "")}


def configured(shop: str) -> bool:
    return all(config(shop).values())


def check_url(url: str, what: str) -> str:
    """Keys travel in these requests, so only https (or localhost for testing) is allowed."""
    u = urlparse(url)
    local = u.hostname in ("localhost", "127.0.0.1")
    if u.scheme not in ("http", "https") or not u.hostname or (u.scheme == "http" and not local):
        raise SyncError(f"{what} must be an https:// address.")
    return url


def shopify_base(c) -> str:
    store = c["store"].replace("https://", "").replace("http://", "").strip("/")
    if "/" in store or " " in store:
        raise SyncError("SHOPIFY_STORE should look like my-store.myshopify.com")
    scheme = "http" if store.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{store}/admin/api/{c['version']}"


# ---------- http with limited retries ----------

def request(client, method, url, **kw):
    """One call. Retries timeouts, 429 and 5xx a few times; never follows redirects (keys!)."""
    last = None
    for attempt in range(RETRIES):
        try:
            r = client.request(method, url, **kw)
        except httpx.TimeoutException:
            last = "the shop took too long to answer"
        except httpx.HTTPError:
            last = "could not reach the shop"
        else:
            if r.status_code == 429 or r.status_code >= 500:
                last = f"the shop answered {r.status_code}"
                wait = min(float(r.headers.get("Retry-After", 0) or 0), 5)
                if attempt < RETRIES - 1:
                    time.sleep(wait or 0.5 * (attempt + 1))
                continue
            if 300 <= r.status_code < 400:
                raise SyncError("the shop address redirected; use its final address")
            return r
        if attempt < RETRIES - 1:
            time.sleep(0.5 * (attempt + 1))
    raise SyncError(last or "the shop did not answer")


def fail_message(r) -> str:
    """Short, key-free reason from a shop's error answer."""
    try:
        body = r.json()
        text = body.get("message") or body.get("errors") or body.get("error") or ""
        text = text if isinstance(text, str) else json.dumps(text)
    except Exception:
        text = ""
    if r.status_code in (401, 403):
        return f"the shop refused the key ({r.status_code}); check the key and its permissions"
    return f"the shop answered {r.status_code}" + (f": {text[:200]}" if text else "")


# ---------- product to payload ----------

def product_dict(entry, base="") -> dict:
    p = entry["product"]
    names = option_names(entry["variants"])[:MAX_OPTIONS]
    return {
        "sku": f"MB-{p['id']}", "name": p["name"], "brand": p["brand"], "category": p["category"],
        "description": p["description"], "image": image_url(p["photo"], base),
        "attrs": json.loads(p["attrs"] or "{}"),
        "options": names,
        "variants": [
            {"sku": f"MB-{p['id']}-{vid}", "price": price_pair(v)[0], "mrp": price_pair(v)[1] or price_pair(v)[0],
             "stock": v["stock"], "options": {n: v["options"].get(n, "") for n in names}}
            for vid, v in entry["variants"]
        ],
    }


def problem(pd) -> str | None:
    if any(v["price"] is None for v in pd["variants"]):
        return "no MRP or cost (fill one in first)"
    return None


# ---------- adapters: each returns (remote_id, remote_url) ----------

def sign(secret: str, ts: str, body: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def send_webhook(client, c, pd, known, live):
    check_url(c["url"], "WEBHOOK_URL")
    body = json.dumps({"event": "product.upsert", "live": live, "product": pd},
                      separators=(",", ":"), ensure_ascii=False).encode()
    ts = str(int(time.time()))
    r = request(client, "POST", c["url"], content=body, headers={
        "Content-Type": "application/json", "X-MB-Timestamp": ts,
        "X-MB-Signature": sign(c["secret"], ts, body)})
    if r.status_code >= 400:
        raise SyncError(fail_message(r))
    try:
        data = r.json()
    except Exception:
        data = {}
    return str(data.get("id", "") or ""), str(data.get("url", "") or "")


def _html(text):
    return "<p>" + html.escape(text or "").replace("\n", "<br>") + "</p>" if text else ""


def send_shopify(client, c, pd, known, live):
    check_url(shopify_base(c), "SHOPIFY_STORE")
    base = shopify_base(c)
    headers = {"X-Shopify-Access-Token": c["token"], "Content-Type": "application/json"}
    status = "active" if live else "draft"
    top = {"title": pd["name"], "body_html": _html(pd["description"]), "vendor": pd["brand"] or "",
           "product_type": pd["category"] or "",
           "tags": ", ".join(x for x in [pd["category"], pd["brand"]] if x), "status": status}
    if known:   # update product-level fields only; variants are left as they are in the shop
        r = request(client, "PUT", f"{base}/products/{known}.json", headers=headers,
                    json={"product": {"id": int(known), **top}})
        if r.status_code != 404:
            if r.status_code >= 400:
                raise SyncError(fail_message(r))
            return known, ""
    variants = []
    for v in pd["variants"]:
        row = {"sku": v["sku"], "price": money(v["price"]), "inventory_management": "shopify"}
        if v["mrp"] is not None and v["mrp"] > v["price"]:
            row["compare_at_price"] = money(v["mrp"])
        for i, n in enumerate(pd["options"], 1):
            row[f"option{i}"] = v["options"][n]
        variants.append(row)
    body = dict(top, variants=variants)
    if pd["options"]:
        body["options"] = [{"name": n.title()} for n in pd["options"]]
    if pd["image"]:
        body["images"] = [{"src": pd["image"]}]
    r = request(client, "POST", f"{base}/products.json", headers=headers, json={"product": body})
    if r.status_code >= 400:
        raise SyncError(fail_message(r))
    try:
        rid = str(r.json()["product"]["id"])
    except Exception:
        raise SyncError("the shop accepted the product but did not return its id")
    return rid, ""


def send_woo(client, c, pd, known, live):
    check_url(c["url"], "WOO_URL")
    api = c["url"] + "/wp-json/wc/v3"
    auth = (c["key"], c["secret"])
    status = "publish" if live else "draft"
    variable = bool(pd["options"])
    top = {"name": pd["name"], "status": status, "description": pd["description"] or "",
           "categories": [{"name": pd["category"]}] if pd["category"] else []}
    rid, url = "", ""
    if known:
        r = request(client, "PUT", f"{api}/products/{known}", auth=auth, json=top)
        if r.status_code != 404:
            if r.status_code >= 400:
                raise SyncError(fail_message(r))
            rid = known
    if not rid:
        body = dict(top, sku=pd["sku"], type="variable" if variable else "simple")
        if pd["image"]:
            body["images"] = [{"src": pd["image"]}]
        if variable:
            body["attributes"] = [
                {"name": n.title(), "visible": True, "variation": True,
                 "options": sorted({v["options"][n] for v in pd["variants"] if v["options"][n]})}
                for n in pd["options"]]
        else:
            v = pd["variants"][0]
            body.update(regular_price=money(v["mrp"]), sale_price=money(v["price"]) if v["mrp"] > v["price"] else "",
                        manage_stock=True, stock_quantity=v["stock"])
        r = request(client, "POST", f"{api}/products", auth=auth, json=body)
        if r.status_code >= 400:
            raise SyncError(fail_message(r))
        try:
            data = r.json()
            rid, url = str(data["id"]), str(data.get("permalink", "") or "")
        except Exception:
            raise SyncError("the shop accepted the product but did not return its id")
    elif not variable:
        v = pd["variants"][0]
        r = request(client, "PUT", f"{api}/products/{rid}", auth=auth,
                    json={"regular_price": money(v["mrp"]), "sale_price": money(v["price"]) if v["mrp"] > v["price"] else "",
                          "manage_stock": True, "stock_quantity": v["stock"]})
        if r.status_code >= 400:
            raise SyncError(fail_message(r))
    if variable:
        existing = {}
        if known and rid == known:
            r = request(client, "GET", f"{api}/products/{rid}/variations", auth=auth, params={"per_page": 100})
            if r.status_code < 400:
                existing = {x.get("sku"): x["id"] for x in r.json() if x.get("sku")}
        create, update = [], []
        for v in pd["variants"]:
            row = {"sku": v["sku"], "regular_price": money(v["mrp"]),
                   "sale_price": money(v["price"]) if v["mrp"] > v["price"] else "", "manage_stock": True,
                   "stock_quantity": v["stock"],
                   "attributes": [{"name": n.title(), "option": v["options"][n]} for n in pd["options"]]}
            if v["sku"] in existing:
                update.append(dict(row, id=existing[v["sku"]]))
            else:
                create.append(row)
        r = request(client, "POST", f"{api}/products/{rid}/variations/batch", auth=auth,
                    json={"create": create, "update": update})
        if r.status_code >= 400:
            raise SyncError("product saved, but its variants failed: " + fail_message(r))
        try:
            errs = [x for k in ("create", "update") for x in r.json().get(k, []) if "error" in x]
        except Exception:
            errs = []
        if errs:
            raise SyncError(f"product saved, but {len(errs)} variant(s) were rejected")
    return rid, url


SENDERS = {"webhook": send_webhook, "shopify": send_shopify, "woocommerce": send_woo}


# ---------- routes ----------

@router.get("/config")
def sync_config():
    """Which shops are set up. Never returns the keys themselves."""
    return {s: configured(s) for s in SHOPS}


@router.post("/{shop}", dependencies=[Depends(require_off)])
def sync(shop: str, product_id: int | None = None, live: bool = Query(False)):
    if shop not in SHOPS:
        raise HTTPException(404, "Unknown shop. Use webhook, shopify or woocommerce.")
    if not configured(shop):
        raise HTTPException(400, f"{shop} is not set up. Add its settings to .env and restart.")
    if not _locks[shop].acquire(blocking=False):
        raise HTTPException(409, "A sync to this shop is already running.")
    try:
        catalog = load_catalog()
        if product_id is not None:
            catalog = [e for e in catalog if e["product"]["id"] == product_id]
            if not catalog:
                raise HTTPException(404, "Product not found")
        if not catalog:
            raise HTTPException(404, "No products to send yet.")
        c, base = config(shop), os.environ.get("SYNC_IMAGE_BASE", "").strip().rstrip("/")
        results = []
        with get_conn() as conn:
            conn.executescript(SYNC_SQL)
            known = {r["product_id"]: r["remote_id"] for r in conn.execute(
                "SELECT product_id, remote_id FROM sync_map WHERE shop = ?", (shop,))}
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            for entry in catalog:
                pid, pd = entry["product"]["id"], product_dict(entry, base)
                row = {"product_id": pid, "name": pd["name"]}
                why = problem(pd)
                if why:
                    results.append(dict(row, status="skipped", reason=why))
                    continue
                try:
                    rid, url = SENDERS[shop](client, c, pd, known.get(pid) or None, live)
                except SyncError as e:
                    results.append(dict(row, status="failed", reason=str(e)))
                    continue
                except Exception:
                    results.append(dict(row, status="failed", reason="unexpected error; nothing else was sent for this product"))
                    continue
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO sync_map (product_id, shop, remote_id, remote_url) VALUES (?,?,?,?) "
                        "ON CONFLICT(product_id, shop) DO UPDATE SET remote_id=excluded.remote_id, "
                        "remote_url=excluded.remote_url, synced_at=CURRENT_TIMESTAMP",
                        (pid, shop, rid, url))
                results.append(dict(row, status="updated" if pid in known else "sent", remote_id=rid, url=url))
        counts = {k: sum(1 for r in results if r["status"] == k) for k in ("sent", "updated", "skipped", "failed")}
        log("owner", "shop_sync", f"shop={shop} live={live} " + " ".join(f"{k}={v}" for k, v in counts.items()))
        return {"shop": shop, "live": live, "counts": counts, "results": results}
    finally:
        _locks[shop].release()
