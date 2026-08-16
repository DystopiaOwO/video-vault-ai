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
NVENC_PROBE_GEOMETRY = "256x256"
NVENC_PROBE_STDERR_TAIL_LIMIT = 2000
_DIAGNOSTIC_CONTRACT_KEYS = frozenset({"nvenc_probe"})


class EncoderContractError(ValueError):
    pass


def resolve_encoder_contract(cfg: Mapping[str, Any], profile: Mapping[str, Any], requested: str | None = None) -> dict[str, Any]:
    choice = str(requested or "auto").lower()
    probe_audit: dict[str, Any] | None = None
    if choice in {"cpu", "x264", "libx264"}:
        implementation, fallback_reason = "libx264", "explicit_cpu"
    elif choice in {"nvenc", "h264_nvenc"}:
        implementation, fallback_reason = "h264_nvenc", "explicit_nvenc"
    elif choice == "auto":
        probe_audit = _normalize_probe_audit(_nvenc_probe(str(cfg.get("ffmpeg_path") or "ffmpeg")))
        if probe_audit["result"] == "pass" and probe_audit["returncode"] == 0:
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
    if probe_audit is not None:
        # Diagnostics are persisted for auditability but deliberately excluded
        # from semantic identity and all cache hashes.
        contract["nvenc_probe"] = probe_audit
    contract["contract_hash"] = _contract_hash(contract)
    return contract


def validate_encoder_contract(contract: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    if str(contract.get("version") or "") != ENCODER_CONTRACT_VERSION:
        raise EncoderContractError("unsupported encoder contract version")
    if str(contract.get("implementation") or "") not in {"libx264", "h264_nvenc"}:
        raise EncoderContractError("unsupported resolved encoder")
    expected = dict(contract)
    supplied_hash = str(expected.pop("contract_hash", ""))
    if _contract_hash(expected) != supplied_hash:
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


def _nvenc_probe(ffmpeg_path: str) -> dict[str, Any]:
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
        f"color=c=black:s={NVENC_PROBE_GEOMETRY}:d=0.04",
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
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=15)
        returncode = getattr(completed, "returncode", None)
        stderr_tail = _stderr_tail(getattr(completed, "stderr", ""))
        passed = returncode == 0
        return {
            "attempted": True,
            "geometry": NVENC_PROBE_GEOMETRY,
            "result": "pass" if passed else "failed",
            "returncode": returncode,
            "stderr_tail": stderr_tail,
            "failure_class": None if passed else _classify_probe_failure(stderr_tail),
            "timed_out": False,
            "start_failed": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True,
            "geometry": NVENC_PROBE_GEOMETRY,
            "result": "failed",
            "returncode": None,
            "stderr_tail": _stderr_tail(getattr(exc, "stderr", "")),
            "failure_class": "timeout",
            "timed_out": True,
            "start_failed": False,
        }
    except OSError as exc:
        return {
            "attempted": True,
            "geometry": NVENC_PROBE_GEOMETRY,
            "result": "failed",
            "returncode": None,
            "stderr_tail": _stderr_tail(getattr(exc, "strerror", "")),
            "failure_class": "start_failed",
            "timed_out": False,
            "start_failed": True,
        }


def _normalize_probe_audit(value: Mapping[str, Any] | bool) -> dict[str, Any]:
    """Normalize test doubles and real probe results to the audit contract."""

    if isinstance(value, Mapping):
        result = "pass" if str(value.get("result") or "") == "pass" else "failed"
        return {
            "attempted": bool(value.get("attempted", True)),
            "geometry": str(value.get("geometry") or NVENC_PROBE_GEOMETRY),
            "result": result,
            "returncode": value.get("returncode"),
            "stderr_tail": _stderr_tail(value.get("stderr_tail", "")),
            "failure_class": value.get("failure_class"),
            "timed_out": bool(value.get("timed_out", False)),
            "start_failed": bool(value.get("start_failed", False)),
        }
    passed = bool(value)
    return {
        "attempted": True,
        "geometry": NVENC_PROBE_GEOMETRY,
        "result": "pass" if passed else "failed",
        "returncode": 0 if passed else None,
        "stderr_tail": "",
        "failure_class": None if passed else "encoder_initialization_failed",
        "timed_out": False,
        "start_failed": False,
    }


def _stderr_tail(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    return text[-NVENC_PROBE_STDERR_TAIL_LIMIT:]


def _classify_probe_failure(stderr_tail: str) -> str:
    text = str(stderr_tail or "").lower()
    if "frame dimension" in text or "minimum supported value" in text:
        return "geometry_invalid"
    if "unknown encoder" in text or "encoder not found" in text:
        return "encoder_unavailable"
    if any(token in text for token in ("nvencodeapi", "driver does not support", "minimum required nvidia driver")):
        return "driver_api_mismatch"
    if any(token in text for token in ("no capable devices", "openencodesessionex", "cannot init cuda", "cannot initialize cuda")):
        return "device_initialization_failed"
    if any(token in text for token in ("unable to parse", "error setting option", "invalid option")):
        return "encoder_option_incompatible"
    return "encoder_initialization_failed"


def _contract_hash(contract: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in contract.items()
        if key != "contract_hash" and key not in _DIAGNOSTIC_CONTRACT_KEYS
    }
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
