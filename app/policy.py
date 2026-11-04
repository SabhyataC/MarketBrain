import json
import re
from app.model_client import ask
from app.catalog import parse_json_list
from app.calllog import purpose

RULES = [
    (r"\b(cure|cures|heal|heals|prevent|prevents|clinically|dermatologist|doctor)\b", "medical claim"),
    (r"\b(guarantee|guaranteed|miracle|proven)\b", "guarantee or proof claim"),
    (r"(#1|\bbest\b|\bnumber one\b)", "superlative claim"),
    (r"\b(free|\d+\s?% off|limited time|hurry|last chance)\b", "fake offer or urgency"),
    (r"\b(anti-aging|weight loss|lose weight)\b", "body or health claim"),
]

CHECK_PROMPT = """Product facts (the ONLY verified facts): {facts}
Below are ad variants. For each, list product attributes or claims that are
NOT supported by the facts, but ONLY physical product attributes: scent,
ingredients, texture, or effects on the user. Do not flag mood, lifestyle,
or gifting language.
Ignore the name, brand and price. Return ONLY a JSON array, one item per variant:
{{"index": <number>, "unsupported": ["claim", ...]}}
Variants:
{variants}"""


def check_variants(variants: list[dict], facts: dict) -> list[dict]:
    texts = [f"{v['headline']} {v['text']}" for v in variants]
    for v, t in zip(variants, texts):
        v["flags"] = [label for pat, label in RULES if re.search(pat, t, re.I)]
        v["blocked"] = bool(v["flags"])
    listing = json.dumps([{"index": i, "text": t} for i, t in enumerate(texts)])
    with purpose("policy_check"):
        raw = ask(CHECK_PROMPT.format(facts=json.dumps(facts), variants=listing))
    result = parse_json_list(raw)
    for r in result:
        i = r.get("index")
        if isinstance(i, int) and 0 <= i < len(variants):
            for claim in r.get("unsupported", []):
                variants[i]["flags"].append(f"unsupported claim: {claim}")
    return variants