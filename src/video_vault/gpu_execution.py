"""Deterministic GPU decode/filter execution contract for segment rendering.

The execution contract is deliberately runtime-only.  It is suitable for
rendered-artifact cache identity and audit evidence, but must never be mixed
into approval-semantic provenance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any, Mapping

from .media_probe import MediaProbe


GPU_EXECUTION_CONTRACT_VERSION = "1"
GPU_PROBE_STDERR_TAIL_LIMIT = 2000
_DIAGNOSTIC_KEYS = frozenset({"capability_probe", "stderr_tail", "timing", "ffmpeg_command"})


class GPUExecutionError(ValueError):
    pass


class GPUExecutionRegistry:
    """Job-scoped CUDA capability probe and contract resolver."""

    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._capability: dict[str, Any] | None = None
        self._contracts: dict[tuple[Any, ...], dict[str, Any]] = {}

    def resolve(
        self,
        manifest: Mapping[str, Any],
        segment: Mapping[str, Any],
        probe: MediaProbe,
        encoder_contract: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        settings = manifest.get("settings") if isinstance(manifest.get("settings"), Mapping) else {}
        profile = manifest.get("profile") if isinstance(manifest.get("profile"), Mapping) else {}
        requested = _requested_mode(self.cfg, settings, encoder_contract)
        encoder = str((encoder_contract or {}).get("implementation") or "")
        key = (
            requested,
            encoder,
            str((encoder_contract or {}).get("contract_hash") or ""),
            str(segment.get("source_file") or ""),
            probe.width,
            probe.height,
            probe.fps_num,
            probe.fps_den,
            probe.video_codec,
            probe.pixel_format,
            str(profile.get("width") or ""),
            str(profile.get("height") or ""),
            str(profile.get("fps") or ""),
            str(profile.get("pixel_format") or ""),
            _color_requested(settings, segment),
        )
        with self._lock:
            cached = self._contracts.get(key)
        if cached is not None:
            return dict(cached)

        contract = _resolve_contract(
            self.cfg,
            settings,
            segment,
            probe,
            profile,
            encoder_contract,
            requested,
            capability=self._get_capability() if requested != "cpu" else _not_requested_capability(),
        )
        with self._lock:
            self._contracts[key] = dict(contract)
        return contract

    def _get_capability(self) -> dict[str, Any]:
        with self._lock:
            if self._capability is not None:
                return dict(self._capability)
        result = probe_cuda_capability(str(self.cfg.get("ffmpeg_path") or "ffmpeg"))
        with self._lock:
            if self._capability is None:
                self._capability = dict(result)
            return dict(self._capability)


def gpu_execution_cache_identity(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return semantic-only identity; diagnostics never affect cache keys."""

    contract = contract if isinstance(contract, Mapping) else {}
    semantic = _semantic_contract(contract)
    return {
        "binding": str(semantic.get("implementation") or "cpu"),
        "version": str(semantic.get("version") or GPU_EXECUTION_CONTRACT_VERSION),
        "hash": execution_contract_hash(contract),
        "decode": str(semantic.get("decode_used") or "cpu"),
        "filter": str(semantic.get("filter_used") or "cpu"),
        "hardware_api": str(semantic.get("hardware_api") or "cpu"),
        "hardware_device": str(semantic.get("hardware_device") or "cpu"),
    }


