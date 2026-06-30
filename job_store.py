from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

_STORE_PATH = Path(os.environ.get("YTDLP_JOB_STORE", "/tmp/yt-dlp-jobs.json"))
_lock = threading.Lock()

# Job types claimed by each agent
INGESTION_JOBS = {"source_metadata", "validate_source", "list_formats", "playlist_items"}
TRANSCRIPT_JOBS = {"video_transcript", "transcript_quality"}
AUDIO_JOBS = {"podcast_audio"}


def _load() -> dict:
    if _STORE_PATH.exists():
        try:
            return json.loads(_STORE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    _STORE_PATH.write_text(json.dumps(data, indent=2))


def enqueue(job_type: str, payload: dict) -> str:
    job_id = str(uuid.uuid4())
    with _lock:
        data = _load()
        data[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "payload": payload,
            "result": None,
            "error": None,
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _save(data)
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        return _load().get(job_id)


def claim_next(job_types: set[str]) -> dict | None:
    """Atomically claim the next queued job matching any of the given types."""
    with _lock:
        data = _load()
        for job in sorted(data.values(), key=lambda j: j["created_at"]):
            if job["type"] in job_types and job["status"] == "queued":
                job["status"] = "running"
                job["attempts"] += 1
                job["updated_at"] = time.time()
                _save(data)
                return job
    return None


def requeue_failed(job_id: str, max_attempts: int = 4) -> bool:
    """Put a running job back to queued if under max_attempts, else mark failed."""
    with _lock:
        data = _load()
        job = data.get(job_id)
        if not job:
            return False
        if job["attempts"] < max_attempts:
            job["status"] = "queued"
            job["updated_at"] = time.time()
        else:
            job["status"] = "failed"
            job["updated_at"] = time.time()
        _save(data)
        return job["status"] == "queued"


def complete_job(job_id: str, result: dict) -> None:
    with _lock:
        data = _load()
        if job_id in data:
            data[job_id]["status"] = "done"
            data[job_id]["result"] = result
            data[job_id]["updated_at"] = time.time()
            _save(data)


def fail_job(job_id: str, error: str) -> None:
    with _lock:
        data = _load()
        if job_id in data:
            data[job_id]["status"] = "failed"
            data[job_id]["error"] = error
            data[job_id]["updated_at"] = time.time()
            _save(data)


def list_jobs(status: str | None = None) -> list[dict]:
    with _lock:
        jobs = list(_load().values())
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


def purge_done(older_than_seconds: float = 3600) -> int:
    """Remove completed/failed jobs older than the given age. Returns count purged."""
    cutoff = time.time() - older_than_seconds
    with _lock:
        data = _load()
        before = len(data)
        data = {
            jid: j for jid, j in data.items()
            if not (j["status"] in ("done", "failed") and j["updated_at"] < cutoff)
        }
        _save(data)
    return before - len(data)
