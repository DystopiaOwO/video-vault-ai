from pathlib import Path

import pytest

from video_vault.audio_state import (
    default_audio_state,
    editable_audio_patch,
    effective_segment_audio_settings,
    normalize_audio_state,
    update_audio_state,
)
from video_vault.database import init_db
from video_vault.project import create_project


def _project(tmp_path: Path):
    db = tmp_path / "05_index" / "video_vault.sqlite3"
    init_db(db)
    project_id = create_project(db, "audio", [], category="travel", content_type="travel_diary")
    return {"library_root": str(tmp_path)}, db, project_id


def test_audio_state_normalization_and_legacy_roles():
    state = normalize_audio_state({
        "bgm": {"bgm_id": "4", "volume_db": -80, "fade_in_seconds": 2.2},
        "original_audio": {"default_role": "lower_original"},
        "segments": {"seg-1": {"role": "keep_original", "volume_db": -3}},
    })
    assert state["schema_version"] == 1
    assert state["bgm"]["bgm_id"] == 4
    assert state["bgm"]["volume_db"] == -60
    assert state["original_audio"]["default_role"] == "lower"
    assert state["segments"]["seg-1"]["role"] == "keep"


def test_audio_patch_preserves_server_owned_bgm_fields(tmp_path):
    cfg, db, project_id = _project(tmp_path)
    state = default_audio_state()
    state["bgm"] = {
        **state["bgm"],
        "bgm_id": 7,
        "title": "日記配樂",
        "source_path": str(tmp_path / "private.mp3"),
        "license_name": "CC BY",
    }
    from video_vault.audio_state import save_audio_state
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    updated = update_audio_state(cfg, db, project_id, {"bgm": {"volume_db": -12, "source_path": "C:/attacker.mp3", "license_name": "fake"}})
    assert updated["bgm"]["volume_db"] == -12
    assert updated["bgm"]["source_path"] == str(tmp_path / "private.mp3")
    assert updated["bgm"]["license_name"] == "CC BY"


def test_editable_patch_does_not_include_server_owned_fields():
    patch = editable_audio_patch({"bgm": {"bgm_id": 2, "source_path": "private", "license": {"raw": True}}})
    assert patch == {"bgm": {"bgm_id": 2}}


def test_audio_state_rejects_invalid_role_and_values():
    with pytest.raises(ValueError, match="unsupported audio role"):
        normalize_audio_state({"original_audio": {"default_role": "voice_ai"}})
    with pytest.raises(ValueError, match="target_lufs"):
        normalize_audio_state({"normalization": {"target_lufs": 2}})


def test_editing_only_segment_fade_preserves_inherited_role_and_volume(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    updated = update_audio_state(cfg, db, project_id, {"segments": {"seg-1": {"fade_in_seconds": 0.7}}})
    assert updated["segments"]["seg-1"] == {"fade_in_seconds": 0.7}
    effective = effective_segment_audio_settings(cfg, project_id, {"segment_id": "seg-1", "audio_role": "lower"}, state=updated)
    assert effective["role"] == "lower"
    assert effective["volume_db"] == -8.0
    assert effective["fade_in_seconds"] == 0.7


def test_reset_segment_audio_removes_override_and_follows_project_default(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    update_audio_state(cfg, db, project_id, {"segments": {"seg-1": {"role": "keep", "volume_db": -2}}})
    reset = update_audio_state(cfg, db, project_id, {"segments": {"seg-1": None}})
    assert "seg-1" not in reset["segments"]
    reset["original_audio"]["default_role"] = "mute"
    effective = effective_segment_audio_settings(cfg, project_id, {"segment_id": "seg-1", "audio_role": "lower"}, state=reset)
    assert effective["role"] == "mute"
