"""CSV export of the catalog for Shopify and WooCommerce.

Read-only: builds files from the products and variants already in the database.
Nothing is sent anywhere. Products are exported as DRAFT by default so nothing goes
live on the shop by accident. Photos are local, so image columns stay empty unless
the owner gives the public address the photos will be hosted at (image_base).
"""
import csv
import io
import json
import re
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from app.db import get_conn
from app.products import resolve, customer_price

router = APIRouter(prefix="/export", tags=["export"])

MAX_OPTIONS = 3  # both shops allow at most 3 option/attribute columns for variants

SHOPIFY_COLUMNS = [
    "Title", "URL handle", "Description", "Vendor", "Type", "Tags", "Status", "SKU",
    "Option1 name", "Option1 value", "Option2 name", "Option2 value",
    "Option3 name", "Option3 value", "Price", "Compare-at price",
    "Inventory tracker", "Inventory quantity", "Continue selling when out of stock",
    "Product image URL", "Image position",
]

WOO_COLUMNS = [
    "ID", "Type", "SKU", "Name", "Published", "Description", "In stock?", "Stock",
    "Regular price", "Sale price", "Categories", "Images", "Parent",
    "Attribute 1 name", "Attribute 1 value(s)", "Attribute 1 visible", "Attribute 1 global",
    "Attribute 2 name", "Attribute 2 value(s)", "Attribute 2 visible", "Attribute 2 global",
    "Attribute 3 name", "Attribute 3 value(s)", "Attribute 3 visible", "Attribute 3 global",
]


def safe(value) -> str:
    """Text for a cell. A leading = + - @ would run as a formula in Excel, so neutralise it."""
    s = "" if value is None else str(value).replace("\r", " ").strip()
    if s and s[0] in "=+-@\t":
        s = "'" + s
    return s


def money(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def slug(text) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:80] or "product"


def clean_base(value) -> str:
    s = (value or "").strip().rstrip("/")
    if not s:
        return ""
    if not re.match(r"^https?://[^\s]+$", s, re.I):
        raise HTTPException(400, "The image address must start with http:// or https:// and have no spaces.")
    return s


def image_url(photo, base) -> str:
    """Public image address, only when the owner told us where the photos will be hosted."""
    if not base or not photo:
        return ""
    if re.match(r"^https?://", photo, re.I):
        return photo
    return base + "/" + photo.lstrip("/")


def load_catalog() -> list[dict]:
    """Each product with its variants resolved, and the warnings found along the way."""
    out = []
    with get_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
        for p in products:
            variants = conn.execute(
                "SELECT * FROM variants WHERE product_id = ? ORDER BY id", (p["id"],)
            ).fetchall()
            out.append({"product": dict(p), "variants": [(v["id"], resolve(p, v)) for v in variants]})
    return out


def option_names(variants) -> list[str]:
    names = []
    for _, v in variants:
        for k in v["options"]:
            if k not in names:
                names.append(k)
    return sorted(names)


def warnings_for(catalog) -> list[str]:
    warns = []
    for entry in catalog:
        p = entry["product"]
        label = f'"{p["name"]}"'
        if len(option_names(entry["variants"])) > MAX_OPTIONS:
            warns.append(f"{label} has more than {MAX_OPTIONS} option types; only the first {MAX_OPTIONS} are exported.")
        if any(customer_price(v["price"], v["cost"]) is None for _, v in entry["variants"]):
            warns.append(f"{label} has no MRP or cost; the price column is empty and must be filled in before import.")
    return warns


def _handles(catalog) -> dict:
    used, out = set(), {}
    for entry in catalog:
        base = slug(entry["product"]["name"])
        h, n = base, 2
        while h in used:
            h, n = f"{base}-{n}", n + 1
        used.add(h)
        out[entry["product"]["id"]] = h
    return out


def price_pair(v):
    """(price the customer pays, MRP to show as the old price or None)."""
    sell = customer_price(v["price"], v["cost"])
    mrp = v["price"] if sell is not None and v["price"] is not None and v["price"] > sell else None
    return sell, mrp


def _sku(pid, vid) -> str:
    return f"MB-{pid}-{vid}"


def _tags(item) -> str:
    return ", ".join(safe(x) for x in [item.get("category"), item.get("brand")] if x)


def _write(columns, rows) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads Indian/accented text