def execution_contract_hash(contract: Mapping[str, Any] | None) -> str:
    semantic = _semantic_contract(contract or {})
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def probe_cuda_capability(ffmpeg_path: str) -> dict[str, Any]:
    """Probe the local FFmpeg binary for CUDA hardware/filter support."""

    commands = {
        "hwaccel": [ffmpeg_path, "-hide_banner", "-hwaccels"],
        "filters": [ffmpeg_path, "-hide_banner", "-filters"],
    }
    evidence: dict[str, Any] = {"attempted": True, "result": "failed", "returncode": None, "stderr_tail": "", "failure_class": "start_failed", "device": "cuda:0", "checks": {}}
    try:
        for name, command in commands.items():
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=15)
            output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
            evidence["checks"][name] = {"returncode": completed.returncode, "cuda": name == "hwaccel" and _has_cuda(output) or name == "filters" and "scale_cuda" in output}
            if completed.returncode != 0:
                evidence["returncode"] = completed.returncode
                evidence["stderr_tail"] = _stderr_tail(completed.stderr or completed.stdout)
                evidence["failure_class"] = "capability_probe_failed"
                return evidence
        supported = bool(evidence["checks"]["hwaccel"]["cuda"] and evidence["checks"]["filters"]["cuda"])
        evidence["device"] = _query_gpu_device()
        evidence["result"] = "pass" if supported else "failed"
        evidence["returncode"] = 0
        evidence["failure_class"] = None if supported else "cuda_filter_unavailable"
        return evidence
    except subprocess.TimeoutExpired as exc:
        evidence["failure_class"] = "timeout"
        evidence["timed_out"] = True
        evidence["stderr_tail"] = _stderr_tail(getattr(exc, "stderr", ""))
        return evidence
    except OSError as exc:
        evidence["stderr_tail"] = _stderr_tail(str(exc))
        return evidence


def _resolve_contract(
    cfg: Mapping[str, Any],
    settings: Mapping[str, Any],
    segment: Mapping[str, Any],
    probe: MediaProbe,
    profile: Mapping[str, Any],
    encoder_contract: Mapping[str, Any] | None,
    requested: str,
    *,
    capability: Mapping[str, Any],
) -> dict[str, Any]:
    base = {
        "version": GPU_EXECUTION_CONTRACT_VERSION,
        "requested": requested,
        "implementation": "cpu",
        "decode_requested": "nvdec" if requested != "cpu" else "cpu",
        "decode_used": "cpu",
        "filter_requested": "cuda" if requested != "cpu" else "cpu",
        "filter_used": "cpu",
        "hardware_api": "cpu",
        "hardware_device": "cpu",
        "filter_chain": ["scale", "pad", "fps", "format", "setparams"],
        "result": "fallback" if requested != "cpu" else "not_requested",
        "fallback_reason": "explicit_cpu" if requested == "cpu" else "",
        "capability_probe": dict(capability),
    }
    if requested == "cpu":
        return _finalize(base)
    if str((encoder_contract or {}).get("implementation") or "") != "h264_nvenc":
        base["fallback_reason"] = "encoder_not_h264_nvenc"
        return _finalize(base)
    if str(capability.get("result") or "") != "pass":
        base["fallback_reason"] = str(capability.get("failure_class") or "cuda_capability_unavailable")
        return _finalize(base)

    reason = _unsupported_reason(settings, segment, probe, profile)
    if reason:
        base["fallback_reason"] = reason
        return _finalize(base)

    width = int(profile.get("width") or 0)
    height = int(profile.get("height") or 0)
    fps = str(profile.get("fps") or "30")
    pix_fmt = str(profile.get("pixel_format") or "yuv420p")
    scale = f"scale_cuda={width}:{height}:format={pix_fmt}"
    chain = [
        f"trim=start={float(segment.get('source_in_seconds') or 0):.6f}:end={float(segment.get('source_out_seconds') or 0):.6f}",
        "setpts=PTS-STARTPTS",
        f"setpts=PTS/{float(segment.get('speed') or 1):g}",
        scale,
        f"setparams=colorspace={str(profile.get('color_matrix') or 'bt709')}:color_primaries={str(profile.get('color_primaries') or 'bt709')}:color_trc={str(profile.get('color_transfer') or 'bt709')}:range={'limited' if str(profile.get('color_range') or 'tv') == 'tv' else 'full'}",
    ]
    base.update(
        {
            "implementation": "nvdec_cuda",
            "decode_used": "nvdec",
            "filter_used": "cuda",
            "hardware_api": "cuda",
            "hardware_device": str(capability.get("device") or "cuda:0"),
            "filter_chain": chain,
            "output_fps": fps,
            "output_pixel_format": pix_fmt,
            "result": "pass",
            "fallback_reason": "",
        }
    )
    return _finalize(base)


