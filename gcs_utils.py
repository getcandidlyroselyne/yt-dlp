"""
Google Cloud Storage helpers shared by server.py and agents/audio_agent.py.

Required environment variables:
  GCS_BUCKET                  - GCS bucket name to upload audio files into
  GOOGLE_APPLICATION_CREDENTIALS - path to a service-account JSON key file
                                  (optional if running with Workload Identity /
                                   Application Default Credentials)

Required IAM permissions on the bucket (bind to the service account):
  roles/storage.objectCreator   — upload objects
  roles/storage.objectViewer    — read objects / generate signed URLs
  (or the combined roles/storage.objectAdmin)

  If you want to generate V4 signed URLs the service account also needs:
  roles/iam.serviceAccountTokenCreator  (on itself, or use impersonation)
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import tempfile
from pathlib import Path

from google.cloud import storage

_GCS_CLIENT: storage.Client | None = None


def _client() -> storage.Client:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        _GCS_CLIENT = storage.Client()
    return _GCS_CLIENT


def bucket_name() -> str:
    name = os.environ.get("GCS_BUCKET", "").strip()
    if not name:
        raise RuntimeError(
            "GCS_BUCKET environment variable is not set. "
            "Set it to the name of your Cloud Storage bucket."
        )
    return name


def _safe_name(text: str) -> str:
    """Strip characters that are invalid in GCS object names."""
    text = text.strip()
    text = re.sub(r"[^\w\-. ]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:120] or "audio"


def upload_file(local_path: str | Path, gcs_object: str) -> str:
    """
    Upload *local_path* to GCS as *gcs_object* and return the ``gs://`` URI.
    The local file is NOT deleted by this function — callers are responsible
    for cleaning up their temp directories.
    """
    bucket = _client().bucket(bucket_name())
    blob = bucket.blob(gcs_object)
    blob.upload_from_filename(str(local_path))
    return f"gs://{bucket_name()}/{gcs_object}"


def signed_download_url(gcs_object: str, expiration_hours: int = 24) -> str | None:
    """
    Return a V4 signed HTTPS URL valid for *expiration_hours*.
    Returns None if the service account does not support signing
    (e.g. when using plain ADC without a key file); callers should
    fall back to the ``gs://`` URI in that case.
    """
    try:
        bucket = _client().bucket(bucket_name())
        blob = bucket.blob(gcs_object)
        url = blob.generate_signed_url(
            expiration=datetime.timedelta(hours=expiration_hours),
            method="GET",
            version="v4",
        )
        return url
    except Exception:
        return None


def upload_audio_and_cleanup(output_dir: str | Path, title: str, audio_format: str) -> dict:
    """
    Find the converted audio file inside *output_dir*, upload it to GCS,
    delete the entire temp directory, and return GCS location metadata.

    Returns a dict with:
      gcs_uri          — ``gs://bucket/path``
      gcs_download_url — signed HTTPS URL (None if signing unavailable)
      gcs_object       — bare object path inside the bucket
    """
    output_dir = Path(output_dir)
    matches = list(output_dir.glob(f"*.{audio_format}"))
    if not matches:
        matches = list(output_dir.glob("*"))
    if not matches:
        raise FileNotFoundError(f"No output file found in {output_dir}")

    audio_file = matches[0]
    safe_title = _safe_name(title or "audio")
    gcs_object = f"podcast-audio/{safe_title}{audio_file.suffix}"

    try:
        gcs_uri = upload_file(audio_file, gcs_object)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)

    download_url = signed_download_url(gcs_object)
    return {
        "gcs_uri": gcs_uri,
        "gcs_download_url": download_url,
        "gcs_object": gcs_object,
    }
