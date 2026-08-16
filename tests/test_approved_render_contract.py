from __future__ import annotations

from types import SimpleNamespace

import pytest

from video_vault.encoder_contract import EncoderContractError, encoder_arguments, resolve_encoder_contract, validate_encoder_contract
from video_vault.loudness import LoudnessError, _loudnorm_json


def _profile() -> dict:
    return {"fps": 30, "pixel_format": "yuv420p"}


def test_auto_encoder_contract_is_pinned_to_cpu_after_probe_failure(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: False)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({"ffmpeg_path": "ffmpeg"}, _profile(), "auto")
    assert contract["implementation"] == "libx264"
    assert contract["fallback_reason"] == "nvenc_probe_failed"
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
    assert module._nvenc_probe("ffmpeg") is True
    command = calls[0]
    assert "color=c=black:s=256x256:d=0.04" in command
    assert "color=c=black:s=16x16:d=0.04" not in command
    assert command[command.index("-c:v") + 1] == "h264_nvenc"
    assert command[command.index("-preset") + 1] == "p5"
    assert command[command.index("-rc") + 1] == "vbr"
    assert command[command.index("-cq") + 1] == "23"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_nvenc_contract_arguments_use_vbr_cq_not_invalid_rc_cq(monkeypatch):
    import video_vault.encoder_contract as module

    monkeypatch.setattr(module, "_nvenc_probe", lambda *_: True)
    monkeypatch.setattr(module, "_ffmpeg_version", lambda *_: "ffmpeg test")
    contract = resolve_encoder_contract({}, _profile(), "auto")
    assert contract["rate_control"] == "vbr"
    args = encoder_arguments(contract)
    assert args[args.index("-rc") + 1] == "vbr"
    assert args[args.index("-cq") + 1] == "23"


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
