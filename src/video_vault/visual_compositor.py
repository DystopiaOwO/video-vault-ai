"""Approved visual timeline resolution and FFmpeg composition helpers.

The module owns the small, deterministic visual contract shared by the formal
renderer and delivery packages.  It intentionally fails closed when a style,
font or runtime asset cannot be resolved.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


VISUAL_COMPOSITION_VERSION = "visual-composition-v1"
SUPPORTED_ANIMATIONS = {"static", "fade-in-out"}
# Chapter cards are intentionally dark, but a full-black card is interpreted
# as an unintended interior blank interval by the formal Delivery QA contract.
# Keep the visual treatment dark while retaining enough luma for the approved
# black-frame threshold to distinguish a title card from missing video.
CHAPTER_CARD_BACKGROUND = "0x20242a"
STYLE_CONTRACTS: dict[str, dict[str, Any]] = {
    "location-lower-left": {
        "version": 1,
        "font_size": 48,
        "x": "w*0.05",
        "y": "h*0.80",
        "box": True,
        "align": "left",
    },
    "title-center": {
        "version": 1,
        "font_size": 64,
        "x": "(w-text_w)/2",
        "y": "(h-text_h)/2",
        "box": True,
        "align": "center",
    },
    "lower-third": {
        "version": 1,
        "font_size": 38,
        "x": "w*0.05",
        "y": "h*0.78",
        "box": True,
        "align": "left",
    },
}


class VisualCompositionError(ValueError):
    """A visual contract cannot be rendered without guessing."""

    def __init__(self, code: str, message: str, *, action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action


def resolve_visual_timeline(
    visual_timeline: Mapping[str, Any] | None,
    segments: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    require_assets: bool = True,
) -> dict[str, Any]:
    """Resolve insertion points and final duration from one approved contract."""

    timeline = dict(visual_timeline or {})
    items = [dict(item) for item in timeline.get("items", []) if isinstance(item, Mapping)]
    base_segments = [dict(item) for item in sorted(segments, key=lambda item: int(item.get("order") or 0))]
    base_duration = round(sum(float(item.get("timeline_duration_seconds") or 0) for item in base_segments), 6)
    boundaries: list[tuple[float, str]] = [(0.0, "__start__")]
    cursor = 0.0
    segment_base_starts: dict[str, float] = {}
    group_base_starts: dict[str, float] = {}
    for segment in base_segments:
        segment_id = str(segment.get("segment_id") or "")
        segment_base_starts[segment_id] = cursor
        group_id = str(segment.get("group_id") or segment.get("group") or "")
        if group_id and group_id not in group_base_starts:
            group_base_starts[group_id] = cursor
        cursor += float(segment.get("timeline_duration_seconds") or 0)
        boundaries.append((round(cursor, 6), segment_id))

    # A storyboard can retain a chapter card for a group that is no longer
    # represented in the current included segment set.  It is stale metadata,
    # not a request to append content outside the approved media timeline.
    items = [
        item for item in items
        if not (
            str(item.get("type") or "") == "chapter_card"
            and str(item.get("group_id") or "")
            and str(item.get("group_id")) not in group_base_starts
        )
    ]

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        normalized.append(_normalize_visual_item(item, index, segment_base_starts, group_base_starts, base_duration, profile, require_assets=require_assets))
    insertions = [item for item in normalized if item["type"] in {"intro", "outro", "chapter_card"}]
    overlays = [item for item in normalized if item["type"] == "lower_third"]
    for item in insertions:
        start = float(item["start_seconds"])
        if not any(abs(start - boundary) <= 0.001 for boundary, _ in boundaries):
            raise VisualCompositionError("visual_split_required", f"visual item {item['stable_id']} 必須位於 segment 邊界，不能靜默切開片段", action="將 chapter card 移到片段開頭或結尾")
    insertions.sort(key=lambda item: (float(item["start_seconds"]), _visual_priority(item["type"]), str(item["stable_id"])))
    sequence: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    final_cursor = 0.0
    insertion_index = 0
    for segment in base_segments:
        base_start = segment_base_starts[str(segment.get("segment_id") or "")]
        while insertion_index < len(insertions) and float(insertions[insertion_index]["start_seconds"]) <= base_start + 0.001:
            item = dict(insertions[insertion_index])
            item["resolved_start_seconds"] = round(final_cursor, 6)
            sequence.append({"kind": "visual", "stable_id": item["stable_id"], "type": item["type"], "start_seconds": item["resolved_start_seconds"], "duration_seconds": item["duration_seconds"]})
            resolved_items.append(item)
            final_cursor += float(item["duration_seconds"])
            insertion_index += 1
        segment_id = str(segment.get("segment_id") or "")
        duration = float(segment.get("timeline_duration_seconds") or 0)
        sequence.append({"kind": "segment", "stable_id": segment_id, "type": "segment", "start_seconds": round(final_cursor, 6), "duration_seconds": round(duration, 6)})
        segment_base_starts[segment_id] = final_cursor
        final_cursor += duration
    while insertion_index < len(insertions):
        item = dict(insertions[insertion_index])
        if item["type"] != "outro" or float(item["start_seconds"]) < base_duration - 0.001:
            raise VisualCompositionError("visual_position_invalid", f"visual item {item['stable_id']} 位於無效時間軸位置", action="重新產生並核准 visual timeline")
        item["resolved_start_seconds"] = round(final_cursor, 6)
        sequence.append({"kind": "visual", "stable_id": item["stable_id"], "type": item["type"], "start_seconds": item["resolved_start_seconds"], "duration_seconds": item["duration_seconds"]})
        resolved_items.append(item)
        final_cursor += float(item["duration_seconds"])
        insertion_index += 1
    for item in overlays:
        if item.get("segment_id"):
            segment_start = next((entry["start_seconds"] for entry in sequence if entry["kind"] == "segment" and entry["stable_id"] == item["segment_id"]), None)
            if segment_start is None:
                raise VisualCompositionError("visual_segment_missing", f"lower third 找不到 segment：{item['segment_id']}", action="重新產生 visual timeline")
            offset = float(item.get("offset_seconds") or 0)
            if offset < -0.001:
                raise VisualCompositionError("visual_range_invalid", f"visual range invalid：lower third {item['stable_id']} offset 不可為負值", action="將 lower third 放在片段起點或之後")
            item["resolved_start_seconds"] = round(float(segment_start) + offset, 6)
        else:
            item["resolved_start_seconds"] = round(float(item.get("start_seconds") or 0), 6)
        if float(item["resolved_start_seconds"]) < -0.001 or float(item["resolved_start_seconds"]) + float(item["duration_seconds"]) > final_cursor + 0.001:
            raise VisualCompositionError("visual_range_invalid", f"lower third {item['stable_id']} 超出正式時間軸", action="縮短 lower third 時間範圍")
        resolved_items.append(item)
    resolved_items.sort(key=lambda item: (float(item["resolved_start_seconds"]), _visual_priority(item["type"]), str(item["stable_id"])))
    resolved = {
        **timeline,
        "contract_version": str(timeline.get("contract_version") or "visual-timeline-v1"),
        "resolution_version": VISUAL_COMPOSITION_VERSION,
        # Persist the resolved item contract in the approved manifest so a
        # later render can revalidate the same asset fingerprints.
        "items": resolved_items,
        "resolved_items": resolved_items,
        "sequence": sequence,
        "resolved_duration_seconds": round(final_cursor, 6),
    }
    resolved["resolution_hash"] = stable_visual_hash(resolved)
    return resolved


def visual_cache_key(item: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    payload = {
        "version": VISUAL_COMPOSITION_VERSION,
        "item": dict(item),
        "profile": {key: profile.get(key) for key in ("profile_id", "width", "height", "fps", "video_codec", "pixel_format", "audio_codec", "audio_sample_rate", "audio_channels")},
    }
    return _hash(payload)


def stable_visual_hash(timeline: Mapping[str, Any]) -> str:
    return _hash({key: value for key, value in timeline.items() if key not in {"resolution_hash"}})


def render_visual_cards(
    timeline: Mapping[str, Any],
    segment_paths: Mapping[str, Path],
    cache_root: Path,
    work_dir: Path,
    profile: Mapping[str, Any],
    ffmpeg_path: str,
    runner: Any | None = None,
) -> tuple[list[Path], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return concat sequence, report evidence and lower-third items."""

    cache_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    evidence: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    for entry in timeline.get("sequence") or []:
        if entry.get("kind") == "segment":
            path = segment_paths.get(str(entry.get("stable_id") or ""))
            if path is None or not path.is_file():
                raise VisualCompositionError("segment_missing", f"找不到 segment cache：{entry.get('stable_id')}", action="重新產生 segment cache")
            paths.append(path)
            continue
        item = next((dict(item) for item in timeline.get("resolved_items") or [] if str(item.get("stable_id")) == str(entry.get("stable_id"))), None)
        if item is None:
            raise VisualCompositionError("visual_item_missing", f"找不到 visual item：{entry.get('stable_id')}", action="重新核准 visual timeline")
        if item["type"] == "lower_third":
            overlays.append(item)
            continue
        key = visual_cache_key(item, profile)
        output = cache_root / f"{key}.mp4"
        metadata = cache_root / f"{key}.json"
        cache_hit = False
        if output.is_file() and metadata.is_file():
            try:
                cached = json.loads(metadata.read_text(encoding="utf-8"))
                cache_hit = (
                    cached.get("cache_key") == key
                    and cached.get("sha256") == _file_hash(output)
                    and int(cached.get("size") or 0) == output.stat().st_size
                    and output.stat().st_size > 0
                )
            except (OSError, ValueError):
                cache_hit = False
        if not cache_hit:
            partial = output.with_name(f".{output.stem}.partial.mp4")
            metadata_partial = metadata.with_name(f".{metadata.name}.partial")
            try:
                _render_card(ffmpeg_path, item, partial, work_dir, profile, runner=runner)
                digest = _file_hash(partial)
                metadata_partial.write_text(json.dumps({"cache_key": key, "stable_id": item["stable_id"], "sha256": digest, "size": partial.stat().st_size}, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                partial.replace(output)
                metadata_partial.replace(metadata)
            except BaseException:
                partial.unlink(missing_ok=True)
                metadata_partial.unlink(missing_ok=True)
                output.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise
        paths.append(output)
        evidence.append({
            "stable_id": item["stable_id"],
            "type": item["type"],
            "resolved_start_seconds": item["resolved_start_seconds"],
            "duration_seconds": item["duration_seconds"],
            "style_id": item["style_id"],
            "style_version": item["style_version"],
            "animation_id": item["animation_id"],
            "font_path": item["font_path"],
            "font_sha256": item["font_sha256"],
            "cache_key": key,
            "cache_hit": cache_hit,
            "asset_fingerprints": item.get("asset_fingerprints", []),
        })
    overlays = [dict(item) for item in timeline.get("resolved_items") or [] if item.get("type") == "lower_third"]
    evidence.extend({
        "stable_id": item["stable_id"],
        "type": item["type"],
        "resolved_start_seconds": item["resolved_start_seconds"],
        "duration_seconds": item["duration_seconds"],
        "style_id": item["style_id"],
        "style_version": item["style_version"],
        "animation_id": item["animation_id"],
        "font_path": item["font_path"],
        "font_sha256": item["font_sha256"],
        "cache_key": visual_cache_key(item, profile),
        "cache_hit": False,
        "cache_miss_reason": "overlay_applied",
        "asset_fingerprints": item.get("asset_fingerprints", []),
        "composition": "overlay",
    } for item in overlays)
    return paths, evidence, overlays


def apply_lower_thirds(
    ffmpeg_path: str,
    source: Path,
    output: Path,
    overlays: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    work_dir: Path,
    duration_seconds: float | None = None,
    runner: Any | None = None,
) -> None:
    if not overlays:
        shutil.copy2(source, output)
        return
    filters: list[str] = []
    textfiles: list[Path] = []
    for index, item in enumerate(overlays):
        textfile = _write_filter_text(str(item.get("text") or ""), f"vv-visual-text-{index:03d}-")
        textfiles.append(textfile)
        style = STYLE_CONTRACTS[str(item["style_id"])]
        enable = f"between(t\\,{float(item['resolved_start_seconds']):.6f}\\,{float(item['resolved_start_seconds']) + float(item['duration_seconds']):.6f})"
        filters.append(_drawtext(item, style, textfile, enable))
    # Keep an .mp4 suffix so FFmpeg selects the muxer; the leading dot still
    # makes the intermediate unpublishable to normal artifact discovery.
    temp = output.with_name(f".{output.stem}.tmp.mp4")
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
        "-vf", f"{_setparams_filter(profile)},{','.join(filters)}", "-map", "0:v:0", "-map", "0:a?", "-c:v", str(profile["video_codec"]),
        "-pix_fmt", str(profile["pixel_format"]), "-r", str(profile["fps"]), "-fps_mode", "cfr", "-c:a", str(profile["audio_codec"]),
        "-ar", str(profile["audio_sample_rate"]), "-ac", str(profile["audio_channels"]), "-af", "aresample=async=1:first_pts=0", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        "-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"),
        "-colorspace", str(profile.get("color_matrix") or "bt709"), "-color_range", str(profile.get("color_range") or "tv"),
    ]
    if duration_seconds is not None:
        command += ["-t", f"{float(duration_seconds):.6f}"]
    command += [str(temp)]
    try:
        result = _run_visual_command(command, runner, expected_duration_seconds=duration_seconds)
        if result.returncode != 0:
            raise VisualCompositionError("visual_render_failed", f"lower third render 失敗：{result.stderr[-1200:]}", action="檢查 visual style、font 與 FFmpeg")
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
        for textfile in textfiles:
            textfile.unlink(missing_ok=True)


