from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from video_vault.encoder_contract import EncoderContractError, encoder_arguments, resolve_encoder_contract, validate_encoder_contract
from video_vault.loudness import LoudnessError, _loudnorm_json
from video_vault.project_renderer import build_render_report
from video_vault.render_api import build_render_report_dto
from video_vault.final_qc import FinalQCResult


def _profile() -> dict:
    return {"fps": 30, "pixel_format": "yuv420p"}


def test_auto_encoder_contract_is_pinned_to_cpu_after_probe_failure(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: False)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({"ffmpeg_path": "ffmpeg"}, _profile(), "auto")
    assert contract["implementation"] == "libx264"
    assert contract["fallback_reason"] == "nvenc_probe_failed"
    assert contract["nvenc_probe"]["attempted"] is True
    assert contract["nvenc_probe"]["result"] == "failed"
    assert contract["nvenc_probe"]["geometry"] == "256x256"
    assert contract["nvenc_probe"]["returncode"] is None
    validate_encoder_contract(contract, _profile())


def test_nvenc_probe_uses_legal_frame_and_supported_contract(monkeypatch):
    import video_vault.encoder_contract as module

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        # Model the observed host behavior: the old 16x16 fixture fails, while
        # the legal 256x256 fixture succeeds.
        return SimpleNamespace(returncode=1 if "color=c=black:s=16x16:d=0.04" in command else 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    audit = module._nvenc_probe("ffmpeg")
    assert audit["result"] == "pass"
    assert audit["returncode"] == 0
    command = calls[0]
    assert "color=c=black:s=256x256:d=0.04" in command
    assert "color=c=black:s=16x16:d=0.04" not in command
    assert command[command.index("-c:v") + 1] == "h264_nvenc"
    assert command[command.index("-preset") + 1] == "p5"
    assert command[command.index("-rc") + 1] == "vbr"
    assert command[command.index("-cq") + 1] == "23"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_nvenc_probe_failure_keeps_bounded_stderr_tail(monkeypatch):
    import video_vault.encoder_contract as module

    long_stderr = "prefix\n" + ("diagnostic-line\n" * 500)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=7, stderr=long_stderr))
    audit = module._nvenc_probe("ffmpeg")
    assert audit["attempted"] is True
    assert audit["result"] == "failed"
    assert audit["returncode"] == 7
    assert len(audit["stderr_tail"]) <= 2000
    assert audit["stderr_tail"].endswith("diagnostic-line\n")
    assert audit["failure_class"] == "encoder_initialization_failed"
    assert audit["timed_out"] is False
    assert audit["start_failed"] is False


def test_nvenc_probe_timeout_is_audited_and_fails_closed(monkeypatch):
    import video_vault.encoder_contract as module

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 15, stderr="timed out")

    monkeypatch.setattr(module.subprocess, "run", raise_timeout)
    audit = module._nvenc_probe("ffmpeg")
    assert audit["result"] == "failed"
    assert audit["failure_class"] == "timeout"
    assert audit["timed_out"] is True
    assert audit["start_failed"] is False
    assert audit["returncode"] is None


def test_nvenc_probe_start_failure_is_audited_and_fails_closed(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing ffmpeg")))
    audit = module._nvenc_probe("ffmpeg")
    assert audit["result"] == "failed"
    assert audit["failure_class"] == "start_failed"
    assert audit["start_failed"] is True
    assert audit["timed_out"] is False
    assert audit["returncode"] is None


def test_nvenc_contract_arguments_use_vbr_cq_not_invalid_rc_cq(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: True)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({}, _profile(), "auto")
    assert contract["implementation"] == "h264_nvenc"
    assert contract["nvenc_probe"]["result"] == "pass"
    assert contract["nvenc_probe"]["returncode"] == 0
    assert contract["rate_control"] == "vbr"
    args = encoder_arguments(contract)
    assert args[args.index("-rc") + 1] == "vbr"
    assert args[args.index("-cq") + 1] == "23"


def test_probe_diagnostics_do_not_change_semantic_contract_hash(monkeypatch):
    import video_vault.encoder_contract as module

    audits = iter((
        {"attempted": True, "geometry": "256x256", "result": "failed", "returncode": 7, "stderr_tail": "first", "failure_class": "encoder_initialization_failed"},
        {"attempted": True, "geometry": "256x256", "result": "failed", "returncode": 7, "stderr_tail": "second and different", "failure_class": "encoder_initialization_failed"},
    ))
    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: next(audits))
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    first = resolve_encoder_contract({}, _profile(), "auto")
    second = resolve_encoder_contract({}, _profile(), "auto")
    assert first["nvenc_probe"]["stderr_tail"] != second["nvenc_probe"]["stderr_tail"]
    assert first["contract_hash"] == second["contract_hash"]
    validate_encoder_contract(first, _profile())
    validate_encoder_contract(second, _profile())


def test_render_report_and_api_expose_encoder_probe_audit(tmp_path):
    import video_vault.encoder_contract as module

    contract = {
        "version": "2",
        "requested": "auto",
        "implementation": "libx264",
        "fallback_reason": "nvenc_probe_failed",
        "preset": "medium",
        "rate_control": "crf",
        "quality": 23,
        "h264_profile": "high",
        "h264_level": "4.2",
        "gop": 60,
        "bf": 2,
        "fps_num": 30,
        "fps_den": 1,
        "pixel_format": "yuv420p",
        "ffmpeg_version": "ffmpeg test",
        "nvenc_probe": {
            "attempted": True,
            "geometry": "256x256",
            "result": "failed",
            "returncode": 7,
            "stderr_tail": "bounded",
            "failure_class": "encoder_initialization_failed",
            "timed_out": False,
            "start_failed": False,
        },
    }
    contract["contract_hash"] = module._contract_hash(contract)
    output = tmp_path / "final.mp4"
    output.write_bytes(b"output")
    manifest = {"manifest_hash": "m", "profile": {"profile_id": "final_1080p"}, "segments": []}
    report = build_render_report(
        1,
        manifest,
        output,
        FinalQCResult(True, 1.0, "digest"),
        [],
        None,
        None,
        encoder_contract=contract,
    )
    assert report["encoder_probe_audit"] == contract["nvenc_probe"]
    dto = build_render_report_dto(report, currentity="current").to_dict()
    assert dto["encoder_probe_audit"] == contract["nvenc_probe"]


def test_pre_vid33_encoder_contract_version_is_rejected(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: True)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({}, _profile(), "auto")
    contract["version"] = "1"
    with pytest.raises(EncoderContractError, match="version"):
        validate_encoder_contract(contract, _profile())


def test_encoder_contract_rejects_profile_or_hash_tampering(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: True)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({}, _profile(), "auto")
    contract["gop"] = 999
    with pytest.raises(EncoderContractError, match="hash"):
        validate_encoder_contract(contract, _profile())


def test_loudnorm_requires_measurement_json():
    with pytest.raises(LoudnessError, match="did not return JSON"):
        _loudnorm_json("no json")
    parsed = _loudnorm_json('prefix {"input_i":"-15.2"} suffix')
    assert parsed["input_i"] == "-15.2"
