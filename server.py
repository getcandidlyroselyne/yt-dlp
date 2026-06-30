from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from fastmcp import FastMCP

import job_store

mcp = FastMCP("yt-dlp")

_FFMPEG_BUILDS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)
_FFMPEG_CACHE: str | None = None


def _disk_free_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return 0.0


def _resolve_ffmpeg_location() -> str | None:
    """Return ffmpeg directory for yt-dlp, bootstrapping static builds if needed."""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE or None

    env_location = os.environ.get("FFMPEG_LOCATION")
    if env_location:
        _FFMPEG_CACHE = env_location
        return env_location

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

    if _disk_free_mb(cache_dir if cache_dir.exists() else cache_dir.parent) < 200:
        _FFMPEG_CACHE = ""
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.tar.xz"
        with urllib.request.urlopen(_FFMPEG_BUILDS_URL, timeout=120) as response:
            archive.write_bytes(response.read())
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall(tmp, filter="data")
        build_root = next(Path(tmp).glob("ffmpeg-*-linux64-gpl"))
        for name, dest in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
            shutil.copy2(build_root / "bin" / name, dest)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _FFMPEG_CACHE = str(cache_dir)
    return _FFMPEG_CACHE


# ─────────────────────────────────────────────
# Job Queue — Coordination tools for agents
# ─────────────────────────────────────────────

@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """
    Check the status of an async job submitted to the fleet.
    Poll this after calling any ingestion tool until status is 'done' or 'failed'.
    Returns the full job record including result when complete.
    """
    job = job_store.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id!r} not found", "status": "unknown"}
    return job


@mcp.tool()
def list_queued_jobs(status: str = "") -> list:
    """
    List all jobs in the fleet queue.
    Pass status='queued', 'running', 'done', or 'failed' to filter.
    Useful for monitoring fleet health and diagnosing stuck jobs.
    """
    return job_store.list_jobs(status or None)


@mcp.tool()
def purge_completed_jobs(older_than_seconds: int = 3600) -> dict:
    """
    Remove completed and failed jobs older than the given age in seconds.
    Call periodically to keep the job store clean. Default: 1 hour.
    """
    purged = job_store.purge_done(older_than_seconds)
    return {"purged": purged, "older_than_seconds": older_than_seconds}


@mcp.tool()
def retry_failed_job(job_id: str) -> dict:
    """
    Manually requeue a failed job for another attempt by the fleet.
    Use this when a job failed due to a transient error (rate limit, network).
    """
    job = job_store.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id!r} not found"}
    if job["status"] != "failed":
        return {"error": f"Job {job_id!r} is {job['status']!r}, not 'failed'"}
    job_store.requeue_failed(job_id, max_attempts=99)
    return {"job_id": job_id, "status": "requeued"}


# ─────────────────────────────────────────────
# Workstream B: Ingestion & Normalization
# All tools below enqueue work to the agent fleet — no blocking I/O here.
# ─────────────────────────────────────────────

@mcp.tool()
def get_video_transcript(url: str, language: str = "en") -> dict:
    """
    Queue extraction of a video transcript/subtitles for filtering and summarization.
    Used for YouTube sources in the AI News Digest pipeline.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will contain: title, uploader, duration_seconds, upload_date, transcript_url, has_transcript.
    """
    job_id = job_store.enqueue("video_transcript", {"url": url, "language": language})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "language": language,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def get_podcast_transcript(url: str, audio_format: str = "mp3") -> dict:
    """
    Queue download of a podcast episode audio file for transcript generation.
    Used for podcast sources in the AI News Digest ingestion pipeline.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will contain: title, uploader, duration_seconds, output_dir.
    """
    job_id = job_store.enqueue("podcast_audio", {"url": url, "audio_format": audio_format})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "audio_format": audio_format,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def get_source_metadata(url: str) -> dict:
    """
    Queue metadata extraction from any yt-dlp supported source without downloading.
    Used to validate a new source before adding it to the approved source list.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will contain: title, uploader, upload_date, duration_seconds, view_count, etc.
    """
    job_id = job_store.enqueue("source_metadata", {"url": url})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def get_playlist_items(playlist_url: str, max_items: int = 20) -> dict:
    """
    Queue fetching of all video/episode entries from a YouTube playlist or channel.
    Used to monitor video sources for new content during the daily ingestion refresh.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will be a list of: title, url, upload_date, duration_seconds, id.
    """
    job_id = job_store.enqueue("playlist_items", {"url": playlist_url, "max_items": max_items})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": playlist_url,
        "max_items": max_items,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def check_transcript_quality(url: str) -> dict:
    """
    Queue a check of whether a video/podcast source has a usable transcript.
    Returns quality indicators to decide whether to ingest or flag for admin review.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will contain: has_manual_transcript, has_auto_transcript, recommended_action.
    """
    job_id = job_store.enqueue("transcript_quality", {"url": url})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


