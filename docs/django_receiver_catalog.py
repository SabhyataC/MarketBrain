"""Receiver for Marketing Brain products, written for the Sanskruti `catalog` app models.

Put this file in your catalog app (e.g. catalog/mb_receiver.py) and add to urls.py:

    from catalog.mb_receiver import receive_product
    path("marketing-brain/products/", receive_product),

settings.py needs:   WEBHOOK_SECRET = "<same value as in Marketing Brain .env>"
Optional settings:   MB_DEFAULT_CATEGORY = "<name of a SignatureCategoryItem>"
                     MB_DEFAULT_FABRIC   = "<name of a Fabric>"
(used only when the sent product has no category / fabric of its own)

Rules, chosen so the sender can never damage your shop:
- Never creates Category, Fabric or Color rows. They need your hex codes, images and
  curation. If one is missing the request is refused with a message that says which.
- Products are saved with is_active=False (unpublished) unless the sender says live.
  Re-sending updates the same product (matched by product_code) and never unpublishes it.
- All-or-nothing: either the product and its variants are saved, or nothing is.
- Product.product_code = the sender's sku (e.g. MB-12). That is how repeats are matched.

SEO changes (event "seo.update") need these optional fields on Product, then a migration:
    seo_title = models.CharField(max_length=200, blank=True)
    seo_description = models.CharField(max_length=400, blank=True)
    seo_h1 = models.CharField(max_length=200, blank=True)
    seo_json_ld = models.TextField(blank=True)
The image alt text goes to the product's first ProductImage.alt_text. Render the seo_* fields in your
templates (<title>, <meta name="description">, the H1 and a <script type="application/ld+json"> block).
If the fields are missing the request is refused and says so, and nothing is changed.
- Prices: base_price = the MRP, discount_price = the price the customer pays (left empty when it equals the MRP).
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from catalog.models import (Color, Fabric, Print, Product, ProductImage,
                            ProductVariant, SignatureCategoryItem)

MAX_AGE = 300


class Refused(Exception):
    def __init__(self, message, status=422):
        self.message, self.status = message, status


def valid_signature(request) -> bool:
    ts = request.headers.get("X-MB-Timestamp", "")
    sig = request.headers.get("X-MB-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_AGE:
        return False
    expected = hmac.new(settings.WEBHOOK_SECRET.encode(), ts.encode() + b"." + request.body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def money(value) -> Decimal:
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise Refused("a price is not a number")
    if d < 0 or d >= Decimal("100000000"):
        raise Refused("a price is out of range")
    return d


def lookup(model, name):
    name = (name or "").strip()
    return model.objects.filter(name__iexact=name).first() if name else None


def save_product(data: dict, live: bool):
    attrs = data.get("attrs") or {}
    missing = []

    cat_name = data.get("category") or getattr(settings, "MB_DEFAULT_CATEGORY", "")
    category = lookup(SignatureCategoryItem, cat_name)
    if not category:
        missing.append(f"category '{cat_name}'" if cat_name else "a category")

    fab_name = attrs.get("fabric") or getattr(settings, "MB_DEFAULT_FABRIC", "")
    fabric = lookup(Fabric, fab_name)
    if not fabric:
        missing.append(f"fabric '{fab_name}'" if fab_name else "a fabric")

    print_type = lookup(Print, attrs.get("print"))  # optional: unknown prints are simply left blank

    colour_key = next((k for k in data.get("options", []) if k.lower() in ("colour", "color")), None)
    if data.get("options") and not colour_key:
        raise Refused("this product has options other than colour; only colour variants are supported")
    if len(data.get("options", [])) > 1:
        raise Refused("only colour variants are supported (this product has more option types)")
    variants = data.get("variants") or []
    colours = {}
    if colour_key:
        for v in variants:
            name = v["options"].get(colour_key, "")
            if name.lower() not in colours:
                colours[name.lower()] = lookup(Color, name)
                if not colours[name.lower()]:
                    missing.append(f"colour '{name}'")
    if missing:
        raise Refused("missing in your shop, add these first: " + ", ".join(missing))
    if not variants:
        raise Refused("no variants sent")

    mrps = [money(v.get("mrp") or v["price"]) for v in variants]
    sells = [money(v["price"]) for v in variants]
    common = lambda xs: sorted(set(xs), key=lambda x: (-xs.count(x), x))[0]  # most common value, then lowest
    base = common(mrps)
    sale = common(sells)
    discount = sale if sale < base else None
    stock = sum(max(int(v.get("stock") or 0), 0) for v in variants)
    with transaction.atomic():
        product = Product.objects.filter(product_code=data["sku"]).first()
        fields = dict(name=data["name"][:200], description=data.get("description") or "",
                      category=category, fabric=fabric, print_type=print_type,
                      base_price=base, discount_price=discount, stock_quantity=stock)
        if product:
            for k, v in fields.items():
                setattr(product, k, v)
            if live:
                product.is_active = True
            product.save()
        else:
            product = Product.objects.create(product_code=data["sku"], is_active=live, **fields)
        if colour_key:
            for v in variants:
                price = money(v.get("mrp") or v["price"])
                ProductVariant.objects.update_or_create(
                    product=product, color=colours[v["options"][colour_key].lower()],
                    defaults={"price": None if price == base else price})
        image = data.get("image")
        if image and len(image) <= 200 and not product.images.filter(image_url=image).exists():
            try:
                ProductImage.objects.create(product=product, image_url=image, alt_text=product.name[:200])
            except ValidationError:
                pass  # a photo address Django rejects is skipped; the product itself is still saved
    return product.id, ""


SEO_FIELDS = {"title": "seo_title", "description": "seo_description", "h1": "seo_h1", "json_ld": "seo_json_ld"}


def save_seo(sku: str, fields: dict):
    product = Product.objects.filter(product_code=sku).first()
    if not product:
        raise Refused("this product has not been sent to your shop yet")
    wanted = {k: v for k, v in fields.items() if k in SEO_FIELDS}
    missing = [m for m in SEO_FIELDS.values() if wanted and not hasattr(product, m)]
    if missing:
        raise Refused("add these fields to Product first: seo_title, seo_description, seo_h1, seo_json_ld")
    with transaction.atomic():
        for k, v in wanted.items():
            setattr(product, SEO_FIELDS[k], str(v))
        if wanted:
            product.save()
        if "alt" in fields:
            image = product.images.order_by("display_order", "id").first()
            if not image:
                raise Refused("this product has no image, so alt text cannot be saved")
            image.alt_text = str(fields["alt"])[:200]
            image.save()
    return product.id


@csrf_exempt
@require_POST
def receive_product(request):
    if not getattr(settings, "WEBHOOK_SECRET", "") or not valid_signature(request):
        return JsonResponse({"error": "bad signature"}, status=401)
    try:
        payload = json.loads(request.body)
        if payload.get("event") == "seo.update":
            try:
                rid = save_seo(str(payload["sku"]), dict(payload["fields"]))
            except Refused as e:
                return JsonResponse({"error": e.message}, status=e.status)
            return JsonResponse({"id": str(rid), "url": ""})
        product = payload["product"]
        product["sku"], product["name"], product["variants"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "bad payload"}, status=400)
    try:
        rid, url = save_product(product, bool(payload.get("live")))
    except Refused as e:
        return JsonResponse({"error": e.message}, status=e.status)
    except Exception:
        return JsonResponse({"error": "could not save the product"}, status=500)
    return JsonResponse({"id": str(rid), "url": url})
