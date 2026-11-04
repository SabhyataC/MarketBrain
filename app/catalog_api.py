import json
import shutil
import subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from app.db import get_conn
from app.frames import sample_frames, crop_around
from app.catalog import build_catalog

from fastapi import Depends
from app.killswitch import require_off

router = APIRouter(prefix="/catalog", tags=["catalog"])

DATA_DIR = Path("data")
FRAMES_DIR = Path("data/frames")


class VideoIn(BaseModel):
    video_path: str
    notes: str = ""


def _keep_thumbs(pid: int, items: list[dict]):
    folder = FRAMES_DIR / f"draft_{pid}"
    folder.mkdir(parents=True, exist_ok=True)
    for it in items:
        src = Path(it.get("thumb") or it.get("best_frame") or "")
        if src.is_file():
            shutil.copyfile(src, folder / src.name)
            it["thumb"] = f"data/frames/draft_{pid}/{src.name}"


@router.post("/from-video", dependencies=[Depends(require_off)])
def from_video(body: VideoIn):
    video = Path(body.video_path).resolve()
    if DATA_DIR.resolve() not in video.parents or not video.is_file():
        raise HTTPException(400, "Videos must be uploaded through the page or placed inside the data folder.")
    try:
        frames = sample_frames(str(video), str(FRAMES_DIR))
    except FileNotFoundError:
        raise HTTPException(500, "ffmpeg is not installed or not on PATH.")
    except subprocess.CalledProcessError:
        raise HTTPException(400, "That file could not be read as a video.")
    if not frames:
        raise HTTPException(400, "Could not get any frames from this video")
    errors: list = []
    items = build_catalog(frames, errors, body.notes)
    if len(errors) == len(frames):
        raise HTTPException(502, f"The model could not read any frame. First error: {errors[0][1]}")
    for n, it in enumerate(items, 1):
        thumb = f"data/frames/thumb_{n:03d}.jpg"
        it["thumb"] = thumb if crop_around(it["best_frame"], it.get("best_x"), thumb) else it["best_frame"]
    flagged = sum(1 for it in items if it["check"])
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO proposals (kind, payload) VALUES (?, ?)",
            ("catalog_draft", json.dumps({"items": items})),
        )
        pid = cur.lastrowid
        _keep_thumbs(pid, items)
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps({"items": items}), pid))
        conn.execute(
            "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
            ("ai", "catalog_draft_created",
             f"proposal {pid}, {len(items)} items, {flagged} to check, "
             f"{len(frames) - len(errors)}/{len(frames)} frames read"),
        )
    return {
        "proposal_id": pid, "count": len(items), "items": items,
        "frames_read": len(frames) - len(errors), "frames_failed": len(errors),
    }


class DraftEdit(BaseModel):
    items: list[dict]


@router.put("/draft/{pid}")
def edit_draft(pid: int, body: DraftEdit):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, kind FROM proposals WHERE id = ?", (pid,)
        ).fetchone()
        if not row or row["kind"] != "catalog_draft":
            raise HTTPException(404, "Catalog draft not found")
        if row["status"] != "pending":
            raise HTTPException(400, f"Already {row['status']}")
        # keep what the model first produced, so the report can show how much the owner fixed
        stored = json.loads(conn.execute("SELECT payload FROM proposals WHERE id = ?", (pid,)).fetchone()["payload"])
        original = stored.get("original") or stored.get("items", [])
        conn.execute(
            "UPDATE proposals SET payload = ? WHERE id = ?",
            (json.dumps({"items": body.items, "original": original}), pid),
        )
        conn.execute(
            "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
            ("owner", "catalog_draft_edited", f"proposal {pid}, {len(body.items)} items"),
        )
    return {"proposal_id": pid, "count": len(body.items)}


@router.post("/upload", dependencies=[Depends(require_off)])
def upload(file: UploadFile = File(...), notes: str = Form("")):
    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / "uploaded_video.mp4"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return from_video(VideoIn(video_path=str(dest), notes=notes))