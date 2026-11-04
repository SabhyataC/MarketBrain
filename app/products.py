import itertools
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.db import get_conn
from app.audit import log

router = APIRouter()

TEXT_FIELDS = ("name", "brand", "category", "description", "photo")
NUMBER_FIELDS = ("price", "cost")
CORE_FIELDS = TEXT_FIELDS + NUMBER_FIELDS
OVERRIDABLE = set(CORE_FIELDS) | {"attrs"}
MAX_VARIANTS_PER_CALL = 200


def customer_price(price, cost):
    """What the customer pays. In this shop's words: Cost is the price the customer pays, and
    price is the MRP. Cost wins when the owner entered one; otherwise the MRP is the price."""
    if cost is not None and cost > 0:
        return cost
    return price


def canon(options: dict) -> str:
    """One canonical text form, so {"a":1,"b":2} and {"b":2,"a":1} are the same variant."""
    return json.dumps(options, sort_keys=True, separators=(",", ":"))


def resolve(product, variant) -> dict:
    """A variant as the customer sees it: the product's values unless it overrides them."""
    item = {k: product[k] for k in CORE_FIELDS}
    attrs = json.loads(product["attrs"] or "{}")
    overrides = json.loads(variant["overrides"] or "{}")
    attrs.update(overrides.pop("attrs", None) or {})
    item.update(overrides)
    item["attrs"] = attrs
    item["options"] = json.loads(variant["options"])
    item["stock"] = variant["stock"]
    return item


def create_product(conn, name, *, brand=None, category=None, description=None,
                   price=None, cost=None, photo=None, attrs=None, stock=0) -> int:
    """Insert a product plus its default variant (no options). Caller commits and logs."""
    name = name.title() if name.isupper() else name
    cur = conn.execute(
        "INSERT INTO products (name, brand, category, description, price, cost, photo, attrs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, brand, category, description, price, cost, photo, json.dumps(attrs or {})),
    )
    pid = cur.lastrowid
    conn.execute(
        "INSERT INTO variants (product_id, options, stock) VALUES (?, '{}', ?)", (pid, stock)
    )
    return pid


# ---------- products ----------

class ProductIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1)
    brand: str | None = None
    category: str | None = None
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    photo: str | None = None
    attrs: dict = {}                       # any extra facts: {"fabric": "cotton"}
    stock: int = Field(default=0, ge=0)    # stock of the default (no-options) variant


class ProductPatch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    photo: str | None = None
    attrs: dict | None = None              # merged in; a key set to null is removed


@router.post("/products")
def add_product(p: ProductIn):
    with get_conn() as conn:
        pid = create_product(
            conn, p.name, brand=p.brand, category=p.category, description=p.description,
            price=p.price, cost=p.cost, photo=p.photo, attrs=p.attrs, stock=p.stock,
        )
    log("owner", "product_added", f"id={pid} name={p.name}")
    return {"id": pid}


@router.get("/products")
def list_products():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.*, COALESCE(SUM(v.stock), 0) AS stock, COUNT(v.id) AS variant_count "
            "FROM products p LEFT JOIN variants v ON v.product_id = p.id "
            "GROUP BY p.id ORDER BY p.id"
        ).fetchall()
    return [dict(r) | {"attrs": json.loads(r["attrs"] or "{}")} for r in rows]


@router.get("/products/{pid}")
def get_product(pid: int):
    with get_conn() as conn:
        product = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        variants = conn.execute(
            "SELECT * FROM variants WHERE product_id = ? ORDER BY id", (pid,)
        ).fetchall()
    return dict(product) | {
        "attrs": json.loads(product["attrs"] or "{}"),
        "stock": sum(v["stock"] for v in variants),
        "variants": [
            {"id": v["id"], **resolve(product, v), "overrides": json.loads(v["overrides"] or "{}")}
            for v in variants
        ],
    }


@router.patch("/products/{pid}")
def edit_product(pid: int, body: ProductPatch):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "Nothing to change")
    if "name" in changes and not changes["name"]:
        raise HTTPException(400, "name cannot be empty")

    diffs, updates = [], {}
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Product not found")
        for key, new in changes.items():
            if key == "attrs":
                attrs = json.loads(row["attrs"] or "{}")
                for ak, av in (new or {}).items():
                    old = attrs.get(ak)
                    if av is None:
                        attrs.pop(ak, None)
                    else:
                        attrs[ak] = av
                    if old != av:
                        diffs.append(f"attrs.{ak}: {old!r} -> {av!r}")
                updates["attrs"] = json.dumps(attrs)
            elif row[key] != new:
                diffs.append(f"{key}: {row[key]!r} -> {new!r}")
                updates[key] = new
        if diffs:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*updates.values(), pid))
    if diffs:
        log("owner", "product_edited", f"id={pid} " + "; ".join(diffs)[:400])
    return {"id": pid, "changed": diffs}


