from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from video_vault.gpu_execution import GPU_EXECUTION_CONTRACT_VERSION, GPUExecutionRegistry, execution_contract_hash, gpu_execution_cache_identity
from video_vault.media_probe import MediaProbe
from video_vault.segment_cache import build_segment_cache_key
from video_vault.segment_provenance import segment_approval_provenance
from video_vault.segment_renderer import build_segment_ffmpeg_command


def _probe(codec="hevc", pixel_format="yuv420p10le", fps=30):
    return MediaProbe(Path("coffee.mkv"), 8.0, True, True, 3840, 2160, fps, fps, 1, pixel_format, codec, "aac", 48000, 2)


def _manifest(gpu="auto"):
    return {
        "project_id": 1,
        "profile": {"profile_id": "final_1080p", "width": 1920, "height": 1080, "fps": 30, "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        "settings": {"encoder": "auto", "gpu_execution": gpu, "color": {"mode": "none"}, "audio": {}},
    }


def _encoder():
    return {"version": "2", "implementation": "h264_nvenc", "contract_hash": "encoder-contract"}


def _segment():
    return {"segment_id": "clip-001", "source_file": "coffee.mkv", "source_in_seconds": 0, "source_out_seconds": 8, "speed": 1}


def _capability_pass(monkeypatch):
    import video_vault.gpu_execution as module

    monkeypatch.setattr(module, "probe_cuda_capability", lambda _: {"attempted": True, "result": "pass", "returncode": 0, "stderr_tail": "", "failure_class": None, "checks": {}})


def test_supported_coffee_resolves_nvdec_cuda_and_emits_real_path(monkeypatch):
    _capability_pass(monkeypatch)
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), _probe(fps=60), _encoder())
    assert contract["version"] == GPU_EXECUTION_CONTRACT_VERSION
    assert contract["implementation"] == "nvdec_cuda"
    assert contract["decode_used"] == "nvdec"
    assert contract["filter_used"] == "cuda"
    assert contract["hardware_api"] == "cuda"
    assert contract["capability_probe"]["result"] == "pass"

    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "gpu_execution_contract": contract}}
    command = build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, manifest, _segment(), _probe(), output="out.mp4", encoder="h264_nvenc")
    text = " ".join(command)
    assert "-hwaccel cuda" in text
    assert "-hwaccel_output_format cuda" in text
    assert "scale_cuda=1920:1080:format=yuv420p" in text
    assert "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=limited" in text
    assert "-t" in command
    assert "-c:v h264_nvenc" in text
    assert "-pix_fmt" not in command


def test_unsupported_source_gets_explicit_cpu_fallback(monkeypatch):
    _capability_pass(monkeypatch)
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), _probe(codec="mpeg4"), _encoder())
    assert contract["implementation"] == "cpu"
    assert contract["decode_used"] == "cpu"
    assert contract["filter_used"] == "cpu"
    assert contract["fallback_reason"] == "unsupported_decoder:mpeg4"
    pixel_contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), _probe(pixel_format="yuv444p10le"), _encoder())
    assert pixel_contract["implementation"] == "cpu"
    assert pixel_contract["fallback_reason"] == "unsupported_source_pixel_format:yuv444p10le"


def test_rotated_display_geometry_falls_back_before_cuda_scale(monkeypatch):
    _capability_pass(monkeypatch)
    rotated = replace(
        _probe(),
        coded_width=3840,
        coded_height=2176,
        display_aspect_ratio="9:16",
        display_ratio=9 / 16,
        display_width=2160,
        display_height=3840,
        rotation_degrees=-90,
        display_matrix="matrix -90",
        display_geometry_source="display_matrix",
    )
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), rotated, _encoder())
    assert contract["implementation"] == "cpu"
    assert contract["fallback_reason"] == "requires_cpu_display_matrix_transform"
    assert contract["display_geometry"]["display_aspect_ratio"] == "9:16"

    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "gpu_execution_contract": contract}}
    command = build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, manifest, _segment(), rotated, output="out.mp4", encoder="h264_nvenc")
    text = " ".join(command)
    assert "setsar=1" in text
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in text
    assert "scale_cuda=1920:1080" not in text


def test_non_square_sar_is_normalized_before_composition(monkeypatch):
    _capability_pass(monkeypatch)
    sar_probe = replace(
        _probe(),
        width=720,
        height=480,
        coded_width=720,
        coded_height=480,
        sample_aspect_ratio="4:3",
        display_aspect_ratio="2:1",
        display_ratio=2.0,
        display_width=960,
        display_height=480,
    )
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), sar_probe, _encoder())
    assert contract["implementation"] == "cpu"
    assert contract["fallback_reason"] == "requires_cpu_sample_aspect_normalization"
    assert contract["display_geometry"]["sar_normalization"] == "display_pixel_width_before_composition"

    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "gpu_execution_contract": contract}}
    command = build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, manifest, _segment(), sar_probe, output="out.mp4", encoder="libx264")
    text = " ".join(command)
    normalization = "scale=ceil(iw*4/3/2)*2:ih:eval=init,setsar=1"
    assert normalization in text
    assert text.index(normalization) < text.index("scale=1920:1080:force_original_aspect_ratio=decrease")
    assert text.count("setsar=1") == 2


