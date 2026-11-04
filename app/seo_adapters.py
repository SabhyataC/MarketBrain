"""How SEO text is read from and written to each kind of shop.

Every adapter says which fields it can write, so the engine never pretends. A field that a
platform cannot write through its API (for example JSON-LD, which usually lives in the theme)
is reported as "manual" and the owner copies it. Shopify and WooCommerce calls follow their
public REST docs but were only tested here against mock servers.
"""
import json
import os
import time
from app.db import get_conn
from app.shop_sync import (SyncError, request, fail_message, config, check_url, shopify_base, sign)

FIELDS = ("title", "description", "h1", "alt", "json_ld")
LABELS = {"title": "SEO title", "description": "Meta description", "h1": "Main heading (H1)",
          "alt": "Image alt text", "json_ld": "Product schema (JSON-LD)"}

MOCK_SQL = """
CREATE TABLE IF NOT EXISTS mock_shop (
    product_id INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (product_id, field)
);
"""

WOO_KEYS = {
    "yoast": {"title": "_yoast_wpseo_title", "description": "_yoast_wpseo_metadesc"},
    "rankmath": {"title": "rank_math_title", "description": "rank_math_description"},
}


class Adapter:
    name = ""
    label = ""
    why_not = {}

    def writable(self, field: str) -> bool:
        return True

    def manual_reason(self, field: str) -> str:
        return self.why_not.get(field, "This shop cannot be written to for this field.")

    def configured(self) -> bool:
        return True

    def read(self, client, ctx) -> dict | None:
        """Current values in the shop, or None when this shop cannot be read."""
        return None

    def write(self, client, ctx, values: dict) -> None:
        raise NotImplementedError


class MockAdapter(Adapter):
    """A test store kept in the local database, so apply and rollback can be shown with no real shop."""
    name, label = "mock", "Test store (local)"

    def read(self, client, ctx):
        with get_conn() as conn:
            conn.executescript(MOCK_SQL)
            rows = conn.execute("SELECT field, value FROM mock_shop WHERE product_id = ?", (ctx["product_id"],)).fetchall()
        got = {r["field"]: r["value"] for r in rows}
        return {f: got.get(f, "") for f in FIELDS}

    def write(self, client, ctx, values):
        with get_conn() as conn:
            conn.executescript(MOCK_SQL)
            for f, v in values.items():
                conn.execute(
                    "INSERT INTO mock_shop (product_id, field, value) VALUES (?,?,?) "
                    "ON CONFLICT(product_id, field) DO UPDATE SET value = excluded.value",
                    (ctx["product_id"], f, v))


class WebhookAdapter(Adapter):
    """Your own site: a signed POST that your code stores. The shop decides what to do with each field."""
    name, label = "webhook", "Your own site"

    def configured(self):
        return all(config("webhook").values())

    def write(self, client, ctx, values):
        c = config("webhook")
        check_url(c["url"], "WEBHOOK_URL")
        body = json.dumps({"event": "seo.update", "sku": ctx["sku"], "fields": values},
                          separators=(",", ":"), ensure_ascii=False).encode()
        ts = str(int(time.time()))
        r = request(client, "POST", c["url"], content=body, headers={
            "Content-Type": "application/json", "X-MB-Timestamp": ts, "X-MB-Signature": sign(c["secret"], ts, body)})
        if r.status_code >= 400:
            raise SyncError(fail_message(r))


