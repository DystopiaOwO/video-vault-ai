import hashlib
import json
from pathlib import Path
import os

from video_vault.segment_cache import SEGMENT_RENDERER_CONTRACT_VERSION, build_segment_cache_key, cache_key_payload, cache_paths


def _inputs(source: Path, lut: Path | None = None):
    manifest = {
        "project_id": 1,
        "profile": {
            "profile_id": "accurate_preview_1080p",
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
            "encoder": "cpu",
            "audio": {"original_gain_db": 0, "lower_original_gain_db": -12},
            "color": {"mode": "dji_lut" if lut else "none", "lut_path": str(lut or "")},
        },
    }
    segment = {"segment_id": "clip_001", "source_file": str(source), "source_in_seconds": 1, "source_out_seconds": 3, "speed": 1, "audio_role": "keep_original"}
    return manifest, segment


def test_cache_key_is_deterministic_and_tracks_source_and_settings(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    lut = tmp_path / "look.cube"
    lut.write_bytes(b"lut-a")
    manifest, segment = _inputs(source, lut)
    first = build_segment_cache_key(manifest, segment)
    assert first == build_segment_cache_key(manifest, segment)
    for field, value in (("source_in_seconds", 1.5), ("source_out_seconds", 4), ("speed", 2), ("audio_role", "mute")):
        changed = dict(segment, **{field: value})
        assert build_segment_cache_key(manifest, changed) != first
    changed_manifest = {**manifest, "settings": {**manifest["settings"], "encoder": "auto"}}
    assert build_segment_cache_key(changed_manifest, segment) != first
    changed_color = {**manifest, "settings": {**manifest["settings"], "color": {**manifest["settings"]["color"], "highlights": 0.2}}}
    assert build_segment_cache_key(changed_color, segment) != first
    lut.write_bytes(b"lut-b")
    assert build_segment_cache_key(manifest, segment) != first


def test_cache_key_tracks_source_content_even_when_size_and_mtime_match(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-a")
    manifest, segment = _inputs(source)
    first = build_segment_cache_key(manifest, segment)
    stat = source.stat()
    source.write_bytes(b"source-b")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_size == stat.st_size
    assert source.stat().st_mtime_ns == stat.st_mtime_ns
    assert build_segment_cache_key(manifest, segment) != first


def test_approved_source_fingerprint_avoids_rehash_for_cache_key(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest, segment = _inputs(source)
    expected = build_segment_cache_key(manifest, segment)
    fingerprint = {"path": str(source.resolve()), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns, "sha256": ""}
    import hashlib
    fingerprint["sha256"] = hashlib.sha256(b"source").hexdigest()
    monkeypatch.setattr("video_vault.segment_cache._sha256_file", lambda path: (_ for _ in ()).throw(AssertionError("unexpected source hash")))
    assert build_segment_cache_key(manifest, segment, source_fingerprint=fingerprint) == expected
    assert cache_key_payload(manifest, segment, source_fingerprint=fingerprint)["source_sha256"] == fingerprint["sha256"]


def test_cache_paths_use_hash_and_partial_mp4_suffix(tmp_path: Path):
    paths = cache_paths(tmp_path, "abc123")
    assert paths["output"].name == "abc123.mp4"
    assert paths["partial"].name == "abc123.partial.mp4"
    assert paths["metadata"].name == "abc123.json"
    assert "segment_id" not in paths["output"].name


def test_bgm_and_normalization_do_not_invalidate_segment_cache(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest, segment = _inputs(source)
    segment["audio"] = {"role": "keep", "volume_db": 0, "fade_in_seconds": 0.1, "fade_out_seconds": 0.2}
    first = build_segment_cache_key(manifest, segment)
    changed = {**manifest, "settings": {**manifest["settings"], "audio": {"bgm": {"track_id": 99}, "normalization": {"enabled": True}}}}
    assert build_segment_cache_key(changed, segment) == first


def test_segment_audio_change_only_invalidates_that_segment(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest, first_segment = _inputs(source)
    second_segment = dict(first_segment, segment_id="clip_002", source_in_seconds=3, source_out_seconds=5)
    first_key = build_segment_cache_key(manifest, first_segment)
    second_key = build_segment_cache_key(manifest, second_segment)
    changed = dict(first_segment, audio={"role": "keep", "volume_db": -6, "fade_in_seconds": 0.2, "fade_out_seconds": 0.3})
    assert build_segment_cache_key(manifest, changed) != first_key
    assert build_segment_cache_key(manifest, second_segment) == second_key


def _resolved_encoder_contract(implementation: str, contract_hash: str, stderr: str = ""):
    return {
        "version": "2",
        "contract_hash": contract_hash,
        "implementation": implementation,
        "nvenc_probe": {"result": "failed" if stderr else "pass", "stderr_tail": stderr},
    }


def test_segment_cache_binds_resolved_encoder_contract_and_bumps_version(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    raw_manifest, segment = _inputs(source)
    raw_manifest["settings"]["encoder"] = "auto"
    cpu_manifest = {**raw_manifest, "settings": {**raw_manifest["settings"], "encoder_contract": _resolved_encoder_contract("libx264", "cpu-hash")}}
    nvenc_manifest = {**raw_manifest, "settings": {**raw_manifest["settings"], "encoder_contract": _resolved_encoder_contract("h264_nvenc", "nvenc-hash")}}

    assert SEGMENT_RENDERER_CONTRACT_VERSION == 6
    assert build_segment_cache_key(raw_manifest, segment) != build_segment_cache_key(cpu_manifest, segment)
    assert build_segment_cache_key(cpu_manifest, segment) != build_segment_cache_key(nvenc_manifest, segment)
    assert cache_key_payload(nvenc_manifest, segment)["encoder_cache_identity"] == {
        "binding": "resolved_contract",
        "version": "2",
        "hash": "nvenc-hash",
        "implementation": "h264_nvenc",
    }


def test_pre_vid36_v4_auto_payload_cannot_match_resolved_v5_key(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    raw_manifest, segment = _inputs(source)
    raw_manifest["settings"]["encoder"] = "auto"
    legacy_payload = cache_key_payload(raw_manifest, segment)
    legacy_payload["contract_version"] = 4
    legacy_key = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    resolved_manifest = {**raw_manifest, "settings": {**raw_manifest["settings"], "encoder_contract": _resolved_encoder_contract("h264_nvenc", "nvenc-hash")}}
    assert legacy_key != build_segment_cache_key(resolved_manifest, segment)


def test_probe_diagnostics_do_not_change_resolved_encoder_cache_identity(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest, segment = _inputs(source)
    base = {"encoder": "auto", "color": {"mode": "none"}, "audio": {}}
    first = {**manifest, "settings": {**base, "encoder_contract": _resolved_encoder_contract("h264_nvenc", "same-hash", "first stderr")}}
    second = {**manifest, "settings": {**base, "encoder_contract": _resolved_encoder_contract("h264_nvenc", "same-hash", "different stderr")}}
    assert build_segment_cache_key(first, segment) == build_segment_cache_key(second, segment)


def test_explicit_cpu_contract_is_deterministic_and_separate_from_raw_request(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest, segment = _inputs(source)
    explicit = {**manifest, "settings": {**manifest["settings"], "encoder": "cpu", "encoder_contract": _resolved_encoder_contract("libx264", "cpu-hash")}}
    assert build_segment_cache_key(explicit, segment) == build_segment_cache_key(explicit, segment)
    assert build_segment_cache_key(explicit, segment) != build_segment_cache_key(manifest, segment)
