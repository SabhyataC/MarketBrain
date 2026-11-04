from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import init_db
from app.queue import router as queue_router
from app.products import router as products_router
from app.catalog_api import router as catalog_router
from app.planner import router as planner_router
from app.ads import router as ads_router
from fastapi.staticfiles import StaticFiles
from app.shop_config import router as config_router
from pathlib import Path
from app.report import router as report_router
from app.landing import router as landing_router
from app.meta_draft import router as meta_router
from app.export_csv import router as export_router
from app.shop_sync import router as sync_router
from app.seo import router as seo_router
from app.series import router as series_router
from app.killswitch import router as killswitch_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Marketing Brain", lifespan=lifespan)
app.include_router(queue_router)
app.include_router(products_router)
app.include_router(catalog_router)
app.include_router(planner_router)
app.include_router(ads_router)
app.include_router(config_router)
app.include_router(report_router)
app.include_router(landing_router)
app.include_router(meta_router)
app.include_router(export_router)
app.include_router(sync_router)
app.include_router(seo_router)
app.include_router(series_router)
app.include_router(killswitch_router)
app.mount("/static", StaticFiles(directory="static"), name="static")
for _d in ("data/frames", "data/photos"):          # frames for the review screen, photos kept with products
    Path(_d).mkdir(parents=True, exist_ok=True)
app.mount("/frames", StaticFiles(directory="data/frames"), name="frames")
app.mount("/photos", StaticFiles(directory="data/photos"), name="photos")

@app.get("/health")
def health():
    return {"status": "ok"}

from fastapi.responses import FileResponse

@app.get("/")
def home():
    return FileResponse("static/index.html")
