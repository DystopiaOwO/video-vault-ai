import json
from pathlib import Path
import shutil
import subprocess

import pytest

from video_vault.audio_state import audio_state_for_api, default_audio_state, save_audio_state
from video_vault.bgm_pipeline import BgmPipelineError, build_bgm_mix_command
from video_vault.bgm import list_bgm
from video_vault.database import add_bgm_track, add_project_bgm
from video_vault.render_manifest import build_render_manifest
from video_vault.project import project_detail
from video_vault.timeline_assembler import build_timeline_command

from test_render_manifest import _project


def test_audio_state_is_compiled_into_manifest_and_bgm_is_single_selected_track(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    first = tmp_path / "one.mp3"
    second = tmp_path / "two.mp3"
    _make_audio(first)
    _make_audio(second)
    first_id = add_bgm_track(db, {"title": "第一首", "file_path": str(first), "source_url": "https://example.com/1", "license_name": "CC0", "attribution_text": "第一首"})
    second_id = add_bgm_track(db, {"title": "第二首", "file_path": str(second), "source_url": "https://example.com/2", "license_name": "CC0", "attribution_text": "第二首"})
    add_project_bgm(db, project_id, first_id)
    add_project_bgm(db, project_id, second_id)
    state = default_audio_state()
    state["bgm"].update({"bgm_id": second_id, "enabled": True, "volume_db": -14, "start_seconds": 3})
    state["segments"] = {project_detail(cfg, db, project_id)["segments"][0]["segment_id"]: {"role": "keep", "volume_db": -2, "fade_in_seconds": .2, "fade_out_seconds": .3}}
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    manifest = build_render_manifest(cfg, db, project_id)
    assert [track["track_id"] for track in manifest["bgm"]] == [second_id]
    segment = manifest["segments"][0]
    assert segment["audio"]["role"] == "keep"
    assert segment["audio"]["volume_db"] == -2
    assert manifest["bgm"][0]["start_seconds"] == 3
    assert manifest["settings"]["audio"]["normalization"]["target_lufs"] == -14


def test_disabled_audio_state_uses_legacy_segment_audio_and_no_selected_bgm(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    source = tmp_path / "disabled.mp3"
    _make_audio(source)
    track_id = add_bgm_track(db, {"title": "停用測試", "file_path": str(source), "source_url": "https://example.com", "license_name": "CC0", "attribution_text": "停用測試"})
    add_project_bgm(db, project_id, track_id)
    state = default_audio_state()
    state["enabled"] = False
    state["bgm"].update({"enabled": True, "bgm_id": track_id})
    state["original_audio"]["default_role"] = "keep"
    state["normalization"]["enabled"] = True
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    manifest = build_render_manifest(cfg, db, project_id)
    assert "audio" not in manifest["segments"][0]
    assert manifest["segments"][0]["audio_role"] == "lower_original"
    assert track_id not in [item["track_id"] for item in manifest["bgm"]]
    assert "normalization" not in manifest["settings"]["audio"]

    state["enabled"] = True
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    enabled = build_render_manifest(cfg, db, project_id)
    assert enabled["segments"][0]["audio"]["role"] == "keep"
    assert [item["track_id"] for item in enabled["bgm"]] == [track_id]
    assert enabled["settings"]["audio"]["normalization"]["enabled"] is True


def test_selected_bgm_id_and_bgm_only_without_track_block_manifest(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = default_audio_state()
    state["bgm"].update({"enabled": True, "bgm_id": 9999})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    with pytest.raises(BgmPipelineError, match="attached"):
        build_render_manifest(cfg, db, project_id)

    state = default_audio_state()
    segment_id = project_detail(cfg, db, project_id)["segments"][0]["segment_id"]
    state["segments"] = {segment_id: {"role": "bgm_only"}}
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    with pytest.raises(ValueError, match="bgm_only"):
        build_render_manifest(cfg, db, project_id)


def test_normalization_without_bgm_reencodes_and_disabled_uses_fast_path():
    profile = {"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}
    normalized = build_timeline_command(
        "ffmpeg", Path("timeline.ffconcat"), Path("normalized.mp4"),
        normalization={"enabled": True, "target_lufs": -14, "true_peak_db": -1}, profile=profile,
    )
    assert "loudnorm" in normalized[normalized.index("-filter_complex") + 1]
    assert "copy" not in normalized[normalized.index("-c:a") + 1]

    fast = build_timeline_command(
        "ffmpeg", Path("timeline.ffconcat"), Path("fast.mp4"),
        normalization={"enabled": False}, profile=profile,
    )
    assert fast[fast.index("-c:a") + 1] == "copy"
    assert "loudnorm" not in " ".join(fast)


def test_audio_api_state_does_not_expose_local_bgm_path(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    private = tmp_path / "private.mp3"
    _make_audio(private)
    track_id = add_bgm_track(db, {"title": "私人測試", "file_path": str(private), "source_url": "https://example.com/private", "license_name": "CC0", "attribution_text": "私人測試"})
    add_project_bgm(db, project_id, track_id)
    state = default_audio_state()
    state["bgm"].update({"bgm_id": track_id, "source_path": str(private)})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    api_state = audio_state_for_api(cfg, project_id, db)
    encoded = json.dumps(api_state, ensure_ascii=False)
    assert "source_path" not in encoded
    assert "private.mp3" not in encoded
    assert "file_path" not in json.dumps(list_bgm(db), ensure_ascii=False)
    assert "private.mp3" not in json.dumps(project_detail(cfg, db, project_id), ensure_ascii=False)


def test_bgm_command_supports_start_and_loudness_normalization():
    command = build_bgm_mix_command(
        "ffmpeg", Path("timeline.ffconcat"), Path("preview.mp4"),
        {"source_path": "C:/Music Files/旅途.mp3", "gain_db": -12, "start_seconds": 4.5, "loop": True, "fade_in_seconds": 1, "fade_out_seconds": 2},
        10, {"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        normalization={"enabled": True, "target_lufs": -14, "true_peak_db": -1},
    )
    assert "-ss" in command and "4.500000" in command
    graph = command[command.index("-filter_complex") + 1]
    assert "loudnorm=I=-14.000:TP=-1.000" in graph


def _make_audio(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