def _requested_mode(cfg: Mapping[str, Any], settings: Mapping[str, Any], encoder_contract: Mapping[str, Any] | None) -> str:
    raw = settings.get("gpu_execution")
    if raw is None:
        raw = cfg.get("gpu_execution")
    if raw is None:
        raw = "auto" if str((encoder_contract or {}).get("implementation") or "") == "h264_nvenc" else "cpu"
    value = str(raw).strip().lower()
    if value in {"cpu", "disabled", "off"}:
        return "cpu"
    if value in {"auto", "cuda", "required", "nvdec"}:
        return "auto" if value == "auto" else "required"
    raise GPUExecutionError(f"unsupported gpu execution request: {raw}")


def _unsupported_reason(settings: Mapping[str, Any], segment: Mapping[str, Any], probe: MediaProbe, profile: Mapping[str, Any]) -> str:
    if probe.video_codec.lower() not in {"hevc", "h265", "h264", "av1", "vp9"}:
        return f"unsupported_decoder:{probe.video_codec or 'unknown'}"
    if not probe.pixel_format:
        return "source_pixel_format_unknown"
    if probe.pixel_format.lower() not in {"yuv420p", "yuv420p10le", "p010le", "nv12"}:
        return f"unsupported_source_pixel_format:{probe.pixel_format}"
    if _color_requested(settings, segment):
        return "unsupported_cuda_filter:color_processing"
    source_ratio = probe.width / probe.height if probe.height else 0.0
    target_ratio = float(profile.get("width") or 0) / float(profile.get("height") or 1)
    if abs(source_ratio - target_ratio) > 0.01:
        return "requires_cpu_pad_or_aspect_conversion"
    # The formal output ``-r`` contract performs deterministic output cadence
    # conversion after the CUDA frame path.  A variable/high-rate source does
    # not by itself require a CPU ``fps`` filter, and treating it as a hard
    # incompatibility would incorrectly reject the authoritative Coffee input.
    return ""


def _color_requested(settings: Mapping[str, Any], segment: Mapping[str, Any]) -> str:
    color = segment.get("color") if isinstance(segment.get("color"), Mapping) else settings.get("color")
    if not isinstance(color, Mapping):
        return ""
    mode = str(color.get("mode") or "none")
    return "" if mode in {"", "none", "off"} else mode


def _not_requested_capability() -> dict[str, Any]:
    return {"attempted": False, "result": "not_requested", "returncode": None, "stderr_tail": "", "failure_class": None, "device": "cpu", "checks": {}}


def _semantic_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in contract.items() if key not in _DIAGNOSTIC_KEYS and key not in {"contract_hash"}}


def _finalize(contract: dict[str, Any]) -> dict[str, Any]:
    result = dict(contract)
    result["contract_hash"] = execution_contract_hash(result)
    return result


def _has_cuda(output: str) -> bool:
    return any(line.strip().lower() == "cuda" for line in str(output).splitlines())


def _stderr_tail(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")[-GPU_PROBE_STDERR_TAIL_LIMIT:]


def _query_gpu_device() -> str:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return "cuda:0"
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
        name = str(result.stdout or "").splitlines()[0].strip() if result.returncode == 0 else ""
        return name or "cuda:0"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "cuda:0"


__all__ = [
    "GPU_EXECUTION_CONTRACT_VERSION",
    "GPUExecutionError",
    "GPUExecutionRegistry",
    "execution_contract_hash",
    "gpu_execution_cache_identity",
    "probe_cuda_capability",
]
