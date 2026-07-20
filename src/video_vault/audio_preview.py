"""Project audio previews built from the current, not-yet-approved settings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .bgm_pipeline import BgmPipelineError, bgm_fingerprint, build_bgm_mix_command, validate_bgm_track
from .project import project_dir
from .render_manifest import build_render_manifest
from .segment_renderer import render_segment
from .timeline_assembler import build_concat_file, build_timeline_command, run_command


class AudioPreviewError(RuntimeError):
    pass


def render_project_audio_preview(
    cfg: dict,
    db: Path,
    project_id: int,
    *,
    segment_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    try:
        manifest = build_render_manifest(cfg, db, project_id)
    except Exception as exc:
        raise AudioPreviewError(f"無法建立音訊預覽 Manifest：{exc}") from exc
    segments = list(manifest.get("segments") or [])
    if segment_id:
        segments = [item for item in segments if str(item.get("segment_id")) == str(segment_id)]
    if not segments:
        raise AudioPreviewError("目前沒有可預覽的片段")
    track = next(iter(manifest.get("bgm") or []), None)
    bgm_fp = None
    if track:
        try:
            validate_bgm_track(track, str(cfg.get("ffprobe_path") or "ffprobe"))
            bgm_fp = bgm_fingerprint(track)
        except BgmPipelineError as exc:
            raise AudioPreviewError(str(exc)) from exc
    cache_payload = {
        "project_id": project_id,
        "manifest_hash": manifest.get("manifest_hash"),
        "segment_ids": [item.get("segment_id") for item in segments],
        "audio": (manifest.get("settings") or {}).get("audio", {}),
        "bgm": bgm_fp,
    }
    cache_key = hashlib.sha256(json.dumps(cache_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    folder = project_dir(cfg, project_id) / "output" / "audio_previews"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{cache_key}.mp4"
    metadata = output.with_suffix(".json")
    if not force and output.is_file() and output.stat().st_size > 0 and metadata.is_file():
        return {"ok": True, "cache_hit": True, "file": output.name, "duration_seconds": sum(float(item.get("timeline_duration_seconds") or 0) for item in segments)}
    partial = output.with_suffix(".partial.mp4")
    concat = folder / f".{cache_key}.ffconcat"
    try:
        results = [render_segment(cfg, manifest, item, cache_root=project_dir(cfg, project_id) / "cache" / "segments") for item in segments]
        build_concat_file([item.output_path for item in results], concat)
        expected = sum(float(item["timeline_duration_seconds"]) for item in segments)
        if track:
            command = build_bgm_mix_command(
                str(cfg.get("ffmpeg_path") or "ffmpeg"), concat, partial, track, expected, manifest["profile"],
                normalization=((manifest.get("settings") or {}).get("audio") or {}).get("normalization"),
            )
        else:
            command = build_timeline_command(str(cfg.get("ffmpeg_path") or "ffmpeg"), concat, partial, duration_seconds=expected)
        result = run_command(command)
        if int(getattr(result, "returncode", 0) or 0) != 0:
            raise AudioPreviewError(str(getattr(result, "stderr", "") or "音訊預覽 FFmpeg 失敗"))
        partial.replace(output)
        metadata.write_text(json.dumps({"cache_key": cache_key, "duration_seconds": expected, "segments": [item.get("segment_id") for item in segments]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "cache_hit": False, "file": output.name, "duration_seconds": expected}
    except AudioPreviewError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise AudioPreviewError(f"音訊預覽失敗：{exc}") from exc
    finally:
        concat.unlink(missing_ok=True)


def audio_preview_file_path(cfg: dict, project_id: int, filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not name.endswith(".mp4"):
        raise ValueError("invalid audio preview filename")
    path = (project_dir(cfg, project_id) / "output" / "audio_previews" / name).resolve()
    root = (project_dir(cfg, project_id) / "output" / "audio_previews").resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(path)
    return path


__all__ = ["AudioPreviewError", "audio_preview_file_path", "render_project_audio_preview"]
