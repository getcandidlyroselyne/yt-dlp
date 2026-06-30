"""
Fleet launcher — starts all agents as daemon threads in one process.

Usage:
    python -m agents.run_fleet

Each agent polls the shared job store independently. If one agent crashes
its thread is restarted automatically. The process stays alive until killed.

Environment variables:
    YTDLP_JOB_STORE   — path to the JSON job store file (default: /tmp/yt-dlp-jobs.json)
    YTDLP_FFMPEG_DIR  — path to cached ffmpeg binaries  (default: /tmp/yt-dlp-ffmpeg)
    FFMPEG_LOCATION   — override ffmpeg directory directly
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import audio_agent, ingestion_agent, transcript_agent

_AGENTS = [
    ("ingestion", ingestion_agent.loop),
    ("transcript", transcript_agent.loop),
    ("audio", audio_agent.loop),
]

_RESTART_DELAY = 5  # seconds to wait before restarting a crashed thread


def _watched_loop(name: str, target_fn) -> None:
    """Run target_fn in a loop; restart on unexpected exit."""
    while True:
        try:
            target_fn()
        except Exception as exc:
            print(f"[fleet] {name} agent crashed — restarting in {_RESTART_DELAY}s: {exc}")
            time.sleep(_RESTART_DELAY)


def main() -> None:
    print("[fleet] starting yt-dlp agent fleet")

    threads: list[threading.Thread] = []
    for name, fn in _AGENTS:
        t = threading.Thread(
            target=_watched_loop,
            args=(name, fn),
            name=f"agent-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"[fleet] started {name} agent (tid={t.ident})")

    print("[fleet] all agents running — press Ctrl+C to stop")
    try:
        while True:
            alive = [t.name for t in threads if t.is_alive()]
            dead = [t.name for t in threads if not t.is_alive()]
            if dead:
                print(f"[fleet] WARNING dead threads: {dead}")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[fleet] shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
