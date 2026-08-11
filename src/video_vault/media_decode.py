from __future__ import annotations

from typing import Any, Mapping


class MediaDecodeConfigError(ValueError):
    pass


def perception_decode_contract(cfg: Mapping[str, Any]) -> dict[str, Any]:
    sampling = cfg.get("sampling") or {}
    requested = str(sampling.get("hardware_decode") or "software").strip().lower()
    if requested in {"software", "cpu", "none", "disabled"}:
        return {
            "requested": requested,
            "implementation": "software",
            "ffmpeg_input_args": [],
            "fallback": False,
        }
    if requested in {"cuda", "nvdec", "nvidia"}:
        return {
            "requested": requested,
            "implementation": "cuda_nvdec",
            "ffmpeg_input_args": ["-hwaccel", "cuda"],
            "fallback": False,
        }
    raise MediaDecodeConfigError(f"unsupported sampling hardware_decode: {requested}")


def perception_decode_args(cfg: Mapping[str, Any]) -> list[str]:
    return list(perception_decode_contract(cfg)["ffmpeg_input_args"])


__all__ = [
    "MediaDecodeConfigError",
    "perception_decode_args",
    "perception_decode_contract",
]
