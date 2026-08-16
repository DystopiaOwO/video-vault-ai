from pathlib import Path
import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from video_vault.final_qc import FinalQCResult, validate_final_output
from video_vault.project_renderer import _final_cache_miss_reason, _policy_key, _resolve_current_gpu_execution
from video_vault.media_probe import MediaProbe, SourceProbeRegistry
from video_vault.render_api import build_render_report_dto
from video_vault.render_profiles import get_render_profile
from video_vault.segment_cache import build_segment_cache_key


def _current_gpu_execution(segment_id: str, contract_hash: str, cache_key: str, implementation: str = "cpu") -> dict:
    return {
        "contracts": [{
            "segment_id": segment_id,
            "contract_version": "1",
            "contract_hash": contract_hash,
            "implementation": implementation,
        }],
        "cache_keys": [{"segment_id": segment_id, "cache_key": cache_key}],
    }


def test_render_report_dto_redacts_local_paths():
    report = {
        "project_id": 7,
        "manifest_hash": "m" * 64,
        "profile_id": "final_1080p",
        "output_path": r"D:\VideoLibrary\08_projects\project_7\renders\final.mp4",
        "output_size": 123,
        "output_sha256": "s" * 64,
        "color": {"effective_source": "project", "lut_path": r"C:\Users\b3b3b\Downloads\look.cube"},
        "bgm": {"source_path": r"D:\VideoLibrary\04_audio\bgm\city.mp3", "title": "City"},
        "qc": {"passed": True, "errors": [], "warnings": []},
    }

    dto = build_render_report_dto(report, currentity="current").to_dict()
    encoded = json.dumps(dto, ensure_ascii=False)

    assert dto["status"] == "current"
    assert dto["output"]["filename"] == "final.mp4"
    assert "D:\\VideoLibrary" not in encoded
    assert "C:\\Users" not in encoded
    assert dto["bgm"]["title"] == "City"


def test_final_cache_revalidation_binds_snapshot_and_encoder_contract(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "final.mp4"
    output.write_bytes(b"final")
    manifest = {
        "manifest_hash": "m" * 64,
        "profile": {"profile_id": "final_1080p"},
        "settings": {"encoder": "cpu"},
        "segments": [{
            "segment_id": "seg-1", "order": 1, "source_file": str(source),
            "source_in_seconds": 0.0, "source_out_seconds": 1.0, "speed": 1.0,
        }],
    }
    cache_key = build_segment_cache_key(manifest, manifest["segments"][0])
    contract = {"contract_hash": "encoder-contract"}
    snapshot = {"snapshot_id": "snapshot-1", "snapshot_hash": "snapshot-hash"}
    report = {
        "manifest_hash": "m" * 64,
        "profile_id": "final_1080p",
        "qc_schema_version": 2,
        "approval_snapshot": snapshot,
        "encoder_contract": contract,
        "gpu_execution_contract_version": "1",
        "gpu_execution_requested": "auto",
        "gpu_execution_segments": [{"segment_id": "seg-1", "contract_version": "1", "contract_hash": "cpu-test", "implementation": "cpu"}],
        "cache": {
            "qc_policy_version": 2,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "encoder_contract_hash": contract["contract_hash"],
            "loudness_policy_key": _policy_key(None),
        },
        "segments": [{"segment_id": "seg-1", "cache_key": cache_key}],
        "bgm": {"fingerprint": {}},
        "output_size": output.stat().st_size,
        "output_sha256": "digest",
        "qc": {"passed": True},
    }
    report_path = output.with_name(output.name + ".render.json")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("video_vault.project_renderer.sha256_file", lambda _path: "digest")
    monkeypatch.setattr("video_vault.project_renderer.validate_final_output", lambda *args, **kwargs: FinalQCResult(True, 1.0, "digest"))

    assert _final_cache_miss_reason(
        output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe",
        approval_snapshot=snapshot, encoder_contract=contract,
        current_gpu_execution=_current_gpu_execution("seg-1", "cpu-test", cache_key),
    ) == ""

    report["cache"]["encoder_contract_hash"] = "changed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert _final_cache_miss_reason(
        output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe",
        approval_snapshot=snapshot, encoder_contract=contract,
    ) == "encoder_contract_changed"


