"""
Transcript Agent — handles subtitle/caption extraction for video sources.

Job types claimed:
  video_transcript    → get_video_transcript, extract_timestamped_segments
  transcript_quality  → check_transcript_quality

Run standalone:  python -m agents.transcript_agent
Or via fleet:    python -m agents.run_fleet
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import job_store
import yt_dlp

_POLL_INTERVAL = 5
_RETRY_BACKOFF = [10, 30, 60, 120]


def _make_opts(**extra) -> dict:
    return {"quiet": True, "cachedir": False, **extra}


def _handle_video_transcript(payload: dict) -> dict:
    url = payload["url"]
    language = payload.get("language", "en")
    max_segments = payload.get("max_segments")
    timestamped = payload.get("timestamped", False)

    opts = _make_opts(
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=[language],
        skip_download=True,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title")
    duration = info.get("duration")
    subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})

    transcript_url = None
    if language in subtitles:
        for fmt in subtitles[language]:
            if fmt.get("ext") in ("vtt", "srv3", "json3"):
                transcript_url = fmt.get("url")
                break

    result = {
        "title": title,
        "uploader": info.get("uploader"),
        "duration_seconds": duration,
        "upload_date": info.get("upload_date"),
        "url": url,
        "transcript_url": transcript_url,
        "has_transcript": transcript_url is not None,
    }

    if timestamped and transcript_url and max_segments:
        result["segments"] = _fetch_timestamped_segments(
            subtitles, language, title, url, duration, max_segments
        )

    return result


def _fetch_timestamped_segments(
    subtitles: dict,
    language: str,
    title: str,
    url: str,
    duration: int | None,
    max_segments: int,
) -> list[dict]:
    sub_url = None
    for fmt in subtitles.get(language, []):
        if fmt.get("ext") == "json3":
            sub_url = fmt["url"]
            break
    if not sub_url and subtitles.get(language):
        sub_url = subtitles[language][0]["url"]
    if not sub_url:
        return []

    try:
        with urllib.request.urlopen(sub_url, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        return []

    segments: list[dict] = []
    try:
        data = json.loads(raw)
        for event in data.get("events", []):
            segs = event.get("segs")
            if not segs:
                continue
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if not text or text == "\n":
                continue
            start_ms = event.get("tStartMs", 0)
            minutes = start_ms // 60000
            seconds = (start_ms % 60000) // 1000
            segments.append({
                "timestamp": f"~{minutes}:{seconds:02d}",
                "start_ms": start_ms,
                "text": text,
            })
            if len(segments) >= max_segments:
                break
    except Exception:
        segments = [{"timestamp": "N/A", "text": raw[:2000]}]

    return segments


def _handle_transcript_quality(payload: dict) -> dict:
    url = payload["url"]
    with yt_dlp.YoutubeDL(_make_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    subtitles = info.get("subtitles", {})
    auto_captions = info.get("automatic_captions", {})
    has_manual = bool(subtitles)
    has_auto = bool(auto_captions)

    return {
        "title": info.get("title"),
        "has_manual_transcript": has_manual,
        "has_auto_transcript": has_auto,
        "has_any_transcript": has_manual or has_auto,
        "manual_transcript_languages": list(subtitles.keys()),
        "auto_transcript_languages": list(auto_captions.keys()),
        "recommended_action": (
            "ingest" if has_manual
            else "ingest_with_caution" if has_auto
            else "flag_for_admin"
        ),
        "duration_seconds": info.get("duration"),
    }


_HANDLERS = {
    "video_transcript": _handle_video_transcript,
    "transcript_quality": _handle_transcript_quality,
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
        print(f"[transcript] done  {job_id} ({job['type']})")
    except Exception as exc:
        requeued = job_store.requeue_failed(job_id)
        if requeued:
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            print(f"[transcript] retry {job_id} attempt={attempt} sleep={backoff}s — {exc}")
            time.sleep(backoff)
        else:
            job_store.fail_job(job_id, str(exc))
            print(f"[transcript] fail  {job_id} — {exc}")

    return True


def loop() -> None:
    print("[transcript] agent started")
    while True:
        processed = run_once()
        if not processed:
            time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    loop()
