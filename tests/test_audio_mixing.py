import json
from pathlib import Path

from video_vault.audio_state import audio_state_for_api, default_audio_state, save_audio_state
from video_vault.bgm_pipeline import build_bgm_mix_command
from video_vault.database import add_bgm_track, add_project_bgm
from video_vault.render_manifest import build_render_manifest
from video_vault.project import project_detail

from test_render_manifest import _project


def test_audio_state_is_compiled_into_manifest_and_bgm_is_single_selected_track(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    first = tmp_path / "one.mp3"
    second = tmp_path / "two.mp3"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
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
    assert manifest["settings"]["audio"]["normalization"]["target_lufs"] == -14


def test_audio_api_state_does_not_expose_local_bgm_path(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path, count=1)
    state = default_audio_state()
    state["bgm"].update({"bgm_id": 1, "source_path": str(tmp_path / "private.mp3")})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    api_state = audio_state_for_api(cfg, project_id, db)
    encoded = json.dumps(api_state, ensure_ascii=False)
    assert "source_path" not in encoded
    assert "private.mp3" not in encoded


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
