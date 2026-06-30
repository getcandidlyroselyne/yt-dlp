"""
Audio Agent — downloads podcast/audio episodes via yt-dlp + ffmpeg.

Job types claimed:
  podcast_audio → get_podcast_transcript

This is the only agent that performs actual file downloads to disk.
It requires ffmpeg. On Linux it will auto-bootstrap ffmpeg if missing.

Run standalone:  python -m agents.audio_agent
Or via fleet:    python -m agents.run_fleet
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import gcs_utils
import job_store
import yt_dlp

_POLL_INTERVAL = 5
_RETRY_BACKOFF = [15, 45, 90, 180]

_FFMPEG_BUILDS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)
_FFMPEG_CACHE: str | None = None


def _resolve_ffmpeg() -> str | None:
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE or None

    env_loc = os.environ.get("FFMPEG_LOCATION")
    if env_loc:
        _FFMPEG_CACHE = env_loc
        return env_loc

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        _FFMPEG_CACHE = ""
        return None

    cache_dir = Path(os.environ.get("YTDLP_FFMPEG_DIR", "/tmp/yt-dlp-ffmpeg"))
    ffmpeg_bin = cache_dir / "ffmpeg"
    ffprobe_bin = cache_dir / "ffprobe"
    if ffmpeg_bin.is_file() and ffprobe_bin.is_file():
        _FFMPEG_CACHE = str(cache_dir)
        return _FFMPEG_CACHE

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system != "linux" or machine not in {"x86_64", "amd64"}:
        _FFMPEG_CACHE = ""
        return None

    usage = shutil.disk_usage(cache_dir if cache_dir.exists() else cache_dir.parent)
    if usage.free / (1024 * 1024) < 200:
        _FFMPEG_CACHE = ""
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.tar.xz"
        with urllib.request.urlopen(_FFMPEG_BUILDS_URL, timeout=120) as resp:
            archive.write_bytes(resp.read())
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall(tmp, filter="data")
        build_root = next(Path(tmp).glob("ffmpeg-*-linux64-gpl"))
        for name, dest in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
            shutil.copy2(build_root / "bin" / name, dest)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _FFMPEG_CACHE = str(cache_dir)
    return _FFMPEG_CACHE


def _handle_podcast_audio(payload: dict) -> dict:
    url = payload["url"]
    audio_format = payload.get("audio_format", "mp3")

    output_dir = tempfile.mkdtemp()
    opts: dict = {
        "quiet": True,
        "cachedir": False,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
            }
        ],
    }

    ffmpeg_location = _resolve_ffmpeg()
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    gcs = gcs_utils.upload_audio_and_cleanup(
        output_dir=output_dir,
        title=info.get("title") or "",
        audio_format=audio_format,
    )

    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:500],
        "gcs_uri": gcs["gcs_uri"],
        "gcs_download_url": gcs["gcs_download_url"],
        "gcs_object": gcs["gcs_object"],
        "audio_format": audio_format,
        "url": url,
    }


_HANDLERS = {
    "podcast_audio": _handle_podcast_audio,
}


def run_once() -> bool:
    job = job_store.claim_next(set(_HANDLERS))
    if not job:
        return False

    job_id = job["id"]
    attempt = job["attempts"]
    handler = _HANDLERS[job["type"]]

    try:
        result = handler(job["payload"])
        job_store.complete_job(job_id, result)
        print(f"[audio] done  {job_id} ({job['type']})")
    except Exception as exc:
        requeued = job_store.requeue_failed(job_id)
        if requeued:
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            print(f"[audio] retry {job_id} attempt={attempt} sleep={backoff}s — {exc}")
            time.sleep(backoff)
        else:
            job_store.fail_job(job_id, str(exc))
            print(f"[audio] fail  {job_id} — {exc}")

    return True


def loop() -> None:
    print("[audio] agent started")
    while True:
        processed = run_once()
        if not processed:
            time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    loop()