@router.delete("/products/{pid}")
def delete_product(pid: int):
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM products WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Product not found")
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))   # variants cascade
    log("owner", "product_deleted", f"id={pid} name={row['name']}")
    return {"id": pid}


# ---------- variants ----------

class AxesIn(BaseModel):
    axes: dict[str, list[str]]     # {"colour": ["red", "blue"], "size": ["S", "M", "L"]}


class VariantPatch(BaseModel):
    stock: int | None = Field(default=None, ge=0)
    overrides: dict | None = None  # merged in; a key set to null goes back to inheriting


@router.post("/products/{pid}/variants/generate")
def generate_variants(pid: int, body: AxesIn):
    axes = {}
    for name, values in body.axes.items():
        name = name.strip()
        vals = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
        if not name or not vals:
            raise HTTPException(400, "Every axis needs a name and at least one value")
        axes[name] = vals
    if not axes:
        raise HTTPException(400, 'Give at least one axis, e.g. {"axes": {"colour": ["red"]}}')
    total = 1
    for vals in axes.values():
        total *= len(vals)
    if total > MAX_VARIANTS_PER_CALL:
        raise HTTPException(
            400, f"That would create {total} variants (max {MAX_VARIANTS_PER_CALL}). Split it up."
        )

    keys, created = list(axes), 0
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise HTTPException(404, "Product not found")
        # The plain default variant is only a placeholder: drop it if it holds nothing.
        conn.execute(
            "DELETE FROM variants WHERE product_id = ? AND options = '{}' "
            "AND stock = 0 AND overrides = '{}'", (pid,),
        )
        for combo in itertools.product(*axes.values()):
            created += conn.execute(
                "INSERT OR IGNORE INTO variants (product_id, options) VALUES (?, ?)",
                (pid, canon(dict(zip(keys, combo)))),
            ).rowcount
        count = conn.execute(
            "SELECT COUNT(*) FROM variants WHERE product_id = ?", (pid,)
        ).fetchone()[0]
    log("owner", "variants_generated", f"product={pid} created={created} total={count}")
    return {"created": created, "variants_total": count}


@router.patch("/variants/{vid}")
def edit_variant(vid: int, body: VariantPatch):
    incoming = body.overrides or {}
    if body.stock is None and not incoming:
        raise HTTPException(400, "Nothing to change")
    bad = set(incoming) - OVERRIDABLE
    if bad:
        raise HTTPException(
            400, f"Cannot override {sorted(bad)}. Allowed: {sorted(OVERRIDABLE)}"
        )
    for key, val in incoming.items():
        if val is None:
            continue
        if key in NUMBER_FIELDS and (isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0):
            raise HTTPException(400, f"{key} must be a number, 0 or more")
        if key in TEXT_FIELDS and not isinstance(val, str):
            raise HTTPException(400, f"{key} must be text")
        if key == "attrs" and not isinstance(val, dict):
            raise HTTPException(400, "attrs must be an object")

    diffs = []
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM variants WHERE id = ?", (vid,)).fetchone()
        if not row:
            raise HTTPException(404, "Variant not found")
        overrides = json.loads(row["overrides"] or "{}")
        for key, val in incoming.items():
            old = overrides.get(key)
            if val is None:
                overrides.pop(key, None)
            else:
                overrides[key] = val
            if old != val:
                diffs.append(f"{key}: {old!r} -> {val!r}")
        if body.stock is not None and body.stock != row["stock"]:
            diffs.append(f"stock: {row['stock']} -> {body.stock}")
            conn.execute("UPDATE variants SET stock = ? WHERE id = ?", (body.stock, vid))
        conn.execute("UPDATE variants SET overrides = ? WHERE id = ?", (json.dumps(overrides), vid))
    if diffs:
        log("owner", "variant_edited", f"id={vid} product={row['product_id']} " + "; ".join(diffs)[:400])
    return {"id": vid, "changed": diffs}


@router.delete("/variants/{vid}")
def delete_variant(vid: int):
    with get_conn() as conn:
        row = conn.execute("SELECT product_id, options FROM variants WHERE id = ?", (vid,)).fetchone()
        if not row:
            raise HTTPException(404, "Variant not found")
        conn.execute("DELETE FROM variants WHERE id = ?", (vid,))
    log("owner", "variant_deleted", f"id={vid} product={row['product_id']} options={row['options']}")
    return {"id": vid}