def test_non_square_sar_crop_to_fill_uses_canonical_display_geometry(monkeypatch):
    _capability_pass(monkeypatch)
    sar_probe = replace(
        _probe(),
        width=720,
        height=480,
        coded_width=720,
        coded_height=480,
        sample_aspect_ratio="4:3",
        display_aspect_ratio="2:1",
        display_ratio=2.0,
        display_width=960,
        display_height=480,
    )
    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "display_geometry_policy": "crop_to_fill"}}
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(manifest, _segment(), sar_probe, _encoder())
    command_manifest = {**manifest, "settings": {**manifest["settings"], "gpu_execution_contract": contract}}
    text = " ".join(build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, command_manifest, _segment(), sar_probe, output="out.mp4", encoder="libx264"))
    normalization = "scale=ceil(iw*4/3/2)*2:ih:eval=init,setsar=1"
    assert text.index(normalization) < text.index("scale=1920:1080:force_original_aspect_ratio=increase")
    assert text.index("scale=1920:1080:force_original_aspect_ratio=increase") < text.index("crop=1920:1080")


def test_rotated_non_square_sar_has_deterministic_cpu_fallback(monkeypatch):
    _capability_pass(monkeypatch)
    rotated_sar = replace(
        _probe(),
        sample_aspect_ratio="4:3",
        rotation_degrees=90,
        display_aspect_ratio="3:8",
        display_ratio=3 / 8,
        display_width=2160,
        display_height=5760,
        display_matrix="matrix 90",
        display_geometry_source="display_matrix",
    )
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(_manifest(), _segment(), rotated_sar, _encoder())
    assert contract["implementation"] == "cpu"
    assert contract["fallback_reason"] == "requires_cpu_display_matrix_transform"
    assert contract["display_geometry"]["sample_aspect_ratio"] == "4:3"
    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "gpu_execution_contract": contract}}
    command = build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, manifest, _segment(), rotated_sar, output="out.mp4", encoder="libx264")
    assert "scale=ceil(iw*4/3/2)*2:ih:eval=init,setsar=1" in " ".join(command)


def test_display_geometry_changes_segment_cache_identity(tmp_path):
    source = tmp_path / "coffee.mkv"
    source.write_bytes(b"source")
    segment = {**_segment(), "source_file": str(source)}
    base = _manifest()
    first = {"version": "1", "implementation": "cpu", "decode_used": "cpu", "filter_used": "cpu", "hardware_api": "cpu", "display_geometry": {"display_aspect_ratio": "16:9", "rotation_degrees": 0}}
    second = {**first, "display_geometry": {"display_aspect_ratio": "9:16", "rotation_degrees": -90}}
    manifest_a = {**base, "settings": {**base["settings"], "gpu_execution_contract": first}}
    manifest_b = {**base, "settings": {**base["settings"], "gpu_execution_contract": second}}
    assert build_segment_cache_key(manifest_a, segment) != build_segment_cache_key(manifest_b, segment)


def test_display_geometry_policy_is_explicit_and_deterministic(monkeypatch):
    _capability_pass(monkeypatch)
    rotated = replace(_probe(), rotation_degrees=90, display_ratio=9 / 16, display_aspect_ratio="9:16")
    manifest = {**_manifest(), "settings": {**_manifest()["settings"], "display_geometry_policy": "crop_to_fill"}}
    contract = GPUExecutionRegistry({"ffmpeg_path": "ffmpeg"}).resolve(manifest, _segment(), rotated, _encoder())
    assert contract["display_geometry"]["composition_policy"] == "crop_to_fill"
    assert contract["display_geometry"]["composition_policy_source"] == "manifest.settings"

    command_manifest = {**manifest, "settings": {**manifest["settings"], "gpu_execution_contract": contract}}
    command = build_segment_ffmpeg_command({"ffmpeg_path": "ffmpeg"}, command_manifest, _segment(), rotated, output="out.mp4", encoder="h264_nvenc")
    text = " ".join(command)
    assert "force_original_aspect_ratio=increase" in text
    assert "crop=1920:1080" in text


