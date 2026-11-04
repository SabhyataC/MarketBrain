import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="landing_test_"))
os.chdir(WORK)
(WORK / "static").symlink_to(ROOT / "static")
os.environ["MODEL_API_KEY"] = "test-key"
os.environ.pop("SYNC_IMAGE_BASE", None)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import app.landing as landing
import app.policy as policy
from app.db import get_conn, init_db
from app.main import app
from app.products import create_product

FAILS = 0
CALLS = []
SCEN = {"content": [], "unsupported": []}

GOOD = {
    "headline": "Easy everyday kurta",
    "subheading": "A simple blue kurta for work, family time and festive days.",
    "bullets": ["Made for daily wear", "Pairs with jeans or leggings", "A thoughtful gift for family"],
    "description": "This blue kurta from Vastra is easy to style for the office, a family lunch or a festive evening. It comes in blue and is available now.",
    "cta": "Order now",
}


def check(name, ok, detail=""):
    global FAILS
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else "  -> " + str(detail)[:300]))
    if not ok:
        FAILS += 1


def fake_ask(prompt):
    CALLS.append(prompt)
    if "single-page product landing page" in prompt:
        v = SCEN["content"].pop(0) if SCEN["content"] else GOOD
        return v if isinstance(v, str) else json.dumps(v)
    if "Below are ad variants" in prompt:
        listing = json.loads(prompt.split("Variants:\n", 1)[1])
        out = []
        for item in listing:
            out.append({"index": item["index"],
                        "unsupported": [c for needle, c in SCEN["unsupported"] if needle in item["text"].lower()]})
        return json.dumps(out)
    return "[]"


landing.ask = fake_ask
policy.ask = fake_ask
IMG = {"calls": [], "fail": False}


def fake_edit_image(prompt, ref, out, size="1024x1024"):
    IMG["calls"].append((prompt, ref, out))
    if IMG["fail"]:
        raise RuntimeError("image model down")
    img = Image.new("RGB", (1024, 1024), (30, 60, 90))
    ImageDraw.Draw(img).ellipse([300, 300, 700, 700], fill=(220, 120, 60))
    img.save(out, "PNG")


landing.edit_image = fake_edit_image


def model_calls():
    return len(CALLS)


def make_photo(path):
    img = Image.new("RGB", (800, 1000), (236, 226, 211))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 760, 800, 1000], fill=(214, 200, 180))
    d.polygon([(300, 220), (500, 220), (610, 360), (570, 420), (520, 380), (540, 800), (260, 800), (280, 380), (230, 420), (190, 360)], fill=(27, 94, 112))
    d.rectangle([380, 220, 420, 520], fill=(20, 72, 86))
    d.ellipse([340, 120, 460, 240], fill=(230, 190, 160))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)


def add_product(name="Blue Kurta", brand="Vastra", price=799, cost=499, stock=5, photo=True, attrs=None,
                description="Cotton kurta with side slits", category="kurta"):
    with get_conn() as conn:
        pid = create_product(conn, name, brand=brand, category=category, description=description, price=price, cost=cost,
                             stock=stock, attrs=attrs if attrs is not None else {"fabric": "cotton", "colour": "Blue"})
        if photo:
            conn.execute("UPDATE products SET photo = ? WHERE id = ?", (f"/photos/{pid}.jpg", pid))
    if photo:
        make_photo(f"data/photos/{pid}.jpg")
    return pid


def gen(client, pid, **extra):
    return client.post("/landing/generate", json={"product_id": pid, **extra})


def proposal(client, pid):
    return next((p for p in client.get("/proposals?status=pending").json() + client.get("/proposals?status=approved").json()
                 + client.get("/proposals?status=rejected").json() if p["id"] == pid), None)


def folder(pid):
    return Path("data/landing") / str(pid)


def reset(content=None, unsupported=None):
    CALLS.clear()
    SCEN["content"] = list(content or [])
    SCEN["unsupported"] = list(unsupported or [])


def stray_dirs():
    base = Path("data/landing")
    return [p.name for p in base.iterdir() if p.name.startswith("_")] if base.exists() else []


init_db()

