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
import yt_dlp

import s3_utils

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


def make_ydl_opts(*, require_ffmpeg: bool = False, **options) -> dict:
    opts = {"quiet": True, "cachedir": False, **options}
    if require_ffmpeg:
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
def get_podcast_stream_url(url: str) -> dict:
    """
    Resolve the direct audio stream URL for a podcast episode without downloading
    or writing any files to disk. Zero disk space used.
    Pass the returned stream_url directly to a transcription service (e.g. Whisper API).
    Use this instead of get_podcast_transcript whenever disk space is limited.
    Returns: stream_url, title, uploader, duration_seconds, upload_date, description.
    """
    ydl_opts = make_ydl_opts(format="bestaudio/best")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats") or []
        # Pick the best audio-only format with a direct URL
        audio_formats = [
            f for f in formats
            if f.get("url") and f.get("acodec") != "none"
            and f.get("vcodec") in (None, "none", "")
        ]
        best = audio_formats[-1] if audio_formats else (formats[-1] if formats else {})
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "description": (info.get("description") or "")[:500],
            "stream_url": best.get("url"),
            "audio_ext": best.get("ext"),
            "filesize_bytes": best.get("filesize") or best.get("filesize_approx"),
            "source_url": url,
            "disk_usage": "none — stream directly from stream_url, do not download",
        }


@mcp.tool()
def get_podcast_transcript(url: str, audio_format: str = "mp3") -> dict:
    """
    Extract audio from a podcast episode, upload it directly to S3,
    and return the S3 location for downstream transcription.
    No audio content is retained on local disk after the upload completes.
    Requires the S3_BUCKET environment variable and AWS credentials.
    Returns s3_uri, s3_download_url, and episode metadata.
    """
    output_dir = tempfile.mkdtemp()
    ydl_opts = make_ydl_opts(
        require_ffmpeg=True,
        format="bestaudio/best",
        outtmpl=os.path.join(output_dir, "%(title)s.%(ext)s"),
        restrictfilenames=True,
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
        }],
    )
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    s3 = s3_utils.upload_audio_and_cleanup(
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
        "s3_uri": s3["s3_uri"],
        "s3_download_url": s3["s3_download_url"],
        "s3_key": s3["s3_key"],
        "url": url,
    }


@mcp.tool()
def get_source_metadata(url: str) -> dict:
    """
    Extract metadata from any yt-dlp supported source without downloading.
    Used to validate a new source before adding it to the approved source list.
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
            "description": (info.get("description") or "")[:800],
            "webpage_url": info.get("webpage_url"),
            "extractor": info.get("extractor"),
            "is_live": info.get("is_live", False),
            "availability": info.get("availability"),
        }


@mcp.tool()
def get_playlist_items(playlist_url: str, max_items: int = 20) -> list:
    """
    Fetch all video/episode entries from a YouTube playlist or channel.
    Used to monitor video sources for new content during the daily ingestion refresh.
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
    Returns quality indicators to decide whether to ingest or flag for admin review.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
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


# ─────────────────────────────────────────────
# Workstream A: Source List & Schema support
# ─────────────────────────────────────────────

