from __future__ import annotations

from pathlib import Path
import json

from .cloud_provider import CloudProvider
from .frame_analysis import PROMPT_VERSION, cache_key, merge_frames_to_segments
from .mock_provider import MockProvider
from .local_provider import LocalProvider
from ..database import frames as db_frames, replace_segments, update_frame_analysis
from ..segment_state_migration import migrate_segment_state_for_video


def provider_from_config(cfg: dict):
    name = cfg.get("ai", {}).get("provider", "mock")
    if name == "cloud":
        return CloudProvider(cfg)
    if name == "local":
        return LocalProvider(cfg)
    return MockProvider()


def analyze_video_frames(db: Path, video: dict, cfg: dict, progress=None) -> dict:
    provider = provider_from_config(cfg)
    raw_dir = Path(cfg["library_root"]) / "05_index" / "raw_ai_outputs"
    analyzed = []
    frame_rows = db_frames(db, int(video["id"]))
    for index, frame in enumerate(frame_rows, 1):
        frame_path = Path(frame["frame_path"])
        key = cache_key(frame_path, provider.provider, provider.model, getattr(provider, "prompt_version", PROMPT_VERSION))
        raw_path = raw_dir / f"{key}.json"
        if raw_path.exists():
            result = json.loads(raw_path.read_text(encoding="utf-8"))["parsed"]
        else:
            result, raw = provider.analyze_frame(frame_path, float(frame["timestamp_seconds"]), video)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"frame": str(frame_path), "parsed": result, "raw": raw}, ensure_ascii=False, indent=2), encoding="utf-8")
        update_frame_analysis(db, int(frame["id"]), result)
        analyzed.append({"timestamp_seconds": float(frame["timestamp_seconds"]), **result})
        if progress:
            progress(index, len(frame_rows), frame)
    perceived_segments = merge_frames_to_segments(analyzed, float(cfg["frame_interval_seconds"]))
    migration = replace_segments(db, int(video["id"]), perceived_segments)
    project_migrations = migrate_segment_state_for_video(
        cfg,
        db,
        int(video["id"]),
        migration,
    )
    return {
        "frames": analyzed,
        "segments": perceived_segments,
        "segment_identity_migration": migration,
        "project_segment_state_migrations": project_migrations,
    }
