"""Example receiver for Marketing Brain products, for a Django shop.

1. Put WEBHOOK_SECRET in your Django settings (same value as in Marketing Brain's .env).
2. Adapt save_product() to your own Product model.
3. Add to urls.py:  path("marketing-brain/products/", receive_product)

Products are saved UNPUBLISHED unless the sender says live=true. Publish them yourself.
"""
import hashlib
import hmac
import json
import time
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

MAX_AGE = 300  # seconds; older requests are refused so a captured one cannot be replayed


def valid_signature(request) -> bool:
    ts = request.headers.get("X-MB-Timestamp", "")
    sig = request.headers.get("X-MB-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_AGE:
        return False
    expected = hmac.new(settings.WEBHOOK_SECRET.encode(), ts.encode() + b"." + request.body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def save_product(data: dict, live: bool):
    """Replace with your own model code. Must be safe to run twice for the same sku."""
    # product, _ = Product.objects.update_or_create(sku=data["sku"], defaults={
    #     "name": data["name"], "description": data["description"] or "",
    #     "price": data["variants"][0]["price"], "is_published": live})
    # return product.id, product.get_absolute_url()
    return data["sku"], ""


@csrf_exempt
@require_POST
def receive_product(request):
    if not getattr(settings, "WEBHOOK_SECRET", "") or not valid_signature(request):
        return JsonResponse({"error": "bad signature"}, status=401)
    try:
        payload = json.loads(request.body)
        product = payload["product"]
        product["sku"], product["name"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "bad payload"}, status=400)
    rid, url = save_product(product, bool(payload.get("live")))
    return JsonResponse({"id": str(rid), "url": url})