@mcp.tool()
def validate_source(url: str) -> dict:
    """
    Validate a candidate source URL before adding it to the approved source list.
    Checks reachability, source type, and transcript availability.
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
    Useful for determining the best ingestion format per source type.
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
    Scans transcript/article text for any of the provided keywords.
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
    Extract timestamped subtitle segments from a video/podcast.
    Every podcast/video bullet must include a timestamp per project spec.
    """
    import json

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
            return {"title": title, "url": url, "segments": [], "error": f"No subtitles for language: {language}"}

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

        segments = []
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
                segments.append({"timestamp": f"~{minutes}:{seconds:02d}", "start_ms": start_ms, "text": text})
                if len(segments) >= max_segments:
                    break
        except Exception:
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
    Compute a rubric relevance score (0–100) for content against a monitoring context.
    Top-N items selected when too many qualify; threshold loosens on low-news days.
    """
    score = 0
    rationale = []

    kw_score = min(len(keyword_matches) * 10, 40)
    score += kw_score
    if keyword_matches:
        rationale.append(f"Keyword matches ({', '.join(keyword_matches[:5])}): +{kw_score}")

    context_words = set(monitoring_context.lower().split())
    text_words = set(text.lower().split())
    overlap = context_words & text_words
    context_score = min(int((len(overlap) / max(len(context_words), 1)) * 40), 40)
    score += context_score
    if overlap:
        rationale.append(f"Context overlap ({len(overlap)} shared terms): +{context_score}")

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
        "note": "Consider lowering threshold to 35 on low-news days per project spec." if not passes else "",
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
    5–10 bullets, timestamps for audio/video, source attribution per project spec.
    """
    if len(bullets) > 10:
        bullets = bullets[:10]

    duration_str = f"{duration_seconds // 60} min" if duration_seconds else ""
    source_label = {
        "video": "Video", "podcast": "Podcast", "article": "Article",
        "newsletter": "Newsletter", "rss": "Article",
    }.get(source_type.lower(), source_type.capitalize())

    meta = f"[{uploader} · {source_label}" + (f" · {duration_str}" if duration_str else "") + "]"
    if upload_date and len(upload_date) == 8:
        meta += f" · {upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    bullet_lines = "\n".join(f"- {b}" for b in bullets)
    slack_block = (
        f"*{title}*\n{meta}\n_{monitoring_context}_\n\n"
        f"{bullet_lines}\n\n🔗 {source_url}\n📊 Relevance score: {relevance_score}/100"
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
    Assemble a full daily digest from scored and formatted items.
    Sorts by relevance score, caps at max_items, renders Slack message.
    """
    sorted_items = sorted(items, key=lambda x: x.get("relevance_score", 0), reverse=True)[:max_items]

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
    body = "\n\n---\n\n".join(i.get("slack_formatted_block", "") for i in sorted_items)
    footer = f"{'─' * 40}\n📋 Full source list & attribution log in Notion.\n💬 Feedback? DM this bot directly."
    full_message = f"{header}\n\n{body}\n\n{footer}"

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
def check_duplicate(item_url: str, item_title: str, post_log: list[dict]) -> dict:
    """
    Check whether a digest item has already been posted using the post log.
    Prevents duplicate items across daily digest issues per project spec.
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
    return {"is_duplicate": False, "recommendation": "include", "url": item_url, "title": item_title}


@mcp.tool()
def start_podcast_transcription(url: str, language_code: str = "en-US") -> dict:
    """
    Start transcribing a podcast episode with ZERO local disk usage.
    Downloads audio into memory, uploads to S3, and kicks off an AWS
    Transcribe job — then returns immediately with a job_name.
    Call get_transcription_result(job_name) after ~2-5 minutes to fetch
    the completed transcript. This two-step approach works within Lambda
    time limits. Supports direct MP3/M4A URLs and tracking redirect links
    (swap.fm, podtrac, simplecast, etc.) as well as YouTube and other
    yt-dlp supported sources.
    """
    import io
    import uuid

    import boto3

    audio_s3_key = None
    region = os.environ.get("AWS_REGION", "us-east-1")

    # Step 1: Resolve the final audio stream URL and episode metadata.
    # First try yt-dlp (handles YouTube, Spotify, Apple Podcasts, etc.).
    # If yt-dlp doesn't recognise the URL (e.g. tracking redirect links
    # like swap.fm/podtrac/simplecast), fall back to following HTTP
    # redirects directly to the raw audio file.
    info: dict = {}
    stream_url: str | None = None
    audio_ext = "mp3"

    try:
        ydl_opts = make_ydl_opts(format="bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio/best")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats") or []

        def _format_rank(f):
            if not f.get("url") or f.get("acodec") == "none":
                return -1
            ext = f.get("ext", "")
            audio_only = f.get("vcodec") in (None, "none", "")
            ext_score = {"m4a": 4, "mp4": 3, "mp3": 2}.get(ext, 1)
            return ext_score * 2 + (1 if audio_only else 0)

        ranked = sorted(formats, key=_format_rank)
        best = ranked[-1] if ranked else {}
        stream_url = best.get("url")
        audio_ext = best.get("ext", "mp4")
    except Exception:
        pass  # fall through to direct-URL path

    # Fallback: treat the URL as a direct/redirect audio link.
    # Follow redirects to find the real file URL and infer its format.
    if not stream_url:
        try:
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.url
                content_type = resp.headers.get("Content-Type", "")

            stream_url = final_url
            # Infer extension from the resolved URL or Content-Type
            _ct_map = {
                "audio/mpeg": "mp3", "audio/mp4": "mp4",
                "audio/x-m4a": "mp4", "audio/ogg": "ogg",
                "audio/flac": "flac", "audio/wav": "wav",
                "audio/webm": "webm",
            }
            audio_ext = _ct_map.get(content_type.split(";")[0].strip(), "mp3")
            if audio_ext == "mp3" and ".mp3" not in final_url and ".m4a" in final_url:
                audio_ext = "mp4"

            info = {"title": None, "uploader": None, "duration": None,
                    "upload_date": None, "description": None}
        except Exception as exc:
            return {
                "error": f"Could not resolve audio URL (yt-dlp and direct fallback both failed): {exc}",
                "url": url,
                "step": "resolve_url",
                "hint": "Provide the episode page URL (e.g. the podcast website or Apple Podcasts link), not a tracking/redirect MP3 link.",
            }

    if not stream_url:
        return {"error": "Could not resolve a direct audio stream URL", "url": url, "step": "select_format"}

    try:
        # Step 2: Download audio into memory — no disk writes
        req = urllib.request.Request(stream_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            audio_bytes = io.BytesIO(resp.read())
    except Exception as exc:
        return {
            "error": f"Failed to download audio stream: {exc}",
            "url": url,
            "stream_url": stream_url[:80] + "…",
            "audio_ext": audio_ext,
            "step": "download_stream",
        }

    try:
        # Step 3: Upload from memory directly to S3
        bucket = s3_utils.bucket_name()
        job_suffix = str(uuid.uuid4())[:8]
        safe_title = s3_utils._safe_key(info.get("title") or "podcast")
        audio_s3_key = f"podcast-audio/{safe_title}-{job_suffix}.{audio_ext}"

        s3_client = boto3.client("s3", region_name=region)
        audio_bytes.seek(0)
        s3_client.upload_fileobj(audio_bytes, bucket, audio_s3_key)
        s3_uri = f"s3://{bucket}/{audio_s3_key}"
    except Exception as exc:
        return {"error": f"S3 upload failed: {exc}", "url": url, "step": "s3_upload"}

    try:
        # Step 4: Start AWS Transcribe job and return immediately — no polling.
        # Lambda will time out if we poll here; use get_transcription_result instead.
        transcribe = boto3.client("transcribe", region_name=region)
        job_name = f"podcast-{job_suffix}"
        transcript_s3_key = f"transcripts/{job_name}.json"

        _media_format_map = {
            "mp4": "mp4", "m4a": "mp4", "mp3": "mp3",
            "webm": "webm", "ogg": "ogg", "flac": "flac",
            "wav": "wav", "amr": "amr",
        }
        media_format = _media_format_map.get(audio_ext, "mp4")

        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": s3_uri},
            MediaFormat=media_format,
            LanguageCode=language_code,
            OutputBucketName=bucket,
            OutputKey=transcript_s3_key,
        )
    except Exception as exc:
        return {
            "error": f"Failed to start AWS Transcribe job: {exc}",
            "url": url,
            "audio_ext": audio_ext,
            "s3_key": audio_s3_key,
            "step": "start_transcribe",
        }

    return {
        "status": "transcription_started",
        "job_name": job_name,
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "audio_format": media_format,
        "transcript_s3_key": transcript_s3_key,
        "url": url,
        "next_step": "Call get_transcription_result with the job_name in 2-5 minutes.",
    }


@mcp.tool()
def get_transcription_result(job_name: str) -> dict:
    """
    Fetch the result of a transcription job started by start_podcast_transcription.
    Returns the transcript text if the job is complete, or status if still in progress.
    Call this 2-5 minutes after start_podcast_transcription returns a job_name.
    If status is IN_PROGRESS, wait a bit longer and call again.
    """
    import json

    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        transcribe = boto3.client("transcribe", region_name=region)
        response = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        job = response["TranscriptionJob"]
        status = job["TranscriptionJobStatus"]
    except Exception as exc:
        return {"error": f"Could not retrieve job status: {exc}", "job_name": job_name}

    if status == "IN_PROGRESS" or status == "QUEUED":
        return {
            "status": status,
            "job_name": job_name,
            "message": "Transcription is still running. Call again in 1-2 minutes.",
        }

    if status == "FAILED":
        return {
            "status": "FAILED",
            "job_name": job_name,
            "error": job.get("FailureReason", "unknown"),
        }

    # COMPLETED — fetch the transcript JSON from S3
    try:
        bucket = s3_utils.bucket_name()
        transcript_s3_key = f"transcripts/{job_name}.json"
        s3_client = boto3.client("s3", region_name=region)
        result_obj = s3_client.get_object(Bucket=bucket, Key=transcript_s3_key)
        transcript_data = json.loads(result_obj["Body"].read())
        transcript_text = transcript_data["results"]["transcripts"][0]["transcript"]
    except Exception as exc:
        return {"error": f"Job completed but failed to fetch transcript: {exc}", "job_name": job_name}

    return {
        "status": "COMPLETED",
        "job_name": job_name,
        "transcript": transcript_text,
        "transcript_s3_key": transcript_s3_key,
    }


if __name__ == "__main__":
    mcp.run()