# ─────────────────────────────────────────────
# Workstream A: Source List & Schema support
# ─────────────────────────────────────────────

@mcp.tool()
def validate_source(url: str) -> dict:
    """
    Queue validation of a candidate source URL before adding to the approved source list.
    Checks reachability, source type, and transcript availability.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will contain: reachable, source_type, extractor, title, item_count, suggested_schema_fields.
    """
    job_id = job_store.enqueue("validate_source", {"url": url})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def list_formats(url: str) -> dict:
    """
    Queue listing of all available media formats for a given URL.
    Useful for determining the best ingestion format per source type.
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    Result will be a list of format dicts: format_id, ext, resolution, filesize, vcodec, acodec.
    """
    job_id = job_store.enqueue("list_formats", {"url": url})
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


# ─────────────────────────────────────────────
# Workstream C: Filtering, Summarization & Slack Output
# These are pure-compute — no I/O, run inline synchronously.
# ─────────────────────────────────────────────

@mcp.tool()
def keyword_filter(
    text: str,
    keywords: list[str],
    monitoring_context: str = "",
) -> dict:
    """
    Phase 1 of the two-pass filtering strategy: keyword match.
    Scans transcript/article text for any of the provided keywords
    tied to the source's monitoring context. Returns matched keywords,
    match count, and whether to proceed to LLM judgment pass.
    Per project spec: always run LLM pass even if no keywords match.
    """
    text_lower = text.lower()
    matches = {}
    for kw in keywords:
        pattern = re.compile(re.escape(kw.lower()))
        found = pattern.findall(text_lower)
        if found:
            matches[kw] = len(found)

    return {
        "keyword_matches": matches,
        "match_count": sum(matches.values()),
        "matched_keywords": list(matches.keys()),
        "has_keyword_match": bool(matches),
        "monitoring_context": monitoring_context,
        "recommendation": (
            "proceed_to_llm_with_signal" if matches
            else "proceed_to_llm_no_signal"
        ),
        "note": "Always run LLM judgment pass regardless of keyword result.",
    }


@mcp.tool()
def extract_timestamped_segments(
    url: str,
    language: str = "en",
    max_segments: int = 10,
) -> dict:
    """
    Queue extraction of timestamped subtitle segments from a video/podcast.
    Used to produce timestamp-anchored bullets in the digest
    (e.g. '[~3:20] Sourdough commands 40% price premium').
    Returns immediately with a job_id. Poll get_job_status(job_id) until done.
    """
    job_id = job_store.enqueue(
        "video_transcript",
        {"url": url, "language": language, "max_segments": max_segments, "timestamped": True},
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "note": "Poll get_job_status(job_id) every 10s until status is 'done'.",
    }