with TestClient(app) as client:
    print("== happy path ==")
    p1 = add_product()
    reset()
    r = gen(client, p1)
    check("generate returns 200", r.status_code == 200, r.text)
    body = r.json()
    pid = body.get("proposal_id")
    check("all checks passed on a normal page", body.get("checks_ok") is True, body.get("failed"))
    prop = proposal(client, pid)
    check("a pending landing_page proposal exists", bool(prop) and prop["kind"] == "landing_page" and prop["status"] == "pending")
    pay = prop["payload"]
    check("model called exactly twice (page text, policy check)", model_calls() == 2, model_calls())
    check("all five files saved", all((folder(pid) / n).is_file() for n in landing.PACK_FILES), list(folder(pid).iterdir()))
    check("14 checks measured (7 checks x 2 screens)", len(pay["checks"]) == 14, len(pay["checks"]))
    check("every check passes and says what was measured", all(c["pass"] and len(c["message"]) > 10 for c in pay["checks"]),
          [c for c in pay["checks"] if not c["pass"]])
    hs = [c for c in pay["checks"] if c["id"] == "hscroll" and c["viewport"] == "phone"][0]
    check("scroll check reports real widths", "390 px" in hs["message"], hs["message"])
    ct = [c for c in pay["checks"] if c["id"] == "contrast" and c["viewport"] == "phone"][0]
    ratio = float(re.search(r"Lowest contrast is ([\d.]+)", ct["message"]).group(1))
    check("contrast is a measured number of at least 4.5", ratio >= 4.5, ct["message"])
    tp = [c for c in pay["checks"] if c["id"] == "tap" and c["viewport"] == "phone"][0]
    check("tap target reports real size", re.search(r"\d+ by \d+ px", tp["message"]) is not None, tp["message"])
    check("repair not needed on a normal page", pay["repair"]["applied"] is False)
    check("phone screenshot is 390 px wide", Image.open(folder(pid) / "phone.png").width == 390)
    check("desktop screenshot is 1280 px wide", Image.open(folder(pid) / "desktop.png").width == 1280)
    check("phone screenshot is taller than the screen (full page)", Image.open(folder(pid) / "phone.png").height > 844)
    pg = client.get(f"/landing/{pid}/page")
    check("page route serves html with a title from the catalog", pg.status_code == 200 and "<title>Blue Kurta | Vastra</title>" in pg.text)
    check("page route sets a strict content security policy", "default-src 'none'" in pg.headers.get("content-security-policy", ""))
    check("page has a meta description", 'name="description"' in pg.text)
    check("page has alt text built from the catalog", 'alt="Image of Blue Kurta by Vastra in Blue"' in pg.text, re.findall(r'alt="[^"]*"', pg.text))
    check("page uses the relative photo file", 'src="photo.jpg"' in pg.text)
    check("photo route works", client.get(f"/landing/{pid}/photo.jpg").status_code == 200)
    for which in ("phone", "desktop"):
        s = client.get(f"/landing/{pid}/shot/{which}")
        check(f"{which} screenshot route returns a PNG", s.status_code == 200 and s.content[:8] == b"\x89PNG\r\n\x1a\n")
    check("unknown screenshot name is refused", client.get(f"/landing/{pid}/shot/tablet").status_code == 404)
    check("creation is in the audit log", any(a["action"] == "landing_page_created" for a in client.get("/audit").json()))
    check("no leftover work folders", stray_dirs() == [], stray_dirs())
    check("order link missing is reported", any("order link" in m for m in pay["missing"]), pay["missing"])
    check("no link: disabled placeholder, no href on the button",
          'aria-disabled="true"' in pg.text and "Order on WhatsApp / shop" in pg.text and 'class="buy"' not in pg.text)
    ld_raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', pg.text, re.S).group(1)
    ld = json.loads(ld_raw)
    check("JSON-LD price is the customer price from the catalog", ld["offers"]["price"] == "499.00" and ld["offers"]["priceCurrency"] == "INR", ld["offers"])
    check("JSON-LD says in stock", ld["offers"]["availability"].endswith("InStock"))
    check("JSON-LD name, brand and sku come from the catalog", ld["name"] == "Blue Kurta" and ld["brand"]["name"] == "Vastra" and ld["sku"] == f"MB-{p1}")
    check("JSON-LD description is the catalog description, not the model's", ld.get("description") == "Cotton kurta with side slits")
    check("model headline is not in the JSON-LD", GOOD["headline"] not in ld_raw)
    check("price shown is Cost, MRP struck through", '<span class="price">Rs 499</span>' in pg.text and '<s class="mrp">Rs 799</s>' in pg.text)

    print("== AI hero image ==")
    check("an AI image was requested from the product photo", len(IMG["calls"]) >= 1 and IMG["calls"][0][1].endswith(f"{p1}.jpg"), IMG["calls"][:1])
    check("the image prompt keeps the product the same and forbids text", "exactly the same" in IMG["calls"][0][0] and "Do not add any text" in IMG["calls"][0][0])
    check("the page photo is the AI image, not the real photo", Image.open(folder(pid) / "photo.jpg").getpixel((10, 10)) == (30, 60, 90) or abs(Image.open(folder(pid) / "photo.jpg").getpixel((10, 10))[2] - 90) < 12)
    check("the card says the image is AI made from the photo", pay["image_source"] == "AI image made from your photo", pay.get("image_source"))
    check("no leftover temp image in the page folder", not (folder(pid) / "ai.png").exists())
    IMG["calls"].clear()
    r = gen(client, p1, use_real_photo=True)
    check("real-photo option makes no image call", r.status_code == 200 and IMG["calls"] == [])
    check("real-photo option says so", proposal(client, r.json()["proposal_id"])["payload"]["image_source"] == "your real photo")
    check("real-photo option keeps the real photo pixels", Image.open(folder(r.json()["proposal_id"]) / "photo.jpg").getpixel((10, 10)) != (30, 60, 90))
    IMG["fail"] = True
    r = gen(client, p1)
    check("AI image failure falls back to the real photo and says so", r.status_code == 200 and "could not be made" in proposal(client, r.json()["proposal_id"])["payload"]["image_source"], r.text)
    check("the fallback page still passes the browser checks", r.json()["checks_ok"] is True)
    IMG["fail"] = False

    print("== refusals and model failures ==")
    p_nophoto = add_product("No Photo Kurta", photo=False)
    reset()
    r = gen(client, p_nophoto)
    check("no photo gives a clear 400", r.status_code == 400 and "photo" in r.json()["detail"].lower(), r.text)
    check("no model call was made for a refused product", model_calls() == 0)
    p_noprice = add_product("No Price Kurta", price=None, cost=None)
    r = gen(client, p_noprice)
    check("no cost and no MRP gives a clear 400", r.status_code == 400 and "Cost" in r.json()["detail"], r.text)
    check("unknown product is 404", gen(client, 9999).status_code == 404)
    r = gen(client, p1, link="javascript:alert(1)")
    check("a bad order link is refused", r.status_code == 400, r.text)
    before = len(client.get("/proposals").json())
    reset(["this is not json", "still not json"])
    r = gen(client, p1)
    check("bad model JSON twice gives 502", r.status_code == 502 and "usable page text" in r.json()["detail"], r.text)
    check("only the two page-text calls were made", model_calls() == 2, model_calls())
    check("the retry names the exact problem", "was not valid JSON" in CALLS[1], CALLS[1][-200:])
    check("a failed generation leaves no proposal and no folder", len(client.get("/proposals").json()) == before and stray_dirs() == [])
    reset([dict(GOOD, headline="x" * 80), GOOD])
    r = gen(client, p1)
    check("first answer too long, retry fixes it", r.status_code == 200, r.text)
    check("retry prompt says headline is 80 characters, limit 60", "headline is 80 characters; the limit is 60" in CALLS[1])
    reset([dict(GOOD, bullets=["Free delivery across India", "Easy to style", "Daily wear"]),
           dict(GOOD, description="Now only Rs 499 with free delivery.")])
    r = gen(client, p1)
    check("delivery or price words twice gives 502", r.status_code == 502 and "not allowed" in r.json()["detail"], r.text)
    check("the retry named the forbidden word", "delivery" in CALLS[1] and "not allowed" in CALLS[1])
    reset([dict(GOOD, bullets=["one", "two"]), dict(GOOD, bullets=["one", "two"])])
    r = gen(client, p1)
    check("too few bullets twice gives 502", r.status_code == 502 and "3 to 5" in r.json()["detail"], r.text)
    saved = sys.modules.get("playwright.sync_api")
    sys.modules["playwright.sync_api"] = None
    reset()
    r = gen(client, p1)
    check("no Playwright: clear 400 with install steps", r.status_code == 400 and "playwright install chromium" in r.json()["detail"], r.text)
    check("no Playwright: no model call was spent", model_calls() == 0)
    if saved is not None:
        sys.modules["playwright.sync_api"] = saved
    else:
        del sys.modules["playwright.sync_api"]

    print("== escaping ==")
    p_evil = add_product("Evil </script><img src=x onerror=alert(2)> Kurta", brand="B&B \"Co\"")
    evil = {"headline": "<script>alert(1)</script>", "subheading": "Fine <b>bold</b> & \"quoted\" text",
            "bullets": ["<img src=x onerror=alert(3)>", "Plain point", "Another point"],
            "description": "Line with </style><script>alert(4)</script> inside.", "cta": "<i>Go</i>"}
    reset([evil])
    r = gen(client, p_evil, link='https://shop.example/buy?a=1&b="2"')
    check("page with hostile text still builds", r.status_code == 200, r.text)
    eid = r.json()["proposal_id"]
    html = client.get(f"/landing/{eid}/page").text
    check("script tag from the model is escaped", "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html)
    check("img tag from the model is escaped", "<img src=x" not in html and "&lt;img src=x onerror=alert(3)&gt;" in html)
    check("style/script break-out in the description is escaped", "</style><script>alert(4)" not in html)
    check("only one script element exists (the JSON-LD data block)", html.count("<script") == 1, html.count("<script"))
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    check("JSON-LD survives a hostile product name", ld["name"].startswith("Evil </script>"), ld["name"])
    check("link with quotes is escaped in the href", 'href="https://shop.example/buy?a=1&amp;b=&quot;2&quot;"' in html)
    check("hostile page still passes the browser checks", r.json()["checks_ok"] is True, r.json()["failed"])

    print("== policy gates ==")
    reset([dict(GOOD, headline="Best kurta in town")])
    r = gen(client, p1)
    bid = r.json()["proposal_id"]
    lines = {l["key"]: l for l in proposal(client, bid)["payload"]["lines"]}
    check("a rule-breaking headline is blocked", lines["headline"]["blocked"] and "superlative claim" in lines["headline"]["flags"], lines["headline"])
    a = client.post(f"/proposals/{bid}/approve")
    check("blocked line stops approval", a.status_code == 400 and "breaks the ad rules" in a.json()["detail"], a.text)
    reset()
    e = client.put(f"/landing/{bid}", json={"headline": "A kurta for every day"})
    check("editing the headline succeeds and re-renders", e.status_code == 200 and e.json()["rechecked"] is True, e.text)
    check("only the headline was re-checked by the model (one call)", model_calls() == 1, model_calls())
    lines = {l["key"]: l for l in proposal(client, bid)["payload"]["lines"]}
    check("the fix clears the block", not lines["headline"]["blocked"])
    check("the page was re-rendered with the new headline", "A kurta for every day" in client.get(f"/landing/{bid}/page").text)
    check("the checks were measured again after the edit", proposal(client, bid)["payload"]["checks_ok"] is True)
    a = client.post(f"/proposals/{bid}/approve")
    check("approval works after the fix", a.status_code == 200 and a.json()["status"] == "approved", a.text)
    check("editing an approved page is refused", client.put(f"/landing/{bid}", json={"headline": "Other"}).status_code == 400)

    reset([dict(GOOD, subheading="A soft cotton kurta for every day.")], [("soft cotton", "soft cotton feel")])
    r = gen(client, p1)
    uid = r.json()["proposal_id"]
    lines = {l["key"]: l for l in proposal(client, uid)["payload"]["lines"]}
    check("unverified claim is flagged, not blocked", "unsupported claim: soft cotton feel" in lines["subheading"]["flags"] and not lines["subheading"]["blocked"])
    a = client.post(f"/proposals/{uid}/approve")
    check("unconfirmed claim stops approval and names the line", a.status_code == 400 and "Subheading" in a.json()["detail"], a.text)
    stamp = proposal(client, uid)["payload"]["stamp"]
    reset()
    e = client.put(f"/landing/{uid}", json={"confirmed": ["subheading"]})
    check("confirming does not re-render or call the model", e.status_code == 200 and e.json()["rechecked"] is False and model_calls() == 0, e.text)
    check("the stamp is unchanged after only confirming", proposal(client, uid)["payload"]["stamp"] == stamp)
    reset(unsupported=[("soft cotton", "soft cotton feel")])
    e = client.put(f"/landing/{uid}", json={"subheading": "A soft cotton kurta for work days."})
    lines = {l["key"]: l for l in proposal(client, uid)["payload"]["lines"]}
    check("changing a confirmed line resets the confirmation and re-checks it",
          lines["subheading"]["confirmed"] is False and "unsupported claim: soft cotton feel" in lines["subheading"]["flags"], lines["subheading"])
    a = client.post(f"/proposals/{uid}/approve")
    check("approval is blocked again", a.status_code == 400)
    e = client.put(f"/landing/{uid}", json={"subheading": "A kurta for work days.", "confirmed": []})
    a = client.post(f"/proposals/{uid}/approve")
    check("editing the claim away makes it approvable", a.status_code == 200, a.text)

    print("== edit validation and link ==")
    reset()
    r = gen(client, p1)
    vid = r.json()["proposal_id"]
    n = model_calls()
    for label, patch, expect in [("headline over 60", {"headline": "h" * 61}, "headline"),
                                 ("only 2 bullets", {"bullets": ["a", "b"]}, "3 to 5"),
                                 ("6 bullets", {"bullets": list("abcdef")}, "3 to 5"),
                                 ("empty button text", {"cta": "  "}, "cta"),
                                 ("description over 500", {"description": "d" * 501}, "description")]:
        e = client.put(f"/landing/{vid}", json=patch)
        check(f"edit refused: {label}", e.status_code == 400 and expect in e.json()["detail"], e.text)
    check("refused edits spent no model call", model_calls() == n)
    check("bad link on edit refused", client.put(f"/landing/{vid}", json={"link": "ftp://x"}).status_code == 400)
    e = client.put(f"/landing/{vid}", json={"link": "https://wa.me/919999999999"})
    check("setting an order link re-renders and re-checks", e.status_code == 200 and e.json()["rechecked"] is True, e.text)
    html = client.get(f"/landing/{vid}/page").text
    check("with a link the button is a real link with the button text",
          'class="buy" href="https://wa.me/919999999999"' in html and ">Order now</a>" in html and "aria-disabled" not in html)
    check("order link warning is gone", not any("order link" in m for m in proposal(client, vid)["payload"]["missing"]))
    e = client.put(f"/landing/{vid}", json={"bullets": ["One", "Two", "Three", "Four"], "link": ""})
    html = client.get(f"/landing/{vid}/page").text
    check("bullet list is replaced and the link cleared", html.count("<li>") == 4 and "aria-disabled" in html)
    check("404 for a missing page edit", client.put("/landing/99999", json={"headline": "x"}).status_code == 404)

    print("== deliberately broken template ==")
    long_word = {"description": "a" * 300}
    saved_css = landing.REPAIR_CSS
    landing.REPAIR_CSS = ""
    reset([dict(GOOD, **long_word)])
    r = gen(client, p1)
    check("page is still saved when a check fails", r.status_code == 200, r.text)
    body = r.json()
    check("the report says checks failed instead of hiding it", body["checks_ok"] is False and body["repaired"] is False, body)
    fids = {(f["id"], f["viewport"]) for f in body["failed"]}
    check("sideways scrolling FAILS at phone width", ("hscroll", "phone") in fids, fids)
    pay = proposal(client, body["proposal_id"])["payload"]
    hs = [c for c in pay["checks"] if c["id"] == "hscroll" and c["viewport"] == "phone"][0]
    px = int(re.search(r"is (\d+) px wide", hs["message"]).group(1))
    check("the failure message gives the measured width", px > 390, hs["message"])
    check("the failing check is kept in the saved payload and checks.json", pay["checks_ok"] is False and
          json.loads((folder(body["proposal_id"]) / "checks.json").read_text())["all_passed"] is False)
    landing.REPAIR_CSS = saved_css
    reset([dict(GOOD, **long_word)])
    r = gen(client, p1)
    body = r.json()
    pay = proposal(client, body["proposal_id"])["payload"]
    check("with the repair pass the same page is fixed once", body["repaired"] is True and body["checks_ok"] is True, body)
    check("the report keeps what failed before the repair", any(b["id"] == "hscroll" for b in pay["repair"]["before"]), pay["repair"])
    check("the report says what the repair fixed", any(b["id"] == "hscroll" for b in pay["repair"]["fixed"]) and not pay["repair"]["still_failing"])
    check("final checks were re-measured after the repair", pay["checks_ok"] is True and len(pay["checks"]) == 14)

    print("== each check can fail (measured, not assumed) ==")

    def render_variant(tag, css="", body_extra="", delete_photo=False, content=GOOD):
        d = WORK / f"variant_{tag}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
        make_photo(d / "photo.jpg")
        with get_conn() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (p1,)).fetchone()
        entry = landing._entry(p1)
        attrs = json.loads(p["attrs"])
        page = landing.build_page_data(p, entry, attrs, {"currency": "Rs"})
        page["photo_w"], page["photo_h"] = 800, 1000
        html = landing.render_html(content, page, "", False)
        html = html.replace("</style>", css + "</style>").replace("</body>", body_extra + "</body>")
        (d / "index.html").write_text(html, encoding="utf-8")
        if delete_photo:
            (d / "photo.jpg").unlink()
        res = landing.run_checks(d / "index.html", d)
        return {(c["id"], c["viewport"]): c for c in res}

    base = render_variant("base")
    check("control page passes every check", all(c["pass"] for c in base.values()), [c for c in base.values() if not c["pass"]])
    r_ = render_variant("nophoto", delete_photo=True)
    check("missing photo fails the load check", not r_[("load", "phone")]["pass"], r_[("load", "phone")]["message"])
    check("missing photo fails the photo check", not r_[("image", "phone")]["pass"], r_[("image", "phone")]["message"])
    r_ = render_variant("console", body_extra="<script>console.error('boom')</script>")
    check("a console error fails the load check", not r_[("load", "phone")]["pass"] and "boom" in r_[("load", "phone")]["message"], r_[("load", "phone")]["message"])
    r_ = render_variant("jserr", body_extra="<script>nope()</script>")
    check("a script error fails the load check", not r_[("load", "desktop")]["pass"], r_[("load", "desktop")]["message"])
    r_ = render_variant("contrast", css=".sub{color:#c8c8c8}")
    c = r_[("contrast", "phone")]
    check("light grey text fails the contrast check with a ratio below 4.5", not c["pass"] and float(re.search(r"is ([\d.]+) to 1", c["message"]).group(1)) < 4.5, c["message"])
    r_ = render_variant("tap", css=".buy{min-height:20px;width:30px;padding:0}")
    check("a tiny button fails the tap check with its measured size", not r_[("tap", "phone")]["pass"] and re.search(r"is 30 by \d+ px", r_[("tap", "phone")]["message"]) is not None, r_[("tap", "phone")]["message"])
    r_ = render_variant("fold", css=".hero{height:900px}")
    check("a button pushed below the screen fails the fold check", not r_[("above_fold", "phone")]["pass"] and "scroll" in r_[("above_fold", "phone")]["message"], r_[("above_fold", "phone")]["message"])
    r_ = render_variant("overlap", body_extra='<div style="position:fixed;left:0;top:0;width:100%;height:100%;background:#fff"></div>')
    check("something painted over the button fails the overlap check", not r_[("overlap", "phone")]["pass"], r_[("overlap", "phone")]["message"])
    r_ = render_variant("hit", body_extra='<div style="position:fixed;left:0;top:0;width:100%;height:100%;background:transparent"></div>')
    check("an invisible layer on top of the button fails the overlap check", not r_[("overlap", "phone")]["pass"] and "cannot be reached" in r_[("overlap", "phone")]["message"], r_[("overlap", "phone")]["message"])
    r_ = render_variant("hscroll", css=".hero{width:700px}")
    check("a wide element fails the sideways-scroll check", not r_[("hscroll", "phone")]["pass"], r_[("hscroll", "phone")]["message"])

    print("== prices and JSON-LD for different catalog cases ==")
    cfg = {"currency": "Rs"}

    def price_page(**kw):
        pid_ = add_product(photo=False, **kw)
        with get_conn() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (pid_,)).fetchone()
        page = landing.build_page_data(p, landing._entry(pid_), json.loads(p["attrs"]), cfg)
        page["photo_w"] = page["photo_h"] = 0
        return page, landing.render_html(GOOD, page, "", False)

    pg_, html = price_page(price=599, cost=None)
    check("MRP only: price is the MRP, no strike", pg_["price_text"] == "Rs 599" and '<s class="mrp">' not in html, pg_)
    pg_, html = price_page(price=599, cost=599)
    check("Cost equal to MRP: no strike", pg_["mrp_text"] == "" and "mrp" not in html.split("</style>")[1])
    pg_, html = price_page(price=799, cost=900)
    check("Cost above MRP: shows Cost, no strike", pg_["price_text"] == "Rs 900" and pg_["mrp_text"] == "")
    pg_, html = price_page(price=None, cost=450)
    check("Cost only: shows Cost, no strike", pg_["price_text"] == "Rs 450" and pg_["mrp_text"] == "")
    pg_, html = price_page(price=999, cost=749, stock=0)
    ld = json.loads(pg_["json_ld"])
    check("out of stock: schema says OutOfStock and the page says Out of stock",
          ld["offers"]["availability"].endswith("OutOfStock") and "Out of stock" in html, ld["offers"])
    check("MRP above Cost: struck through MRP", pg_["mrp_text"] == "Rs 999")
    pid_v = add_product("Two Prices", price=500, cost=400, photo=False)
    with get_conn() as conn:
        conn.execute("INSERT INTO variants (product_id, options, overrides, stock) VALUES (?, ?, ?, 2)",
                     (pid_v, json.dumps({"colour": "Red"}), json.dumps({"cost": 350, "price": 500})))
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pid_v,)).fetchone()
    pg_ = landing.build_page_data(p, landing._entry(pid_v), json.loads(p["attrs"]), cfg)
    check("variants with different prices show 'From' the lowest and no strike", pg_["price_text"] == "From Rs 350" and pg_["mrp_text"] == "", pg_["price_text"])
    check("schema uses the lowest price too", json.loads(pg_["json_ld"])["offers"]["price"] == "350.00")
    check("details list brand, category, attributes, availability", [k for k, _ in pg_["details"]][:2] == ["Brand", "Category"] and pg_["details"][-1][0] == "Availability")

    print("== pack ==")
    reset()
    r = gen(client, p1)
    kid = r.json()["proposal_id"]
    check("pack is refused before approval", client.get(f"/landing/{kid}/pack").status_code == 400)
    check("not in the approved list yet", all(a["id"] != kid for a in client.get("/landing/approved").json()))
    a = client.post(f"/proposals/{kid}/approve")
    check("clean page approves", a.status_code == 200, a.text)
    z = client.get(f"/landing/{kid}/pack")
    check("pack downloads as a zip", z.status_code == 200 and z.headers["content-type"] == "application/zip")
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    check("pack holds exactly index.html, photo, both screenshots and checks.json", set(zf.namelist()) == set(landing.PACK_FILES), zf.namelist())
    cj = json.loads(zf.read("checks.json"))
    check("checks.json holds the measured checks", cj["all_passed"] is True and len(cj["checks"]) == 14 and cj["screens"] == {"phone": "390x844", "desktop": "1280x800"})
    check("pack index.html is the approved page", GOOD["headline"].encode() in zf.read("index.html"))
    check("pack screenshots are real PNGs", zf.read("phone.png")[:4] == b"\x89PNG" and zf.read("desktop.png")[:4] == b"\x89PNG")
    check("approved list shows it", any(a["id"] == kid and a["checks_ok"] for a in client.get("/landing/approved").json()))
    r = gen(client, p1)
    xid = r.json()["proposal_id"]
    rj = client.post(f"/proposals/{xid}/reject")
    check("rejecting works", rj.status_code == 200 and rj.json()["status"] == "rejected")
    check("pack of a rejected page is refused", client.get(f"/landing/{xid}/pack").status_code == 400)
    check("unknown page pack is 404", client.get("/landing/99999/pack").status_code == 404)
    check("approving writes the audit log", any(a["action"] == "proposal_approved" for a in client.get("/audit?limit=100").json()))
    reset()
    r = gen(client, p1)
    mid = r.json()["proposal_id"]
    (folder(mid) / "phone.png").unlink()
    a = client.post(f"/proposals/{mid}/approve")
    check("a page with a missing screenshot cannot be approved", a.status_code == 400 and "missing" in a.json()["detail"], a.text)
    reset()
    r = gen(client, p1)
    nid = r.json()["proposal_id"]
    with get_conn() as conn:
        pay = json.loads(conn.execute("SELECT payload FROM proposals WHERE id = ?", (nid,)).fetchone()["payload"])
        pay["checked"] = False
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(pay), nid))
    a = client.post(f"/proposals/{nid}/approve")
    check("a page that was never checked cannot be approved", a.status_code == 400 and "not been checked" in a.json()["detail"], a.text)

    print("== existing flows still work ==")
    check("home page loads", client.get("/").status_code == 200)
    check("proposals, audit, series config respond", client.get("/proposals").status_code == 200 and client.get("/audit").status_code == 200 and client.get("/series/config").status_code == 200)
    r = client.post("/proposals", json={"kind": "catalog_draft", "payload": {"items": [{"name": "Regress Item", "brand": "B", "price": 100, "stock": 2}]}})
    a = client.post(f"/proposals/{r.json()['id']}/approve")
    check("catalog draft still imports", a.status_code == 200 and a.json().get("added") == 1, a.text)
    r = client.post("/proposals", json={"kind": "ad_draft", "payload": {"product_id": 1, "product_name": "X", "daily_budget": 100,
                                                                         "variants": [{"headline": "h", "text": "t", "cta": "Shop Now", "flags": [], "blocked": False}]}})
    a = client.post(f"/proposals/{r.json()['id']}/approve")
    check("ad draft still approves into a paused draft", a.status_code == 200 and "meta_draft" in a.json(), a.text)
    r = client.post("/proposals", json={"kind": "ad_series", "payload": {"slots": [{"url": ""}]}})
    a = client.post(f"/proposals/{r.json()['id']}/approve")
    check("incomplete ad series is still refused", a.status_code == 400 and "Picture" in a.json()["detail"], a.text)
    r = client.post("/proposals", json={"kind": "promotion", "payload": {"picks": []}})
    check("other proposal kinds still approve", client.post(f"/proposals/{r.json()['id']}/approve").status_code == 200)

    print("== kill switch (only if this repo has one) ==")
    if importlib.util.find_spec("app.killswitch"):
        reset()
        client.post("/killswitch", json={"on": True, "reason": "test"})
        r = gen(client, p1)
        check("generate returns 423 while the switch is ON", r.status_code == 423, r.text)
        check("no model call while ON", model_calls() == 0)
        check("edit returns 423 while ON", client.put(f"/landing/{kid}", json={"headline": "x"}).status_code == 423)
        check("page and screenshots still readable while ON", client.get(f"/landing/{kid}/page").status_code == 200)
        client.post("/killswitch", json={"on": False})
        check("generate works again when OFF", gen(client, p1).status_code == 200)
    else:
        print("SKIP  no app/killswitch.py in this repo; the landing routes use it automatically once it exists")
        check("without a kill switch the guard list is empty", landing.GUARD == [])

print()
print("FAILS:", FAILS)
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILS else 0)
