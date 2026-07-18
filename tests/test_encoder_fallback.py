import pytest
from video_vault.render_errors import EncoderError, is_encoder_fallback_error, run_with_encoder_fallback


def test_only_nvenc_errors_fallback():
    assert is_encoder_fallback_error("Cannot load nvcuda.dll")
    assert is_encoder_fallback_error("No NVENC capable devices found")
    assert not is_encoder_fallback_error("Filter syntax error")


def test_fallback_runs_once():
    calls = []
    def render(encoder):
        calls.append(encoder)
        if encoder == "h264_nvenc": raise EncoderError("failed", stderr="NVENC initialization failed")
        return "ok"
    assert run_with_encoder_fallback(render, "h264_nvenc") == ("ok", "libx264")
    assert calls == ["h264_nvenc", "libx264"]


def test_other_errors_are_not_retried():
    calls = []
    def render(encoder):
        calls.append(encoder); raise EncoderError("bad", stderr="Filter syntax error")
    with pytest.raises(EncoderError): run_with_encoder_fallback(render, "h264_nvenc")
    assert calls == ["h264_nvenc"]