def _normalize_visual_item(item: Mapping[str, Any], index: int, segment_starts: Mapping[str, float], group_starts: Mapping[str, float], base_duration: float, profile: Mapping[str, Any], *, require_assets: bool) -> dict[str, Any]:
    stable_id = str(item.get("stable_id") or "").strip()
    visual_type = str(item.get("type") or "").strip()
    if not stable_id or visual_type not in {"intro", "outro", "chapter_card", "lower_third"}:
        raise VisualCompositionError("visual_contract_invalid", f"visual item {index} 缺少有效 stable_id/type", action="重新產生 visual timeline")
    style_id = str(item.get("style_id") or ("lower-third" if visual_type == "lower_third" else "location-lower-left"))
    style = STYLE_CONTRACTS.get(style_id)
    if style is None or int(item.get("style_version") or style["version"]) != int(style["version"]):
        raise VisualCompositionError("style_missing", f"找不到 visual style：{style_id}", action="安裝或更新核准的 visual style")
    animation_id = str(item.get("animation_id") or "static")
    if animation_id not in SUPPORTED_ANIMATIONS:
        raise VisualCompositionError("animation_missing", f"找不到 visual animation：{animation_id}", action="安裝或更新核准的 animation runtime")
    duration = float(item.get("duration_seconds") or 0)
    if duration <= 0:
        raise VisualCompositionError("visual_duration_invalid", f"visual item {stable_id} duration 無效", action="重新產生 visual timeline")
    if not str(item.get("text") or "").strip():
        raise VisualCompositionError("visual_text_missing", f"visual item {stable_id} 缺少文字", action="補上 visual item 文字")
    # ``resolve_visual_runtime_assets`` pins the portable/system font in the
    # approved runtime asset list.  On Linux that font is often DejaVu, while
    # the compositor's platform fallback list intentionally excludes it for
    # CJK text.  Prefer the pinned font asset so approval and render use the
    # same cross-platform contract.
    font_path = _font_path(item.get("font_path"), str(item.get("text") or ""), item.get("runtime_assets"))
    asset_fingerprints: list[dict[str, Any]] = []
    for asset in item.get("runtime_assets") or []:
        path = Path(str(asset.get("path") if isinstance(asset, Mapping) else asset)).expanduser().resolve()
        if not path.is_file():
            raise VisualCompositionError("visual_asset_missing", f"visual asset 不存在：{path}", action="補齊 runtime asset 後重新核准")
        current = {"path": str(path), "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns, "sha256": _file_hash(path)}
        expected = next((entry for entry in item.get("asset_fingerprints") or [] if str(entry.get("path") or "") == str(path)), None)
        if expected is not None and any(str(expected.get(key)) != str(current.get(key)) for key in ("size", "mtime_ns", "sha256")):
            raise VisualCompositionError("visual_asset_changed", f"visual asset fingerprint 不一致：{path}", action="重新核准 visual runtime asset")
        asset_fingerprints.append(current)
    if require_assets and not font_path:
        raise VisualCompositionError("font_missing", f"visual item {stable_id} 找不到可用字型", action="安裝支援此文字的字型後重新核准")
    if visual_type == "intro":
        start = 0.0
    elif visual_type == "outro":
        start = base_duration
    elif visual_type == "chapter_card" and item.get("group_id") in group_starts:
        start = group_starts[str(item["group_id"])]
    elif item.get("segment_id") and visual_type == "lower_third":
        start = segment_starts.get(str(item["segment_id"]), float(item.get("start_seconds") or 0))
    else:
        start = float(item.get("start_seconds") or 0)
    current_font_sha = _file_hash(font_path) if font_path else ""
    if item.get("font_sha256") and str(item.get("font_sha256")) != current_font_sha:
        raise VisualCompositionError("font_changed", f"visual item {stable_id} 的字型 fingerprint 不一致", action="重新核准 visual 字型")
    result = {
        **dict(item),
        "stable_id": stable_id,
        "type": visual_type,
        "start_seconds": round(start, 6),
        "duration_seconds": round(duration, 6),
        "style_id": style_id,
        "style_version": int(style["version"]),
        "animation_id": animation_id,
        "font_path": str(font_path or item.get("font_path") or ""),
        "font_sha256": current_font_sha,
        "asset_fingerprints": asset_fingerprints,
        "profile_id": str(profile.get("profile_id") or ""),
    }
    return result


def _render_card(ffmpeg_path: str, item: Mapping[str, Any], output: Path, work_dir: Path, profile: Mapping[str, Any], *, runner: Any | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    textfile = _write_filter_text(str(item.get("text") or ""), "vv-visual-card-")
    style = STYLE_CONTRACTS[str(item["style_id"])]
    drawtext = _drawtext(item, style, textfile, "1")
    duration = float(item["duration_seconds"])
    video_filter = f"[0:v]{_setparams_filter(profile)},{drawtext}"
    if str(item.get("animation_id") or "static") == "fade-in-out":
        fade = min(0.2, duration / 2.0)
        video_filter += f",fade=t=in:st=0:d={fade:.6f},fade=t=out:st={max(0.0, duration - fade):.6f}:d={fade:.6f}"
    video_filter += "[v]"
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"color=c={CHAPTER_CARD_BACKGROUND}:s={int(profile['width'])}x{int(profile['height'])}:r={profile['fps']}:d={float(item['duration_seconds']):.6f}",
        "-f", "lavfi", "-i", f"anullsrc=r={int(profile['audio_sample_rate'])}:cl=stereo",
        "-filter_complex", video_filter, "-map", "[v]", "-map", "1:a:0", "-t", f"{duration:.6f}",
        "-c:v", str(profile["video_codec"]), "-pix_fmt", str(profile["pixel_format"]), "-c:a", str(profile["audio_codec"]),
        "-ar", str(profile["audio_sample_rate"]), "-ac", str(profile["audio_channels"]), "-shortest", "-movflags", "+faststart",
        "-color_primaries", str(profile.get("color_primaries") or "bt709"), "-color_trc", str(profile.get("color_transfer") or "bt709"),
        "-colorspace", str(profile.get("color_matrix") or "bt709"), "-color_range", str(profile.get("color_range") or "tv"), str(output),
    ]
    try:
        result = _run_visual_command(command, runner, expected_duration_seconds=duration)
        if result.returncode != 0:
            raise VisualCompositionError("visual_render_failed", f"visual card render 失敗：{result.stderr[-1200:]}", action="檢查 visual style、font 與 FFmpeg")
    finally:
        textfile.unlink(missing_ok=True)


def _drawtext(item: Mapping[str, Any], style: Mapping[str, Any], textfile: Path, enable: str) -> str:
    options = [
        f"fontfile='{escape_filter_value(_filter_graph_path(item['font_path']))}'",
        f"textfile='{escape_filter_value(_filter_graph_path(textfile))}'",
        f"fontsize={int(style['font_size'])}",
        "fontcolor=white",
        f"x={style['x']}",
        f"y={style['y']}",
        f"box={1 if style.get('box') else 0}",
        "boxcolor=black@0.60",
        "boxborderw=18",
        f"enable='{enable}'",
    ]
    if str(item.get("animation_id") or "static") == "fade-in-out" and str(item.get("type") or "") == "lower_third":
        start = float(item.get("resolved_start_seconds") or 0)
        end = start + float(item.get("duration_seconds") or 0)
        fade = min(0.2, max(0.001, (end - start) / 2.0))
        alpha = (
            f"if(lt(t\\,{start:.6f})\\,0\\,if(lt(t\\,{start + fade:.6f})\\,(t-{start:.6f})/{fade:.6f}\\,"
            f"if(lt(t\\,{end - fade:.6f})\\,1\\,if(lt(t\\,{end:.6f})\\,({end:.6f}-t)/{fade:.6f}\\,0))))"
        )
        options.append(f"alpha='{alpha}'")
    return "drawtext=" + ":".join(options)


def _run_visual_command(command: list[str], runner: Any | None, *, expected_duration_seconds: float | None) -> Any:
    if runner is None:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if hasattr(runner, "run"):
        try:
            return runner.run(command, capture_output=True, text=True, check=False, expected_duration_seconds=expected_duration_seconds)
        except TypeError:
            return runner.run(command)
    try:
        return runner(command, capture_output=True, text=True, check=False, expected_duration_seconds=expected_duration_seconds)
    except TypeError:
        return runner(command)


def _setparams_filter(profile: Mapping[str, Any]) -> str:
    return (
        "setparams="
        f"color_primaries={profile.get('color_primaries') or 'bt709'}:"
        f"color_trc={profile.get('color_transfer') or 'bt709'}:"
        f"colorspace={profile.get('color_matrix') or 'bt709'}:"
        f"range={profile.get('color_range') or 'tv'}"
    )


def _write_filter_text(text: str, prefix: str) -> Path:
    """Keep filtergraph filenames ASCII and independent of project paths."""

    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".txt")
    path = Path(name)
    stream = None
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8", newline="")
        fd = -1
        stream.write(text)
        stream.close()
        stream = None
    except BaseException:
        if stream is not None:
            stream.close()
        elif fd != -1:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise
    return path