def test_report_cache_revalidation_fails_closed_when_qc_probe_errors(monkeypatch, tmp_path: Path):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"final")
    report_path = output.with_name(output.name + ".render.json")
    report_path.write_text(json.dumps({
        "manifest_hash": "m", "profile_id": "final_1080p", "qc_schema_version": 2,
        "gpu_execution_contract_version": "1", "gpu_execution_requested": "auto", "gpu_execution_segments": [],
        "cache": {"qc_policy_version": 2, "loudness_policy_key": _policy_key(None)},
        "segments": [], "bgm": {"fingerprint": {}}, "output_size": output.stat().st_size,
        "output_sha256": "digest", "qc": {"passed": True},
    }), encoding="utf-8")
    monkeypatch.setattr("video_vault.project_renderer.sha256_file", lambda _path: "digest")
    monkeypatch.setattr("video_vault.project_renderer.validate_final_output", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")))

    reason = _final_cache_miss_reason(output, report_path, {"segments": []}, "m", "final_1080p", None, "ffprobe")

    assert reason == "cache_report_invalid"


def test_final_cache_hit_revalidates_loudness_against_current_policy(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "final.mp4"
    output.write_bytes(b"final")
    manifest = {
        "manifest_hash": "m" * 64,
        "profile": {"profile_id": "final_1080p"},
        "settings": {"encoder": "cpu"},
        "segments": [{
            "segment_id": "seg-1", "order": 1, "source_file": str(source),
            "source_in_seconds": 0.0, "source_out_seconds": 1.0, "speed": 1.0,
        }],
    }
    cache_key = build_segment_cache_key(manifest, manifest["segments"][0])
    policy = {"enabled": True, "target_lufs": -14.0, "true_peak_db": -1.0}
    report = {
        "manifest_hash": "m" * 64, "profile_id": "final_1080p", "qc_schema_version": 2,
        "gpu_execution_contract_version": "1", "gpu_execution_requested": "auto", "gpu_execution_segments": [{"segment_id": "seg-1", "contract_version": "1", "contract_hash": "cpu-test", "implementation": "cpu"}],
        "loudness_policy": policy,
        "loudness": {"final": {"measured_I": -14.0, "measured_TP": -1.0}},
        "cache": {"qc_policy_version": 2, "loudness_policy_key": _policy_key(policy)},
        "segments": [{"segment_id": "seg-1", "cache_key": cache_key}], "bgm": {"fingerprint": {}},
        "output_size": output.stat().st_size, "output_sha256": "digest", "qc": {"passed": True},
    }
    report_path = output.with_name(output.name + ".render.json")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    measured = SimpleNamespace(measured_I=-10.0, measured_TP=-0.5)
    captured: dict[str, object] = {}

    def fake_validate(*args, **kwargs):
        captured["loudness"] = kwargs.get("loudness")
        return FinalQCResult(False, 1.0, "digest", ("loudness mismatch",)) if kwargs.get("loudness") is not None else FinalQCResult(True, 1.0, "digest")

    monkeypatch.setattr("video_vault.project_renderer.sha256_file", lambda _path: "digest")
    monkeypatch.setattr("video_vault.project_renderer.validate_final_output", fake_validate)
    monkeypatch.setattr("video_vault.loudness.measure_loudness", lambda *_args, **_kwargs: measured)

    reason = _final_cache_miss_reason(
        output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe",
        loudness_policy=policy,
        current_gpu_execution=_current_gpu_execution("seg-1", "cpu-test", cache_key),
    )

    assert reason == "final_qc_revalidation_failed"
    assert captured["loudness"] is measured


def test_final_cache_cpu_fallback_misses_when_current_gpu_contract_changes(monkeypatch, tmp_path: Path):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"final")
    report_path = output.with_name(output.name + ".render.json")
    manifest = {
        "manifest_hash": "m" * 64,
        "profile": {"profile_id": "final_1080p"},
        "segments": [{"segment_id": "seg-1", "order": 1}],
    }
    report = {
        "manifest_hash": "m" * 64,
        "profile_id": "final_1080p",
        "qc_schema_version": 2,
        "gpu_execution_contract_version": "1",
        "gpu_execution_requested": "auto",
        "gpu_execution_segments": [{"segment_id": "seg-1", "contract_version": "1", "contract_hash": "cpu-hash", "implementation": "cpu"}],
        "cache": {"qc_policy_version": 2, "loudness_policy_key": _policy_key(None)},
        "segments": [{"segment_id": "seg-1", "cache_key": "cpu-key"}],
        "bgm": {"fingerprint": {}},
        "output_size": output.stat().st_size,
        "output_sha256": "digest",
        "qc": {"passed": True},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("video_vault.project_renderer.sha256_file", lambda _path: "digest")
    monkeypatch.setattr("video_vault.project_renderer.validate_final_output", lambda *args, **kwargs: FinalQCResult(True, 1.0, "digest"))

    current = _current_gpu_execution("seg-1", "gpu-hash", "gpu-key", "nvdec_cuda")
    reason = _final_cache_miss_reason(output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe", current_gpu_execution=current)

    assert reason == "gpu_execution_contract_changed"


def test_final_cache_identical_gpu_contract_hits_and_diagnostics_do_not_matter(monkeypatch, tmp_path: Path):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"final")
    report_path = output.with_name(output.name + ".render.json")
    manifest = {"manifest_hash": "m" * 64, "profile": {"profile_id": "final_1080p"}, "segments": [{"segment_id": "seg-1", "order": 1}]}
    report = {
        "manifest_hash": "m" * 64,
        "profile_id": "final_1080p",
        "qc_schema_version": 2,
        "gpu_execution_contract_version": "1",
        "gpu_execution_requested": "auto",
        "gpu_execution_segments": [{"segment_id": "seg-1", "contract_version": "1", "contract_hash": "gpu-hash", "implementation": "nvdec_cuda", "capability_probe": {"stderr_tail": "old"}}],
        "cache": {"qc_policy_version": 2, "loudness_policy_key": _policy_key(None)},
        "segments": [{"segment_id": "seg-1", "cache_key": "gpu-key"}],
        "bgm": {"fingerprint": {}},
        "output_size": output.stat().st_size,
        "output_sha256": "digest",
        "qc": {"passed": True},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("video_vault.project_renderer.sha256_file", lambda _path: "digest")
    monkeypatch.setattr("video_vault.project_renderer.validate_final_output", lambda *args, **kwargs: FinalQCResult(True, 1.0, "digest"))
    current = _current_gpu_execution("seg-1", "gpu-hash", "gpu-key", "nvdec_cuda")

    assert _final_cache_miss_reason(output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe", current_gpu_execution=current) == ""
    report["segments"][0]["cache_key"] = "different-key"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert _final_cache_miss_reason(output, report_path, manifest, "m" * 64, "final_1080p", None, "ffprobe", current_gpu_execution=current) == "segment_cache_changed"


def test_current_gpu_resolution_reuses_source_probe_registry(monkeypatch, tmp_path: Path):
    import video_vault.media_probe as media_probe

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    calls = {"probe": 0}
    fake_probe = MediaProbe(source, 2.0, True, True, 1280, 720, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2)

    def fake_metadata(_ffprobe, _path):
        calls["probe"] += 1
        return fake_probe

    monkeypatch.setattr(media_probe, "probe_media_metadata", fake_metadata)
    stat = source.stat()
    registry = SourceProbeRegistry("ffprobe", approved_fingerprints={str(source.resolve()): {"canonical_path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": "a" * 64}})

    class FakeGPU:
        def resolve(self, *_args):
            return {"version": "1", "contract_hash": "gpu-hash", "implementation": "nvdec_cuda"}

    manifest = {
        "profile": {"profile_id": "final_1080p", "width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        "settings": {"encoder": "auto"},
        "segments": [
            {"segment_id": "a", "order": 1, "source_file": str(source), "source_in_seconds": 0, "source_out_seconds": 1, "speed": 1},
            {"segment_id": "b", "order": 2, "source_file": str(source), "source_in_seconds": 1, "source_out_seconds": 2, "speed": 1},
        ],
    }
    result = _resolve_current_gpu_execution(manifest, None, registry, FakeGPU())

    assert len(result["contracts"]) == 2
    assert calls["probe"] == 1
    assert registry.audit()["source_probe_calls"] == 1
    assert registry.audit()["source_probe_cache_hits"] == 1


@pytest.mark.media_smoke
@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe not installed")
def test_final_qc_media_probe_records_decode_and_timestamp_measurements(tmp_path: Path):
    output = tmp_path / "qc-media.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(output),
    ], check=True)
    profile = get_render_profile("final_1080p")
    assert profile["color_primaries"] == "bt709"
    assert profile["color_transfer"] == "bt709"
    assert profile["color_matrix"] == "bt709"
    assert profile["color_range"] == "tv"
    result = validate_final_output(
        output, {"profile": profile, "segments": [{"timeline_duration_seconds": 1.0}],},
        shutil.which("ffprobe"), ffmpeg_path=shutil.which("ffmpeg"),
    )

    assert result.passed, result.errors
    assert result.measurements["decode"]["ok"] is True
    assert result.measurements["timestamp_monotonic"] is True
    assert result.measurements["frame_count"] == 30