@mcp.tool()
def score_relevance(
    text: str,
    monitoring_context: str,
    keyword_matches: list[str],
    source_title: str = "",
) -> dict:
    """
    Compute a rubric relevance score (0–100) for a piece of content
    against a source's monitoring context. Combines keyword signal
    weight with context specificity. Used as the pass/fail gate before
    a digest item is included. Per project spec: top-N items selected
    when too many qualify; threshold can loosen slightly on low-news days.
    """
    score = 0
    rationale = []

    kw_score = min(len(keyword_matches) * 10, 40)
    score += kw_score
    if keyword_matches:
        rationale.append(
            f"Keyword matches ({', '.join(keyword_matches[:5])}): +{kw_score}"
        )

    context_words = set(monitoring_context.lower().split())
    text_words = set(text.lower().split())
    overlap = context_words & text_words
    context_score = min(int((len(overlap) / max(len(context_words), 1)) * 40), 40)
    score += context_score
    if overlap:
        rationale.append(
            f"Context overlap ({len(overlap)} shared terms): +{context_score}"
        )

    length_score = min(len(text.split()) // 50, 20)
    score += length_score
    rationale.append(f"Content length signal: +{length_score}")

    default_threshold = 50
    passes = score >= default_threshold

    return {
        "source_title": source_title,
        "score": score,
        "max_score": 100,
        "passes_threshold": passes,
        "threshold_used": default_threshold,
        "rationale": rationale,
        "monitoring_context": monitoring_context,
        "recommendation": "include_in_digest" if passes else "exclude",
        "note": (
            "Consider lowering threshold to 35 on low-news days per project spec."
            if not passes else ""
        ),
    }


@mcp.tool()
def format_digest_item(
    title: str,
    source_url: str,
    uploader: str,
    source_type: str,
    bullets: list[str],
    relevance_score: int,
    monitoring_context: str,
    upload_date: str = "",
    duration_seconds: int = 0,
) -> dict:
    """
    Format a single digest item into the standard Slack-ready structure.
    Applies project digest format rules: 5–10 bullets, timestamps for
    audio/video, source attribution, and a 'why it's relevant' line
    referencing the monitoring context and rubric score.
    """
    if len(bullets) > 10:
        bullets = bullets[:10]

    duration_str = ""
    if duration_seconds:
        mins = duration_seconds // 60
        duration_str = f"{mins} min"

    source_label = {
        "video": "Video",
        "podcast": "Podcast",
        "article": "Article",
        "newsletter": "Newsletter",
        "rss": "Article",
    }.get(source_type.lower(), source_type.capitalize())

    meta = f"[{uploader} · {source_label}" + (f" · {duration_str}" if duration_str else "") + "]"
    if upload_date and len(upload_date) == 8:
        formatted_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        meta += f" · {formatted_date}"

    bullet_lines = "\n".join(f"- {b}" for b in bullets)

    slack_block = (
        f"*{title}*\n"
        f"{meta}\n"
        f"_{monitoring_context}_\n\n"
        f"{bullet_lines}\n\n"
        f"🔗 {source_url}\n"
        f"📊 Relevance score: {relevance_score}/100"
    )

    return {
        "title": title,
        "source_url": source_url,
        "uploader": uploader,
        "source_type": source_type,
        "bullet_count": len(bullets),
        "bullets": bullets,
        "relevance_score": relevance_score,
        "monitoring_context": monitoring_context,
        "slack_formatted_block": slack_block,
        "passes_bullet_guardrail": 5 <= len(bullets) <= 10,
    }


@mcp.tool()
def build_digest(
    items: list[dict],
    issue_number: int,
    publish_date: str,
    max_items: int = 10,
) -> dict:
    """
    Assemble a full daily digest from a list of scored and formatted items.
    Sorts by relevance score (top-N selection per project spec), applies
    the item cap, and renders the complete Slack message ready to post
    to the engineering channel at 6:00 AM.
    """
    sorted_items = sorted(
        items, key=lambda x: x.get("relevance_score", 0), reverse=True
    )[:max_items]

    if not sorted_items:
        return {
            "issue_number": issue_number,
            "publish_date": publish_date,
            "item_count": 0,
            "slack_message": None,
            "post_decision": "skip — no qualifying items today; logged per spec.",
        }

    header = (
        f"🤖 *The Daily Crumb — Issue #{issue_number} · {publish_date}*\n"
        f"Your daily AI news digest for the Candidly engineering team.\n"
        f"{'─' * 40}"
    )

    body_parts = [item.get("slack_formatted_block", "") for item in sorted_items]
    footer = (
        f"{'─' * 40}\n"
        f"📋 Full source list & attribution log in Notion.\n"
        f"💬 Feedback? DM this bot directly."
    )

    full_message = f"{header}\n\n" + "\n\n---\n\n".join(body_parts) + f"\n\n{footer}"

    return {
        "issue_number": issue_number,
        "publish_date": publish_date,
        "item_count": len(sorted_items),
        "items_included": [i.get("title") for i in sorted_items],
        "slack_message": full_message,
        "post_decision": "post",
        "char_count": len(full_message),
    }


@mcp.tool()
def check_duplicate(
    item_url: str,
    item_title: str,
    post_log: list[dict],
) -> dict:
    """
    Check whether a digest item has already been posted, using the
    in-memory post log. In production this queries the AWS Post Log
    table. Prevents duplicate items across daily digest issues per
    project spec deduplication requirement.
    """
    for entry in post_log:
        if entry.get("url") == item_url or entry.get("title") == item_title:
            return {
                "is_duplicate": True,
                "matched_on": "url" if entry.get("url") == item_url else "title",
                "previously_posted_in_issue": entry.get("issue_number"),
                "previously_posted_date": entry.get("publish_date"),
                "recommendation": "exclude",
            }
    return {
        "is_duplicate": False,
        "recommendation": "include",
        "url": item_url,
        "title": item_title,
    }


if __name__ == "__main__":
    mcp.run()
