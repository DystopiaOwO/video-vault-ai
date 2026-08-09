"""Run the local WebUI against an isolated VID-7 acceptance root."""

from __future__ import annotations

import argparse
from pathlib import Path

from video_vault.ui import run_ui


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    cfg = {
        "library_root": str(root / "library"),
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "delivery_qa": {"contract_version": "delivery-qa-v1", "timeout_seconds": 600, "threshold_overrides": {}, "profiles": {}},
    }
    run_ui(cfg, "127.0.0.1", int(args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
