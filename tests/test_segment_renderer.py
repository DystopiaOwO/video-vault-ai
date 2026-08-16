import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from video_vault.media_probe import MediaProbe
from video_vault.render_errors import SegmentRenderError, is_encoder_fallback_error
from video_vault.segment_cache import build_segment_cache_key, cache_key_payload, cache_paths, write_cache_metadata
from video_vault.segment_renderer import build_segment_ffmpeg_command, map_encoder, render_segment


def _probe(has_audio=True):
    return MediaProbe(Path("source.mp4"), 5, True, has_audio, 1280, 720, 24, 24, 1, "yuv420p", "h264", "aac" if has_audio else "", 48000 if has_audio else 0, 2 if has_audio else 0)


def _manifest(settings=None):
    return {"profile": {"profile_id": "accurate_preview_1080p"}, "settings": settings or {"encoder": "cpu", "color": {"mode": "none"}, "audio": {}}}


def test_command_uses_filter_trim_normalization_and_audio():
    command = build_segment_ffmpeg_command(
        {"ffmpeg_path": "ffmpeg"},
        _manifest(),
        {"source_file": "source.mp4", "source_in_seconds": 1.25, "source_out_seconds": 3.875, "speed": 1, "audio_role": "lower_original"},
        _probe(),
        output="out.partial.mp4",
        encoder="libx264",
    )
    text = " ".join(command)
    assert "trim=start=1.250000:end=3.875000" in text
    assert "atrim=start=1.250000:end=3.875000" in text
    assert "setpts=PTS-STARTPTS" in text
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in text
    assert "pad=1920:1080" in text
    assert "fps=30" in text and "format=yuv420p" in text
    assert "-c copy" not in text
    assert "-map [vout] -map [aout]" in text


def test_no_audio_source_gets_silence_input():
    command = build_segment_ffmpeg_command(
        {"ffmpeg_path": "ffmpeg"},
        _manifest(),
        {"source_file": "silent.mp4", "source_in_seconds": 0, "source_out_seconds": 2, "speed": 1, "audio_role": "mute"},
        _probe(False),
        output="out.partial.mp4",
        encoder="libx264",
    )
    text = " ".join(command)
    assert "anullsrc=r=48000:cl=stereo" in text
    assert "-map [aout]" in text


def test_validate_segment_output_uses_fast_metadata_probe(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    output = tmp_path / "segment.mp4"
    output.write_bytes(b"encoded")
    modes = []
    monkeypatch.setattr(renderer, "probe_media", lambda *args: (modes.append(args[2]) or _probe()))
    result = renderer.validate_segment_output(output, {"width": 1280, "height": 720, "fps": 24, "pixel_format": "yuv420p", "audio_sample_rate": 48000, "audio_channels": 2}, 5)
    assert result.passed
    assert modes == ["fast"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("width", 1920, "resolution mismatch"),
        ("fps", 30.0, "fps mismatch"),
        ("pixel_format", "yuv422p", "pixel format mismatch"),
        ("sample_rate", 44100, "sample rate mismatch"),
        ("channels", 1, "channel mismatch"),
    ],
)
def test_validate_segment_output_keeps_format_assertions(monkeypatch, tmp_path: Path, field, value, message):
    import video_vault.segment_renderer as renderer

    output = tmp_path / "segment.mp4"
    output.write_bytes(b"encoded")
    monkeypatch.setattr(renderer, "probe_media", lambda *args: replace(_probe(), **{field: value}))
    result = renderer.validate_segment_output(output, {"width": 1280, "height": 720, "fps": 24, "pixel_format": "yuv420p", "audio_sample_rate": 48000, "audio_channels": 2}, 5)
    assert not result.passed
    assert any(message in error for error in result.errors)


@pytest.mark.parametrize(("requested", "expected"), [("auto", "h264_nvenc"), ("cpu", "libx264"), ("libx264", "libx264"), ("h264_nvenc", "h264_nvenc")])
def test_encoder_mapping(requested, expected):
    assert map_encoder(requested) == expected


def test_fallback_only_matches_encoder_failures():
    assert is_encoder_fallback_error("Cannot load NVENC")
    assert is_encoder_fallback_error("Cannot load libcuda.so.1")
    assert is_encoder_fallback_error("No capable devices found")
    assert not is_encoder_fallback_error("Invalid argument in filter graph")
    assert not is_encoder_fallback_error("No such file or directory: source.mp4")


