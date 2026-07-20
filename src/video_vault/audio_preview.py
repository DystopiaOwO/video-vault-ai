"""Project audio previews built from the current, not-yet-approved settings."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .audio_state import default_audio_state, editable_audio_patch, has_audio_state, load_audio_state, normalize_audio_state
from .bgm_pipeline import BgmPipelineError, bgm_fingerprint, build_bgm_mix_command, validate_bgm_track
from .project import project_dir
from .render_manifest import build_render_manifest, manifest_hash
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
    timeline_start_seconds: float = 0.0,
    duration_seconds: float = 12.0,
    audio_patch: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if not 3.0 <= float(duration_seconds) <= 30.0:
        raise AudioPreviewError("預覽長度必須介於 3 到 30 秒")
    if float(timeline_start_seconds) < 0:
        raise AudioPreviewError("預覽起始時間不可為負數")
    override = _transient_audio_state(cfg, project_id, audio_patch) if audio_patch is not None else None
    try:
        manifest = build_render_manifest(cfg, db, project_id, audio_state_override=override)
    except Exception as exc:
        raise AudioPreviewError(f"無法建立音訊預覽 Manifest：{exc}") from exc
    if segment_id:
        selected = [item for item in manifest.get("segments", []) if str(item.get("segment_id")) == str(segment_id)]
        segments = _slice_timeline(selected, 0.0, float(duration_seconds))
        preview_start = 0.0
    else:
        preview_start = float(timeline_start_seconds)
        segments = _slice_timeline(list(manifest.get("segments") or []), preview_start, float(duration_seconds))
    if not segments:
        raise AudioPreviewError("目前沒有可預覽的片段")
    manifest = _preview_manifest(manifest, segments)
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
        "timeline_start_seconds": preview_start,
        "duration_seconds": float(duration_seconds),
        "audio": (manifest.get("settings") or {}).get("audio", {}),
        "bgm": bgm_fp,
        "source_fingerprints": [_source_fingerprint(item.get("source_file")) for item in segments],
    }
    cache_key = hashlib.sha256(json.dumps(cache_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    folder = project_dir(cfg, project_id) / "output" / "audio_previews"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{cache_key}.mp4"
    metadata = output.with_suffix(".json")
    if not force and output.is_file() and output.stat().st_size > 0 and metadata.is_file():
        return {"ok": True, "cache_hit": True, "file": output.name, "duration_seconds": sum(float(item.get("timeline_duration_seconds") or 0) for item in segments), "timeline_start_seconds": preview_start}
    partial = output.with_suffix(".partial.mp4")
    concat = folder / f".{cache_key}.ffconcat"
    metadata_temp = metadata.with_name(f".{metadata.name}.tmp")
    output_published = False
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
            command = build_timeline_command(
                str(cfg.get("ffmpeg_path") or "ffmpeg"),
                concat,
                partial,
                duration_seconds=expected,
                normalization=((manifest.get("settings") or {}).get("audio") or {}).get("normalization"),
                profile=manifest["profile"],
            )
        result = run_command(command)
        if int(getattr(result, "returncode", 0) or 0) != 0:
            raise AudioPreviewError(str(getattr(result, "stderr", "") or "音訊預覽 FFmpeg 失敗"))
        metadata_temp.write_text(json.dumps({"cache_key": cache_key, "duration_seconds": expected, "segments": [item.get("segment_id") for item in segments]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        partial.replace(output)
        output_published = True
        metadata_temp.replace(metadata)
        return {"ok": True, "cache_hit": False, "file": output.name, "duration_seconds": expected, "timeline_start_seconds": preview_start}
    except AudioPreviewError:
        partial.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
        if output_published:
            output.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
        if output_published:
            output.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
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


def _transient_audio_state(cfg: dict, project_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    current = load_audio_state(cfg, project_id) if has_audio_state(cfg, project_id) else default_audio_state()
    return normalize_audio_state(_deep_merge(current, editable_audio_patch(patch)))


def _preview_manifest(manifest: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(manifest)
    result["segments"] = segments
    result["expected_duration_seconds"] = round(sum(float(item["timeline_duration_seconds"]) for item in segments), 6)
    result["manifest_hash"] = manifest_hash(result)
    return result


def _slice_timeline(segments: list[dict[str, Any]], start: float, duration: float) -> list[dict[str, Any]]:
    end = start + duration
    result: list[dict[str, Any]] = []
    cursor = 0.0
    for source in segments:
        segment_duration = float(source.get("timeline_duration_seconds") or 0.0)
        overlap_start = max(start, cursor)
        overlap_end = min(end, cursor + segment_duration)
        if overlap_end > overlap_start + 0.000001:
            item = dict(source)
            speed = float(item["speed"])
            source_in = float(item["source_in_seconds"]) + (overlap_start - cursor) * speed
            source_out = source_in + (overlap_end - overlap_start) * speed
            item["source_in_seconds"] = round(source_in, 6)
            item["source_out_seconds"] = round(source_out, 6)
            item["source_duration_seconds"] = round(source_out - source_in, 6)
            item["timeline_duration_seconds"] = round(overlap_end - overlap_start, 6)
            item["order"] = len(result) + 1
            result.append(item)
        cursor += segment_duration
        if cursor >= end:
            break
    return result


def _source_fingerprint(value: Any) -> dict[str, Any]:
    path = Path(str(value or "")).expanduser().resolve()
    stat = path.stat() if path.is_file() else None
    return {"path": str(path), "size": stat.st_size if stat else None, "mtime_ns": stat.st_mtime_ns if stat else None}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


__all__ = ["AudioPreviewError", "audio_preview_file_path", "render_project_audio_preview"]
