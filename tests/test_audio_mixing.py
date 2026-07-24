import json
from pathlib import Path
import shutil
import subprocess

import pytest

from video_vault.audio_state import audio_state_for_api, default_audio_state, save_audio_state, update_audio_state
from video_vault.bgm_pipeline import BgmPipelineError, build_bgm_mix_command
from video_vault.bgm import list_bgm
from video_vault.database import add_bgm_track, add_project_bgm, connect, project_bgm_tracks
from video_vault.render_manifest import build_render_manifest
from video_vault.project import project_detail
from video_vault.timeline_assembler import build_timeline_command

from test_render_manifest import _project

pytestmark = pytest.mark.media_e2e


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
    assert track_id in [item["track_id"] for item in manifest["bgm"]]
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


def test_new_audio_state_resolves_unattached_global_bgm_without_legacy_relation(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    bgm = tmp_path / "global-only.mp3"
    _make_audio(bgm)
    track_id = add_bgm_track(db, {"title": "全域未掛載", "file_path": str(bgm), "source_url": "https://example.com/global", "license_name": "CC0", "attribution_text": "全域未掛載"})
    state = default_audio_state()
    state["bgm"].update({"bgm_id": track_id, "enabled": True})
    update_audio_state(cfg, db, project_id, state)

    manifest = build_render_manifest(cfg, db, project_id)
    assert [item["track_id"] for item in manifest["bgm"]] == [track_id]
    assert project_bgm_tracks(db, project_id) == []
    api_state = audio_state_for_api(cfg, project_id, db)
    assert api_state["bgm"]["track"]["id"] == track_id
    assert "file_path" not in json.dumps(api_state, ensure_ascii=False)


def test_enabled_audio_state_without_bgm_does_not_fallback_to_legacy_relation(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    legacy = tmp_path / "legacy-active-state.mp3"
    _make_audio(legacy)
    legacy_id = add_bgm_track(db, {"title": "Legacy", "file_path": str(legacy), "source_url": "https://example.com/legacy-active", "license_name": "CC0", "attribution_text": "Legacy"})
    add_project_bgm(db, project_id, legacy_id)

    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    assert build_render_manifest(cfg, db, project_id)["bgm"] == []


def test_changing_selected_bgm_does_not_accumulate_legacy_rows(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    _make_audio(first)
    _make_audio(second)
    first_id = add_bgm_track(db, {"title": "第一首", "file_path": str(first), "source_url": "https://example.com/1", "license_name": "CC0", "attribution_text": "第一首"})
    second_id = add_bgm_track(db, {"title": "第二首", "file_path": str(second), "source_url": "https://example.com/2", "license_name": "CC0", "attribution_text": "第二首"})
    update_audio_state(cfg, db, project_id, {"bgm": {"bgm_id": first_id, "enabled": True}})
    update_audio_state(cfg, db, project_id, {"bgm": {"bgm_id": second_id, "enabled": True}})
    assert project_bgm_tracks(db, project_id) == []
    assert build_render_manifest(cfg, db, project_id)["bgm"][0]["track_id"] == second_id


def test_disabling_audio_state_restores_only_original_legacy_bgm(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    legacy = tmp_path / "legacy.mp3"
    selected = tmp_path / "selected.mp3"
    _make_audio(legacy)
    _make_audio(selected)
    legacy_id = add_bgm_track(db, {"title": "Legacy", "file_path": str(legacy), "source_url": "https://example.com/legacy", "license_name": "CC0", "attribution_text": "Legacy"})
    selected_id = add_bgm_track(db, {"title": "Selected", "file_path": str(selected), "source_url": "https://example.com/selected", "license_name": "CC0", "attribution_text": "Selected"})
    add_project_bgm(db, project_id, legacy_id)
    update_audio_state(cfg, db, project_id, {"bgm": {"bgm_id": selected_id, "enabled": True}})
    update_audio_state(cfg, db, project_id, {"enabled": False})
    assert [item["track_id"] for item in build_render_manifest(cfg, db, project_id)["bgm"]] == [legacy_id]


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


def test_bgm_command_uses_global_timeline_phase_for_later_preview():
    command = build_bgm_mix_command(
        "ffmpeg", Path("timeline.ffconcat"), Path("preview.mp4"),
        {"source_path": "C:/Music Files/旅途.mp3", "gain_db": -12, "start_seconds": 5, "loop": True, "fade_in_seconds": 2, "fade_out_seconds": 3},
        12, {"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        timeline_offset_seconds=60,
        project_duration_seconds=120,
    )
    assert command[command.index("-ss") + 1] == "65.000000"
    graph = command[command.index("-filter_complex") + 1]
    assert "afade=t=in" not in graph
    assert "afade=t=out" not in graph


def _make_audio(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