def _render_inputs(tmp_path: Path, encoder="cpu"):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest = {"project_id": 1, "profile": {"profile_id": "accurate_preview_1080p"}, "settings": {"encoder": encoder, "color": {"mode": "none"}, "audio": {}}}
    segment = {"segment_id": "clip_001", "source_file": str(source), "source_in_seconds": 0, "source_out_seconds": 1, "speed": 1, "timeline_duration_seconds": 1, "audio_role": "keep_original"}
    return {"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, manifest, segment


def _valid_nvenc_contract(monkeypatch):
    import video_vault.encoder_contract as encoder_contract

    monkeypatch.setattr(encoder_contract, "_nvenc_probe", lambda _: {"result": "pass", "returncode": 0, "stderr_tail": ""})
    monkeypatch.setattr(encoder_contract, "_ffmpeg_version", lambda _: "ffmpeg-test")
    return encoder_contract.resolve_encoder_contract({"ffmpeg_path": "ffmpeg"}, {"fps": 30, "pixel_format": "yuv420p"}, "auto")


def test_metadata_publish_failure_rolls_back_all_formal_files(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path)
    monkeypatch.setattr(renderer, "probe_media", lambda *args: _probe())
    monkeypatch.setattr(renderer, "validate_segment_output", lambda *args: renderer.SegmentQCResult(True, 1.0))
    monkeypatch.setattr(renderer, "write_cache_metadata_temp", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("metadata disk failure")))

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=0, stderr="")

    with pytest.raises(SegmentRenderError, match="metadata disk failure"):
        render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=runner)
    assert not list((tmp_path / "cache").glob("*.mp4"))
    assert not list((tmp_path / "cache").glob("*.json"))
    assert not list((tmp_path / "cache").glob("*.partial.mp4"))
    assert not list((tmp_path / "cache").glob(".*.tmp"))


def test_standalone_render_segment_performs_one_fast_source_probe(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path)
    modes = []
    monkeypatch.setattr(renderer, "probe_media", lambda *args: (modes.append(args[2]) or _probe()))
    monkeypatch.setattr(renderer, "validate_segment_output", lambda *args: renderer.SegmentQCResult(True, 1.0))

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=0, stderr="")

    result = renderer.render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=runner)
    assert not result.cache_hit
    assert modes == ["fast"]


def test_resolved_encoder_contract_is_persisted_and_reused_for_cache_hits(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path, encoder="auto")
    contract = _valid_nvenc_contract(monkeypatch)
    manifest["settings"]["encoder_contract"] = contract
    monkeypatch.setattr(renderer, "probe_media", lambda *args: _probe())
    monkeypatch.setattr(renderer, "validate_segment_output", lambda *args: renderer.SegmentQCResult(True, 1.0))

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=0, stderr="")

    first = render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=runner)
    metadata = json.loads(first.output_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["encoder_contract_binding"] == "resolved_contract"
    assert metadata["encoder_contract_version"] == contract["version"]
    assert metadata["encoder_contract_hash"] == contract["contract_hash"]
    assert metadata["encoder_contract_implementation"] == "h264_nvenc"
    assert metadata["encoder_used"] == "h264_nvenc"

    def unexpected_render(*args, **kwargs):
        raise AssertionError("resolved cache should hit")

    hit = render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=unexpected_render)
    assert hit.cache_hit
    assert hit.encoder_used == "h264_nvenc"


def test_legacy_unbound_metadata_cannot_satisfy_resolved_encoder_cache(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path, encoder="auto")
    contract = _valid_nvenc_contract(monkeypatch)
    manifest["settings"]["encoder_contract"] = contract
    source_fingerprint = renderer.resolve_source_fingerprint(Path(segment["source_file"]))
    key = build_segment_cache_key(manifest, segment, source_fingerprint=source_fingerprint)
    payload = cache_key_payload(manifest, segment, source_fingerprint=source_fingerprint)
    paths = cache_paths(tmp_path / "cache", key)
    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    paths["output"].write_bytes(b"legacy")
    write_cache_metadata(paths["metadata"], key, payload, encoder_requested="auto", encoder_used="h264_nvenc")
    monkeypatch.setattr(renderer, "validate_segment_output", lambda *args: renderer.SegmentQCResult(True, 1.0))

    assert not renderer._valid_cache(paths, payload, {"width": 1920, "height": 1080, "fps": 30, "pixel_format": "yuv420p", "audio_sample_rate": 48000, "audio_channels": 2}, 1.0, "ffprobe")


def test_segment_end_beyond_source_duration_fails_closed(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path)
    segment["source_out_seconds"] = 6
    monkeypatch.setattr(renderer, "probe_media", lambda *args: _probe())
    with pytest.raises(SegmentRenderError, match="exceeds source duration"):
        renderer.render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=lambda *args, **kwargs: None)


def test_ffmpeg_filter_failure_keeps_log_without_formal_cache(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path)
    monkeypatch.setattr(renderer, "probe_media", lambda *args: _probe())

    def runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stderr="filter graph error")

    with pytest.raises(SegmentRenderError, match="FFmpeg failed"):
        render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=runner)
    logs = list((tmp_path / "cache").glob("*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "filter graph error" in text and "attempt_1_command" in text
    assert not list((tmp_path / "cache").glob("*.mp4"))
    assert not list((tmp_path / "cache").glob("*.json"))


def test_failed_nvenc_fallback_log_contains_both_attempts(monkeypatch, tmp_path: Path):
    import video_vault.segment_renderer as renderer

    cfg, manifest, segment = _render_inputs(tmp_path, encoder="auto")
    monkeypatch.setattr(renderer, "probe_media", lambda *args: _probe())
    responses = iter([SimpleNamespace(returncode=1, stderr="NVENC unavailable"), SimpleNamespace(returncode=1, stderr="libx264 failure")])

    def runner(command, **kwargs):
        return next(responses)

    with pytest.raises(SegmentRenderError, match="fallback failed"):
        render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache", runner=runner)
    log = next((tmp_path / "cache").glob("*.log")).read_text(encoding="utf-8")
    assert "attempt_1_stderr" in log and "NVENC unavailable" in log
    assert "attempt_2_stderr" in log and "libx264 failure" in log
