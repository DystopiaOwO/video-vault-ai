"""One resolved video encoder contract per formal render job."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Mapping


# VID-33: the NVENC rate-control arguments are part of the persisted render
# contract.  Bump this when switching from the invalid ``-rc cq`` spelling to
# FFmpeg's supported VBR + CQ contract.
ENCODER_CONTRACT_VERSION = "2"


class EncoderContractError(ValueError):
    pass


def resolve_encoder_contract(cfg: Mapping[str, Any], profile: Mapping[str, Any], requested: str | None = None) -> dict[str, Any]:
    choice = str(requested or "auto").lower()
    if choice in {"cpu", "x264", "libx264"}:
        implementation, fallback_reason = "libx264", "explicit_cpu"
    elif choice in {"nvenc", "h264_nvenc"}:
        implementation, fallback_reason = "h264_nvenc", "explicit_nvenc"
    elif choice == "auto":
        if _nvenc_probe(str(cfg.get("ffmpeg_path") or "ffmpeg")):
            implementation, fallback_reason = "h264_nvenc", ""
        else:
            implementation, fallback_reason = "libx264", "nvenc_probe_failed"
    else:
        raise EncoderContractError(f"unsupported encoder request: {choice}")
    fps = _fps_fraction(profile)
    contract = {
        "version": ENCODER_CONTRACT_VERSION,
        "requested": choice,
        "implementation": implementation,
        "fallback_reason": fallback_reason,
        "preset": "p5" if implementation == "h264_nvenc" else "medium",
        "rate_control": "vbr" if implementation == "h264_nvenc" else "crf",
        "quality": 23,
        "h264_profile": "high",
        "h264_level": "4.2",
        "gop": max(1, round(fps[0] / fps[1] * 2)),
        "bf": 2,
        "fps_num": fps[0],
        "fps_den": fps[1],
        "pixel_format": str(profile.get("pixel_format") or "yuv420p"),
        "ffmpeg_version": _ffmpeg_version(str(cfg.get("ffmpeg_path") or "ffmpeg")),
    }
    contract["contract_hash"] = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return contract


def validate_encoder_contract(contract: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    if str(contract.get("version") or "") != ENCODER_CONTRACT_VERSION:
        raise EncoderContractError("unsupported encoder contract version")
    if str(contract.get("implementation") or "") not in {"libx264", "h264_nvenc"}:
        raise EncoderContractError("unsupported resolved encoder")
    expected = dict(contract)
    supplied_hash = str(expected.pop("contract_hash", ""))
    if hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != supplied_hash:
        raise EncoderContractError("encoder contract hash mismatch")
    fps = _fps_fraction(profile)
    if (int(contract.get("fps_num") or 0), int(contract.get("fps_den") or 0)) != fps:
        raise EncoderContractError("encoder contract FPS mismatch")
    if str(contract.get("pixel_format") or "") != str(profile.get("pixel_format") or ""):
        raise EncoderContractError("encoder contract pixel format mismatch")


def encoder_arguments(contract: Mapping[str, Any]) -> list[str]:
    implementation = str(contract["implementation"])
    args = ["-c:v", implementation, "-profile:v", str(contract["h264_profile"]), "-level:v", str(contract["h264_level"]), "-g", str(int(contract["gop"])), "-bf", str(int(contract["bf"]))]
    if implementation == "h264_nvenc":
        args += ["-preset", str(contract["preset"]), "-rc", str(contract["rate_control"]), "-cq", str(int(contract["quality"]))]
    else:
        args += ["-preset", str(contract["preset"]), "-crf", str(int(contract["quality"]))]
    return args


def _nvenc_probe(ffmpeg_path: str) -> bool:
    # 16x16 is below the minimum frame dimension accepted by the RTX 5070 Ti
    # NVENC path.  Keep this short, but use a legal frame and the same basic
    # options as the formal h264_nvenc contract.
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:d=0.04",
        "-frames:v",
        "1",
        "-c:v",
        "h264_nvenc",
        "-profile:v",
        "high",
        "-level:v",
        "4.2",
        "-preset",
        "p5",
        "-rc",
        "vbr",
        "-cq",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "null",
        "-",
    ]
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ffmpeg_version(ffmpeg_path: str) -> str:
    try:
        output = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, encoding="utf-8", check=False, timeout=5).stdout.splitlines()
        return output[0].strip() if output else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _fps_fraction(profile: Mapping[str, Any]) -> tuple[int, int]:
    raw = str(profile.get("fps") or "30")
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        return max(1, int(numerator)), max(1, int(denominator))
    value = float(raw)
    if abs(value - 59.94) < 0.01:
        return 60000, 1001
    if abs(value - 29.97) < 0.01:
        return 30000, 1001
    return int(round(value)), 1


__all__ = ["ENCODER_CONTRACT_VERSION", "EncoderContractError", "encoder_arguments", "resolve_encoder_contract", "validate_encoder_contract"]
