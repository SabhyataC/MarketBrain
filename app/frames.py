import os
import subprocess
from pathlib import Path

# Every frame becomes one paid model call, so keep these small.
# Override in .env if needed.
FRAME_FPS = float(os.getenv("FRAME_FPS", "1.0"))              # one frame a second: a tag is readable for only a second or two
MAX_FRAMES = int(os.getenv("MAX_FRAMES", "40"))               # cap per video
FRAME_MAX_WIDTH = int(os.getenv("FRAME_MAX_WIDTH", "1280"))   # shrink big frames


def _duration(video_path: str) -> float | None:
    """Video length in seconds, or None if ffprobe is missing or fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def sample_frames(video_path: str, out_dir: str, fps: float | None = None,
                  max_frames: int | None = None) -> list[str]:
    fps = fps or FRAME_FPS
    max_frames = max_frames or MAX_FRAMES
    duration = _duration(video_path)
    if duration and duration * fps > max_frames:
        fps = max_frames / duration   # spread the cap across the whole video

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("frame_*.jpg")) + list(out.glob("thumb_*.jpg")):
        old.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video_path,
         "-vf", f"fps={fps:.4f},scale='min({FRAME_MAX_WIDTH},iw)':-2",
         "-frames:v", str(max_frames), "-q:v", "2",
         str(out / "frame_%03d.jpg")],
        check=True,
    )
    return sorted(str(p) for p in out.glob("frame_*.jpg"))


def crop_around(frame_path: str, x: float | None, out_path: str, width_frac: float = 0.34) -> bool:
    """Save a full-height strip of the frame centred on x (0 to 1), so a row shows just its own product."""
    x = 0.5 if x is None else min(max(float(x), 0.0), 1.0)
    w = width_frac
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", frame_path, "-vf",
             f"crop=w=iw*{w}:h=ih:x='max(0,min(iw-iw*{w},iw*{x:.4f}-iw*{w / 2}))':y=0",
             "-q:v", "2", out_path],
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False