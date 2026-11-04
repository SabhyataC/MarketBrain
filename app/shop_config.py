"""One editable config describes the business, so nothing else is hardcoded.

Later steps read it to build the extraction prompt, the review screen columns
and the ad rules. The owner can change it any time, or start from a preset.
"""
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.audit import log

router = APIRouter(prefix="/config", tags=["config"])

PATH = Path("data/shop_config.json")


class Axis(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1)          # e.g. "size"
    values: list[str] | None = None          # None = free text, list = allowed values


class ShopConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    business_type: str = Field(min_length=1)
    currency: str = "Rs"
    categories: list[str] = Field(min_length=1)
    variant_axes: list[Axis] = []            # what changes price or stock
    attributes: list[str] = []               # extra facts worth recording


DEFAULT = ShopConfig(
    business_type="small shop",
    categories=["other"],
)

PRESETS: dict[str, ShopConfig] = {
    "general": DEFAULT,
    "clothing": ShopConfig(
        business_type="clothing boutique",
        categories=["saree", "kurta", "dress", "top", "bottom", "shirt", "jeans", "other"],
        variant_axes=[
            Axis(name="colour"),
            Axis(name="size", values=["XS", "S", "M", "L", "XL", "XXL", "Free size"]),
        ],
        attributes=["fabric", "pattern", "occasion"],
    ),
    "grocery": ShopConfig(
        business_type="grocery and packaged goods shop",
        categories=["food", "beverages", "snacks", "personal care", "home", "other"],
        variant_axes=[Axis(name="pack size")],
        attributes=["ingredients", "shelf life"],
    ),
    "hardware": ShopConfig(
        business_type="hardware store",
        categories=["tools", "paint", "plumbing", "electrical", "fasteners", "other"],
        variant_axes=[Axis(name="size"), Axis(name="colour")],
        attributes=["material", "warranty"],
    ),
}


def load() -> dict:
    """The saved config, or the default if none was saved or the file is broken."""
    try:
        return ShopConfig(**json.loads(PATH.read_text())).model_dump()
    except (OSError, ValueError):
        return DEFAULT.model_dump()


def _save(cfg: ShopConfig):
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(cfg.model_dump(), indent=2))


@router.get("")
def get_config():
    return load()


@router.put("")
def put_config(cfg: ShopConfig):
    _save(cfg)
    log("owner", "config_saved", f"business_type={cfg.business_type}")
    return cfg.model_dump()


@router.get("/presets")
def list_presets():
    return {name: p.model_dump() for name, p in PRESETS.items()}


@router.post("/preset/{name}")
def use_preset(name: str):
    if name not in PRESETS:
        raise HTTPException(404, f"Unknown preset. Choose from: {', '.join(PRESETS)}")
    _save(PRESETS[name])
    log("owner", "config_preset_applied", name)
    return PRESETS[name].model_dump()