from __future__ import annotations

import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from fastmcp import FastMCP
import yt_dlp

mcp = FastMCP("yt-dlp")

_FFMPEG_BUILDS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)
_FFMPEG_CACHE: str | None = None


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


def make_ydl_opts(**options):
    opts = {"quiet": True, **options}
    ffmpeg_location = _resolve_ffmpeg_location()
    if ffmpeg_location:
        opts.setdefault("ffmpeg_location", ffmpeg_location)
    return opts


# ─────────────────────────────────────────────
# Workstream B: Ingestion & Normalization
# ─────────────────────────────────────────────

@mcp.tool()
def get_video_transcript(url: str, language: str = "en") -> dict:
    """
    Download a video and extract its transcript/subtitles for filtering
    and summarization. Used for YouTube sources in the AI News Digest pipeline.
    Returns transcript text, title, uploader, and duration.
    """
    ydl_opts = make_ydl_opts(
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=[language],
        skip_download=True,
    )
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})
        transcript_url = None
        if language in subtitles:
            for fmt in subtitles[language]:
                if fmt.get("ext") in ("vtt", "srv3", "json3"):
                    transcript_url = fmt.get("url")
                    break
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "url": url,
            "transcript_url": transcript_url,
            "has_transcript": transcript_url is not None,
        }


@mcp.tool()
def get_podcast_transcript(url: str, audio_format: str = "mp3") -> dict:
    """
    Download a podcast episode audio file for transcript generation.
    Used for podcast sources (e.g., RSS audio feeds) in the AI News Digest
    ingestion pipeline. Returns file path and episode metadata.
    """
    import tempfile, os
    output_dir = tempfile.mkdtemp()
    ydl_opts = make_ydl_opts(
        format="bestaudio/best",
        outtmpl=os.path.join(output_dir, "%(title)s.%(ext)s"),
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
        }],
    )
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "description": info.get("description", "")[:500],
            "output_dir": output_dir,
            "url": url,
        }


@mcp.tool()
def get_source_metadata(url: str) -> dict:
    """
    Extract metadata from any yt-dlp supported source (YouTube video,
    podcast, RSS feed item) without downloading. Used to validate a new
    source before adding it to the approved source list.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "duration_seconds": info.get("duration"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "description": info.get("description", "")[:800],
            "webpage_url": info.get("webpage_url"),
            "extractor": info.get("extractor"),
            "is_live": info.get("is_live", False),
            "availability": info.get("availability"),
        }


@mcp.tool()
def get_playlist_items(playlist_url: str, max_items: int = 20) -> list:
    """
    Fetch all video/episode entries from a YouTube playlist or channel.
    Used to monitor video sources (e.g., a YouTube channel) for new
    content during the daily ingestion refresh cycle (4x/day).
    """
    ydl_opts = make_ydl_opts(extract_flat=True, playlistend=max_items)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        entries = info.get("entries", [])
        return [
            {
                "title": e.get("title"),
                "url": e.get("url") or e.get("webpage_url"),
                "upload_date": e.get("upload_date"),
                "duration_seconds": e.get("duration"),
                "id": e.get("id"),
            }
            for e in entries if e
        ]


@mcp.tool()
def check_transcript_quality(url: str) -> dict:
    """
    Check whether a video/podcast source has a usable transcript.
    Returns quality indicators to decide whether to ingest or flag
    the source for admin review. Per project spec: discard/flag sources
    with poor or missing transcripts.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        subtitles = info.get("subtitles", {})
        auto_captions = info.get("automatic_captions", {})
        has_manual = bool(subtitles)
        has_auto = bool(auto_captions)
        languages_manual = list(subtitles.keys())
        languages_auto = list(auto_captions.keys())
        return {
            "title": info.get("title"),
            "has_manual_transcript": has_manual,
            "has_auto_transcript": has_auto,
            "has_any_transcript": has_manual or has_auto,
            "manual_transcript_languages": languages_manual,
            "auto_transcript_languages": languages_auto,
            "recommended_action": (
                "ingest" if has_manual
                else "ingest_with_caution" if has_auto
                else "flag_for_admin"
            ),
            "duration_seconds": info.get("duration"),
        }


# ─────────────────────────────────────────────
# Workstream A: Source List & Schema support
# ─────────────────────────────────────────────

@mcp.tool()
def validate_source(url: str) -> dict:
    """
    Validate a candidate source URL before adding it to the approved
    source list. Checks reachability, source type, and transcript
    availability. Returns a structured report to support the admin
    vetting process (credibility rubric + monitoring context review).
    """
    ydl_opts = make_ydl_opts(extract_flat=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
    except Exception as e:
        return {
            "url": url,
            "reachable": False,
            "error": str(e),
            "recommended_action": "do_not_add",
        }


@mcp.tool()
def list_formats(url: str) -> list:
    """
    List all available media formats for a given URL.
    Useful for determining the best ingestion format per source type
    (prefer RSS/clean feeds; use audio for podcasts; video for YouTube).
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
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
            for f in formats
        ]


if __name__ == "__main__":
    _resolve_ffmpeg_location()
    mcp.run()


# ─────────────────────────────────────────────
# Workstream C: Filtering, Summarization & Slack Output
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
    import re
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
    Extract timestamped subtitle segments from a video/podcast.
    Used to produce timestamp-anchored bullets in the digest
    (e.g. '[~3:20] Sourdough commands 40% price premium').
    Per project spec: every podcast/video bullet must include a timestamp
    so readers can jump directly to the relevant moment.
    """
    import urllib.request, json, re

    ydl_opts = make_ydl_opts(
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=[language],
        skip_download=True,
    )
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title")
        duration = info.get("duration")

        subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})
        if language not in subtitles:
            return {
                "title": title,
                "url": url,
                "segments": [],
                "error": f"No subtitles found for language: {language}",
            }

        # Prefer json3 format for structured timestamps
        sub_url = None
        for fmt in subtitles[language]:
            if fmt.get("ext") == "json3":
                sub_url = fmt["url"]
                break
        if not sub_url:
            sub_url = subtitles[language][0]["url"]

        try:
            with urllib.request.urlopen(sub_url) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:
            return {"title": title, "url": url, "segments": [], "error": str(e)}

        # Parse json3 subtitle format
        segments = []
        try:
            data = json.loads(raw)
            events = data.get("events", [])
            for event in events[:max_segments * 3]:
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
            # Fallback: return raw text with no timestamps
            segments = [{"timestamp": "N/A", "text": raw[:2000]}]

        return {
            "title": title,
            "url": url,
            "duration_seconds": duration,
            "language": language,
            "segments": segments,
            "segment_count": len(segments),
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

    # Keyword signal: up to 40 points
    kw_score = min(len(keyword_matches) * 10, 40)
    score += kw_score
    if keyword_matches:
        rationale.append(
            f"Keyword matches ({', '.join(keyword_matches[:5])}): +{kw_score}"
        )

    # Monitoring context overlap: up to 40 points
    context_words = set(monitoring_context.lower().split())
    text_words = set(text.lower().split())
    overlap = context_words & text_words
    context_score = min(int((len(overlap) / max(len(context_words), 1)) * 40), 40)
    score += context_score
    if overlap:
        rationale.append(
            f"Context overlap ({len(overlap)} shared terms): +{context_score}"
        )

    # Content length signal: up to 20 points (penalise very short content)
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
    # Enforce bullet count guardrails (5–10 per spec)
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
    # Sort by relevance score descending, cap at max_items
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

    body_parts = []
    for item in sorted_items:
        body_parts.append(item.get("slack_formatted_block", ""))

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
