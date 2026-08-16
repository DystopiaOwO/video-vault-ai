from __future__ import annotations

from copy import deepcopy

from video_vault.segment_provenance import SEGMENT_APPROVAL_PROVENANCE_VERSION, segment_approval_provenance


def _manifest() -> dict:
    return {
        "profile": {
            "profile_id": "final_1080p",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        },
        "settings": {
            "encoder": "auto",
            "encoder_contract": {"implementation": "h264_nvenc", "version": "2", "contract_hash": "resolved"},
            "segment_renderer_contract_version": 5,
        },
    }


def _segment() -> dict:
    return {
        "segment_id": "segment-1",
        "order": 1,
        "source_in_seconds": 1.25,
        "source_out_seconds": 5.25,
        "source_duration_seconds": 4.0,
        "speed": 1.0,
        "timeline_duration_seconds": 4.0,
        "audio_role": "lower_original",
        "audio": {"role": "lower_original", "volume_db": -2.0, "fade_in_seconds": 0.1, "fade_out_seconds": 0.2},
    }


def _source(sha: str = "a" * 64) -> dict:
    return {"contract_version": "source-fingerprint-v2", "sha256": sha, "size": 100, "mtime_ns": 200}


def test_runtime_encoder_and_cache_contract_do_not_change_approval_identity():
    manifest = _manifest()
    segment = _segment()
    first = segment_approval_provenance(manifest, segment, source_fingerprint=_source())

    runtime_variant = deepcopy(manifest)
    runtime_variant["settings"]["encoder_contract"] = {
        "implementation": "libx264",
        "version": "99",
        "contract_hash": "different",
        "nvenc_probe": {"stderr_tail": "different diagnostics"},
    }
    runtime_variant["settings"]["segment_renderer_contract_version"] = 4
    second = segment_approval_provenance(runtime_variant, segment, source_fingerprint=_source())

    assert first["version"] == second["version"] == SEGMENT_APPROVAL_PROVENANCE_VERSION
    assert first["hash"] == second["hash"]
    assert "encoder_contract" not in first["payload"]
    assert "segment_renderer_contract_version" not in first["payload"]
    assert "nvenc_probe" not in first["payload"]
    assert "cache_key" not in first["payload"]
    assert "artifact_path" not in first["payload"]


def test_audio_and_source_semantic_mutations_change_identity():
    manifest = _manifest()
    segment = _segment()
    baseline = segment_approval_provenance(manifest, segment, source_fingerprint=_source())["hash"]

    mutations = (
        {"audio": {"role": "mute", "volume_db": -2.0, "fade_in_seconds": 0.1, "fade_out_seconds": 0.2}},
        {"audio": {"role": "lower_original", "volume_db": -3.0, "fade_in_seconds": 0.1, "fade_out_seconds": 0.2}},
        {"source_in_seconds": 2.25},
        {"source_out_seconds": 6.25},
        {"speed": 1.25},
    )
    for mutation in mutations:
        changed = deepcopy(segment)
        changed.update(mutation)
        assert segment_approval_provenance(manifest, changed, source_fingerprint=_source())["hash"] != baseline

    assert segment_approval_provenance(manifest, segment, source_fingerprint=_source("b" * 64))["hash"] != baseline


def test_order_and_missing_or_extra_semantics_are_distinct():
    manifest = _manifest()
    first = _segment()
    second = {**_segment(), "segment_id": "segment-2", "order": 2, "source_in_seconds": 10.0, "source_out_seconds": 14.0}
    first_hash = segment_approval_provenance(manifest, first, source_fingerprint=_source())["hash"]
    reordered = {**first, "order": 2}
    assert segment_approval_provenance(manifest, reordered, source_fingerprint=_source())["hash"] != first_hash
    assert segment_approval_provenance(manifest, second, source_fingerprint=_source())["hash"] != first_hash