def _filter_graph_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if os.name != "nt":
        return path
    # FFmpeg filter options are parsed before Windows wide-path conversion.
    # Prefer an 8.3 path when the filesystem exposes one, while the manifest
    # and report continue to retain the original resolved Unicode path.
    try:
        import ctypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short.restype = ctypes.c_uint32
        size = 260
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            length = int(get_short(str(path), buffer, size))
            if length == 0:
                break
            if length < size:
                return Path(buffer.value)
            size *= 2
    except (AttributeError, OSError, TypeError):
        pass
    return path


def _font_path(explicit: Any, text: str, runtime_assets: Any = None) -> Path | None:
    if str(explicit or "").strip():
        path = Path(str(explicit)).expanduser().resolve()
        if not path.is_file():
            raise VisualCompositionError("font_missing", f"指定字型不存在：{path}", action="補齊指定字型後重新核准")
        return path
    for asset in runtime_assets or []:
        if not isinstance(asset, Mapping):
            continue
        if str(asset.get("kind") or "").lower() != "font":
            continue
        path = Path(str(asset.get("path") or "")).expanduser().resolve()
        if path.is_file() and not path.is_symlink():
            return path
        raise VisualCompositionError("font_missing", f"指定字型不存在：{path}", action="補齊指定字型後重新核准")
    has_cjk = bool(re.search(r"[\u2e80-\u9fff\uf900-\ufaff]", text))
    windows = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name for name in ("msjh.ttc", "mingliu.ttc", "arial.ttf")]
    portable = [Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    candidates = windows + portable if has_cjk else windows + portable
    for path in candidates:
        if path.is_file():
            if has_cjk and path.name.lower() == "dejavusans.ttf":
                continue
            return path.resolve()
    return None


def escape_filter_value(value: str | Path) -> str:
    text = str(Path(value).expanduser().resolve()).replace("\\", "/")
    for char in ("\\", ":", "'", ",", ";", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def _visual_priority(value: str) -> int:
    return {"intro": 0, "chapter_card": 1, "lower_third": 2, "outro": 3}.get(value, 9)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "STYLE_CONTRACTS",
    "SUPPORTED_ANIMATIONS",
    "VISUAL_COMPOSITION_VERSION",
    "VisualCompositionError",
    "apply_lower_thirds",
    "escape_filter_value",
    "render_visual_cards",
    "resolve_visual_timeline",
    "stable_visual_hash",
    "visual_cache_key",
]