def test_gpu_execution_cache_identity_differs_from_cpu_and_ignores_diagnostics():
    gpu = {"version": "1", "implementation": "nvdec_cuda", "decode_used": "nvdec", "filter_used": "cuda", "hardware_api": "cuda", "filter_chain": ["scale_cuda=1920:1080:format=yuv420p"], "capability_probe": {"stderr_tail": "first"}}
    gpu2 = {**gpu, "capability_probe": {"stderr_tail": "different"}}
    cpu = {"version": "1", "implementation": "cpu", "decode_used": "cpu", "filter_used": "cpu", "hardware_api": "cpu", "filter_chain": [], "fallback_reason": "cuda_filter_unavailable"}
    assert execution_contract_hash(gpu) == execution_contract_hash(gpu2)
    assert gpu_execution_cache_identity(gpu)["hash"] == gpu_execution_cache_identity(gpu2)["hash"]
    assert gpu_execution_cache_identity(gpu)["hash"] != gpu_execution_cache_identity(cpu)["hash"]


def test_sar_normalization_contract_invalidates_old_geometry_cache():
    old = {
        "version": "1",
        "implementation": "cpu",
        "decode_used": "cpu",
        "filter_used": "cpu",
        "hardware_api": "cpu",
        "filter_chain": ["autorotate", "setsar", "scale", "pad", "fps", "format", "setsar", "setparams"],
        "display_geometry": {"sample_aspect_ratio": "4:3", "display_ratio": 2.0},
    }
    current = {
        **old,
        "filter_chain": ["autorotate", "display_geometry_normalize", "setsar", "scale", "pad", "fps", "format", "setsar", "setparams"],
        "display_geometry": {**old["display_geometry"], "contract_version": "2", "sar_normalization": "display_pixel_width_before_composition"},
    }
    assert execution_contract_hash(old) != execution_contract_hash(current)


def test_gpu_contract_does_not_change_approval_provenance():
    manifest = _manifest()
    segment = {**_segment(), "order": 1, "audio": {"role": "keep_original", "volume_db": 0, "fade_in_seconds": 0, "fade_out_seconds": 0}}
    cpu = {"version": "1", "implementation": "cpu", "decode_used": "cpu", "filter_used": "cpu", "hardware_api": "cpu"}
    gpu = {"version": "1", "implementation": "nvdec_cuda", "decode_used": "nvdec", "filter_used": "cuda", "hardware_api": "cuda", "capability_probe": {"stderr_tail": "runtime"}}
    cpu_manifest = {**manifest, "settings": {**manifest["settings"], "gpu_execution_contract": cpu}}
    gpu_manifest = {**manifest, "settings": {**manifest["settings"], "gpu_execution_contract": gpu}}
    source = {"kind": "source", "sha256": "a" * 64, "size": 100, "mtime_ns": 1, "source_identity": {"file_id": "one"}}
    assert segment_approval_provenance(cpu_manifest, segment, source_fingerprint=source)["hash"] == segment_approval_provenance(gpu_manifest, segment, source_fingerprint=source)["hash"]


def test_cache_key_separates_cpu_and_gpu_execution_contract(tmp_path):
    source = tmp_path / "coffee.mkv"
    source.write_bytes(b"source")
    segment = {**_segment(), "source_file": str(source)}
    base = _manifest()
    cpu = {**base, "settings": {**base["settings"], "gpu_execution_contract": {"version": "1", "implementation": "cpu", "decode_used": "cpu", "filter_used": "cpu", "hardware_api": "cpu"}}}
    gpu = {**base, "settings": {**base["settings"], "gpu_execution_contract": {"version": "1", "implementation": "nvdec_cuda", "decode_used": "nvdec", "filter_used": "cuda", "hardware_api": "cuda", "filter_chain": ["scale_cuda=1920:1080:format=yuv420p"]}}}
    assert build_segment_cache_key(cpu, segment) != build_segment_cache_key(gpu, segment)
    assert build_segment_cache_key(gpu, segment) == build_segment_cache_key(gpu, segment)


def test_probe_capability_failure_is_bounded_and_structured(monkeypatch):
    import video_vault.gpu_execution as module

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="x" * 5000)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    audit = module.probe_cuda_capability("ffmpeg")
    assert audit["result"] == "failed"
    assert audit["returncode"] == 1
    assert len(audit["stderr_tail"]) == 2000
    assert audit["failure_class"] == "capability_probe_failed"


def test_probe_capability_timeout_and_start_failure_are_auditable(monkeypatch):
    import video_vault.gpu_execution as module

    def timeout(*args, **kwargs):
        raise module.subprocess.TimeoutExpired("ffmpeg", 15, stderr=b"timed out")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    timed_out = module.probe_cuda_capability("ffmpeg")
    assert timed_out["failure_class"] == "timeout"
    assert timed_out["timed_out"] is True
    assert timed_out["stderr_tail"] == "timed out"

    def start_failed(*args, **kwargs):
        raise OSError("ffmpeg missing")

    monkeypatch.setattr(module.subprocess, "run", start_failed)
    failed = module.probe_cuda_capability("ffmpeg")
    assert failed["failure_class"] == "start_failed"
    assert failed["stderr_tail"] == "ffmpeg missing"
