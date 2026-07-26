from __future__ import annotations

from types import SimpleNamespace

import pytest

from video_vault.encoder_contract import EncoderContractError, resolve_encoder_contract, validate_encoder_contract
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