def shopify_rows(catalog, base="", status="draft") -> list[dict]:
    handles = _handles(catalog)
    rows = []
    for entry in catalog:
        p = entry["product"]
        names = option_names(entry["variants"])[:MAX_OPTIONS]
        image = image_url(p["photo"], base)
        for i, (vid, v) in enumerate(entry["variants"]):
            row = {
                "URL handle": handles[p["id"]],
                "SKU": _sku(p["id"], vid),
                "Price": money(price_pair(v)[0]),
                "Compare-at price": money(price_pair(v)[1]),
                "Inventory tracker": "shopify",
                "Inventory quantity": v["stock"],
                "Continue selling when out of stock": "deny",
            }
            if names:
                for n, name in enumerate(names, 1):
                    row[f"Option{n} name"] = safe(name.title())
                    row[f"Option{n} value"] = safe(v["options"].get(name, ""))
            else:
                row["Option1 name"], row["Option1 value"] = "Title", "Default Title"
            if i == 0:  # product-level fields only on the first row of the product
                row.update({
                    "Title": safe(p["name"]), "Description": safe(v["description"] or p["description"]),
                    "Vendor": safe(p["brand"]), "Type": safe(p["category"]),
                    "Tags": _tags(p), "Status": status,
                    "Product image URL": image, "Image position": 1 if image else "",
                })
            rows.append(row)
    return rows


def woo_rows(catalog, base="", published=-1) -> list[dict]:
    rows = []
    for entry in catalog:
        p = entry["product"]
        names = option_names(entry["variants"])[:MAX_OPTIONS]
        image = image_url(p["photo"], base)
        common = {
            "Name": safe(p["name"]), "Published": published,
            "Description": safe(p["description"]), "Categories": safe(p["category"]),
        }
        if not names:
            vid, v = entry["variants"][0]
            rows.append({**common, "Type": "simple", "SKU": _sku(p["id"], vid),
                         "In stock?": 1 if v["stock"] > 0 else 0, "Stock": v["stock"],
                         "Regular price": money(price_pair(v)[1] or price_pair(v)[0]),
                         "Sale price": money(price_pair(v)[0]) if price_pair(v)[1] else "", "Images": image})
            continue
        parent_sku = f"MB-{p['id']}"
        parent = {**common, "Type": "variable", "SKU": parent_sku, "Images": image,
                  "In stock?": 1 if any(v["stock"] > 0 for _, v in entry["variants"]) else 0}
        for n, name in enumerate(names, 1):
            values = []
            for _, v in entry["variants"]:
                val = v["options"].get(name)
                if val and val not in values:
                    values.append(val)
            parent[f"Attribute {n} name"] = safe(name.title())
            parent[f"Attribute {n} value(s)"] = ", ".join(safe(x) for x in values)
            parent[f"Attribute {n} visible"] = 1
            parent[f"Attribute {n} global"] = 0
        rows.append(parent)
        for vid, v in entry["variants"]:  # variations follow their parent
            row = {"Type": "variation", "SKU": _sku(p["id"], vid), "Parent": parent_sku,
                   "Name": safe(p["name"]), "Published": published,
                   "In stock?": 1 if v["stock"] > 0 else 0, "Stock": v["stock"],
                   "Regular price": money(price_pair(v)[1] or price_pair(v)[0]),
                   "Sale price": money(price_pair(v)[0]) if price_pair(v)[1] else ""}
            for n, name in enumerate(names, 1):
                row[f"Attribute {n} name"] = safe(name.title())
                row[f"Attribute {n} value(s)"] = safe(v["options"].get(name, ""))
                row[f"Attribute {n} global"] = 0
            rows.append(row)
    return rows


def _download(data: bytes, name: str) -> Response:
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/check")
def check():
    catalog = load_catalog()
    return {"products": len(catalog),
            "variants": sum(len(e["variants"]) for e in catalog),
            "warnings": warnings_for(catalog)}


@router.get("/shopify.csv")
def shopify_csv(image_base: str = "", live: bool = Query(False)):
    catalog = load_catalog()
    if not catalog:
        raise HTTPException(404, "No products to export yet.")
    rows = shopify_rows(catalog, clean_base(image_base), "active" if live else "draft")
    return _download(_write(SHOPIFY_COLUMNS, rows), "shopify_products.csv")


@router.get("/woocommerce.csv")
def woocommerce_csv(image_base: str = "", live: bool = Query(False)):
    catalog = load_catalog()
    if not catalog:
        raise HTTPException(404, "No products to export yet.")
    rows = woo_rows(catalog, clean_base(image_base), 1 if live else -1)
    return _download(_write(WOO_COLUMNS, rows), "woocommerce_products.csv")
