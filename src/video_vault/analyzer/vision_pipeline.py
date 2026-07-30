from __future__ import annotations

from pathlib import Path
import json

from .cloud_provider import CloudProvider
from .frame_analysis import PROMPT_VERSION, cache_key, merge_frames_to_segments
from .mock_provider import MockProvider
from .local_provider import LocalProvider
from ..database import frames as db_frames, replace_segments, update_frame_analysis
from ..segment_state_migration import migrate_segment_state_for_video


class AnalysisCancelled(RuntimeError):
    pass


def provider_from_config(cfg: dict):
    name = cfg.get("ai", {}).get("provider", "mock")
    if name == "cloud":
        return CloudProvider(cfg)
    if name == "local":
        return LocalProvider(cfg)
    return MockProvider()


def analyze_frame_manifest(
    video: dict,
    cfg: dict,
    frame_manifest: list[dict],
    progress=None,
    should_cancel=None,
    duration_seconds: float | None = None,
) -> dict:
    """Analyze an explicit frame manifest without mutating published DB rows."""
    provider = provider_from_config(cfg)
    raw_dir = Path(cfg["library_root"]) / "05_index" / "raw_ai_outputs"
    analyzed = []
    total = len(frame_manifest)
    cache_hits = 0
    vision_calls = 0
    for index, frame in enumerate(frame_manifest, 1):
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")
        frame_path = Path(str(frame["frame_path"]))
        timestamp = float(frame.get("timestamp_seconds") or 0)
        key = cache_key(
            frame_path,
            provider.provider,
            provider.model,
            getattr(provider, "prompt_version", PROMPT_VERSION),
        )
        raw_path = raw_dir / f"{key}.json"
        if raw_path.exists():
            result = json.loads(raw_path.read_text(encoding="utf-8"))["parsed"]
            cache_hits += 1
        else:
            result, raw = provider.analyze_frame(frame_path, timestamp, video)
            vision_calls += 1
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps(
                    {"frame": str(frame_path), "parsed": result, "raw": raw},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        analyzed.append(
            {
                "frame_path": str(frame_path),
                "timestamp_seconds": timestamp,
                **result,
            }
        )
        if progress:
            progress(index, total, frame)
        if should_cancel and should_cancel():
            raise AnalysisCancelled("perception cancelled by user")
    perceived_segments = merge_frames_to_segments(
        analyzed,
        float(cfg["frame_interval_seconds"]),
        duration_seconds=(
            float(video.get("duration_seconds") or 0)
            if duration_seconds is None
            else duration_seconds
        ),
    )
    return {
        "provider": provider.provider,
        "model": provider.model,
        "frames": analyzed,
        "segments": perceived_segments,
        "cache_hits": cache_hits,
        "vision_calls": vision_calls,
    }


def analyze_video_frames(db: Path, video: dict, cfg: dict, progress=None) -> dict:
    """Legacy immediate-publish wrapper used by CLI and non-project flows."""
    frame_rows = [dict(frame) for frame in db_frames(db, int(video["id"]))]
    result = analyze_frame_manifest(video, cfg, frame_rows, progress)
    for frame_row, analyzed in zip(frame_rows, result["frames"], strict=True):
        update_frame_analysis(db, int(frame_row["id"]), analyzed)
    migration = replace_segments(db, int(video["id"]), result["segments"])
    project_migrations = migrate_segment_state_for_video(
        cfg,
        db,
        int(video["id"]),
        migration,
    )
    return {
        **result,
        "segment_identity_migration": migration,
        "project_segment_state_migrations": project_migrations,
    }
