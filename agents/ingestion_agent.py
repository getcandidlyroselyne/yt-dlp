"""
Ingestion Agent — handles all metadata-only yt-dlp calls.

Job types claimed:
  source_metadata   → get_source_metadata
  validate_source   → validate_source
  list_formats      → list_formats
  playlist_items    → get_playlist_items

Run standalone:  python -m agents.ingestion_agent
Or via fleet:    python -m agents.run_fleet
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

import job_store
import yt_dlp

_POLL_INTERVAL = 5   # seconds between queue checks
_RETRY_BACKOFF = [10, 30, 60, 120]  # sleep before each retry attempt


def _make_opts(**extra) -> dict:
    return {"quiet": True, "cachedir": False, **extra}


def _handle_source_metadata(payload: dict) -> dict:
    url = payload["url"]
    with yt_dlp.YoutubeDL(_make_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": (info.get("description") or "")[:800],
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor"),
        "is_live": info.get("is_live", False),
        "availability": info.get("availability"),
    }


def _handle_validate_source(payload: dict) -> dict:
    url = payload["url"]
    try:
        with yt_dlp.YoutubeDL(_make_opts(extract_flat=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        extractor = info.get("extractor", "unknown")
        is_playlist = info.get("_type") == "playlist"
        entries = info.get("entries", [])
        return {
            "url": url,
            "reachable": True,
            "source_type": (
                "playlist_or_channel" if is_playlist
                else "single_video" if extractor in ("youtube", "vimeo")
                else "podcast_or_audio" if extractor in ("soundcloud", "buzzsprout", "simplecast")
                else "article_or_rss"
            ),
            "extractor": extractor,
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "item_count": len(entries) if is_playlist else 1,
            "suggested_schema_fields": {
                "source_link": url,
                "monitoring_contexts": [],
                "why_it_matters": "",
                "cadence": info.get("upload_frequency", "unknown"),
                "last_successful_fetch": None,
                "error_state": None,
            },
        }
    except Exception as exc:
        return {
            "url": url,
            "reachable": False,
            "error": str(exc),
            "recommended_action": "do_not_add",
        }


def _handle_list_formats(payload: dict) -> list:
    url = payload["url"]
    with yt_dlp.YoutubeDL(_make_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    return [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "filesize": f.get("filesize"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "tbr": f.get("tbr"),
        }
        for f in info.get("formats", [])
    ]


def _handle_playlist_items(payload: dict) -> list:
    url = payload["url"]
    max_items = payload.get("max_items", 20)
    opts = _make_opts(extract_flat=True, playlistend=max_items)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [
        {
            "title": e.get("title"),
            "url": e.get("url") or e.get("webpage_url"),
            "upload_date": e.get("upload_date"),
            "duration_seconds": e.get("duration"),
            "id": e.get("id"),
        }
        for e in (info.get("entries") or [])
        if e
    ]


_HANDLERS = {
    "source_metadata": _handle_source_metadata,
    "validate_source": _handle_validate_source,
    "list_formats": _handle_list_formats,
    "playlist_items": _handle_playlist_items,
}


def run_once() -> bool:
    """Claim and process one job. Returns True if a job was processed."""
    job = job_store.claim_next(set(_HANDLERS))
    if not job:
        return False

    job_id = job["id"]
    attempt = job["attempts"]
    handler = _HANDLERS[job["type"]]

    try:
        result = handler(job["payload"])
        job_store.complete_job(job_id, result)
        print(f"[ingestion] done  {job_id} ({job['type']})")
    except Exception as exc:
        requeued = job_store.requeue_failed(job_id)
        if requeued:
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            print(f"[ingestion] retry {job_id} attempt={attempt} sleep={backoff}s — {exc}")
            time.sleep(backoff)
        else:
            job_store.fail_job(job_id, str(exc))
            print(f"[ingestion] fail  {job_id} — {exc}")

    return True


def loop() -> None:
    print("[ingestion] agent started")
    while True:
        processed = run_once()
        if not processed:
            time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    loop()