class ShopifyAdapter(Adapter):
    name, label = "shopify", "Shopify"
    why_not = {
        "h1": "Shopify uses the product title as the heading. Change the title in the product, or edit the theme.",
        "json_ld": "Shopify adds product schema from the theme. Copy this into the theme if you need to change it.",
    }

    def writable(self, field):
        return field in ("title", "description", "alt")

    def configured(self):
        return all(config("shopify").values())

    def _base(self):
        c = config("shopify")
        base = shopify_base(c)
        check_url(base, "SHOPIFY_STORE")
        return base, {"X-Shopify-Access-Token": c["token"], "Content-Type": "application/json"}

    def _remote(self, ctx):
        if not ctx.get("remote_id"):
            raise SyncError("Send this product to Shopify first (Export tab), then apply its SEO.")
        return ctx["remote_id"]

    def read(self, client, ctx):
        base, h = self._base()
        rid = self._remote(ctx)
        out = {}
        r = request(client, "GET", f"{base}/products/{rid}/metafields.json", headers=h, params={"namespace": "global"})
        if r.status_code >= 400:
            raise SyncError(fail_message(r))
        for m in r.json().get("metafields", []):
            if m.get("key") == "title_tag":
                out["title"] = m.get("value") or ""
            if m.get("key") == "description_tag":
                out["description"] = m.get("value") or ""
        out.setdefault("title", ""), out.setdefault("description", "")
        r = request(client, "GET", f"{base}/products/{rid}.json", headers=h)
        if r.status_code >= 400:
            raise SyncError(fail_message(r))
        imgs = r.json().get("product", {}).get("images") or []
        if imgs:
            out["alt"] = imgs[0].get("alt") or ""
            ctx["image_id"] = imgs[0].get("id")
        return out

    def write(self, client, ctx, values):
        base, h = self._base()
        rid = self._remote(ctx)
        body = {"id": int(rid)}
        if "title" in values:
            body["metafields_global_title_tag"] = values["title"]
        if "description" in values:
            body["metafields_global_description_tag"] = values["description"]
        if len(body) > 1:
            r = request(client, "PUT", f"{base}/products/{rid}.json", headers=h, json={"product": body})
            if r.status_code >= 400:
                raise SyncError(fail_message(r))
        if "alt" in values:
            if not ctx.get("image_id"):
                self.read(client, ctx)
            if not ctx.get("image_id"):
                raise SyncError("This product has no image in Shopify, so alt text cannot be set.")
            r = request(client, "PUT", f"{base}/products/{rid}/images/{ctx['image_id']}.json", headers=h,
                        json={"image": {"id": ctx["image_id"], "alt": values["alt"]}})
            if r.status_code >= 400:
                raise SyncError(fail_message(r))


class WooAdapter(Adapter):
    name, label = "woocommerce", "WooCommerce"
    why_not = {
        "h1": "WooCommerce uses the product name as the heading. Change the name in the product, or edit the theme.",
        "json_ld": "WooCommerce adds product schema itself. Copy this into the theme if you need to change it.",
        "title": "Set SEO_PLUGIN=yoast or SEO_PLUGIN=rankmath in .env so SEO titles can be written.",
        "description": "Set SEO_PLUGIN=yoast or SEO_PLUGIN=rankmath in .env so meta descriptions can be written.",
    }

    def _plugin(self):
        return os.environ.get("SEO_PLUGIN", "").strip().lower()

    def writable(self, field):
        if field == "alt":
            return True
        return field in ("title", "description") and self._plugin() in WOO_KEYS

    def configured(self):
        return all(config("woocommerce").values())

    def _api(self):
        c = config("woocommerce")
        check_url(c["url"], "WOO_URL")
        return c["url"] + "/wp-json/wc/v3", (c["key"], c["secret"])

    def _remote(self, ctx):
        if not ctx.get("remote_id"):
            raise SyncError("Send this product to WooCommerce first (Export tab), then apply its SEO.")
        return ctx["remote_id"]

    def read(self, client, ctx):
        api, auth = self._api()
        rid = self._remote(ctx)
        r = request(client, "GET", f"{api}/products/{rid}", auth=auth)
        if r.status_code >= 400:
            raise SyncError(fail_message(r))
        data = r.json()
        meta = {m.get("key"): m.get("value") for m in data.get("meta_data", [])}
        out = {}
        keys = WOO_KEYS.get(self._plugin())
        if keys:
            out["title"] = meta.get(keys["title"]) or ""
            out["description"] = meta.get(keys["description"]) or ""
        imgs = data.get("images") or []
        if imgs:
            out["alt"] = imgs[0].get("alt") or ""
            ctx["image_id"] = imgs[0].get("id")
        return out

    def write(self, client, ctx, values):
        api, auth = self._api()
        rid = self._remote(ctx)
        body = {}
        keys = WOO_KEYS.get(self._plugin(), {})
        meta = [{"key": keys[f], "value": values[f]} for f in ("title", "description") if f in values and f in keys]
        if meta:
            body["meta_data"] = meta
        if "alt" in values:
            if not ctx.get("image_id"):
                self.read(client, ctx)
            if not ctx.get("image_id"):
                raise SyncError("This product has no image in WooCommerce, so alt text cannot be set.")
            body["images"] = [{"id": ctx["image_id"], "alt": values["alt"]}]
        if body:
            r = request(client, "PUT", f"{api}/products/{rid}", auth=auth, json=body)
            if r.status_code >= 400:
                raise SyncError(fail_message(r))


ADAPTERS = {a.name: a for a in (MockAdapter(), WebhookAdapter(), ShopifyAdapter(), WooAdapter())}
