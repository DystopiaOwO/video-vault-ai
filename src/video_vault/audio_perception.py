"""Local-first, low-cost audio perception for Phase 5.

The extractor deliberately stays below transcription and cloud review.  It
turns mono PCM into auditable window features and conservative event
candidates.  Recommendations are separate from user audio decisions: this
module never writes ``audio_settings.json``.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5


AUDIO_PERCEPTION_VERSION = "audio-perception-v1"
AUDIO_FEATURE_VERSION = "pcm-features-v1"
EVENT_TYPES = (
    "dialogue",
    "ambience",
    "grinding",
    "pouring",
    "matcha_whisking",
    "transport",
    "invalid_noise",
)
RECOMMENDATIONS = frozenset({"keep", "mute", "duck"})


class AudioPerceptionError(RuntimeError):
    """Raised when local audio extraction cannot produce a trustworthy result."""


@dataclass(frozen=True)
class AudioFeatures:
    rms_db: float
    peak_db: float
    zero_crossing_rate: float
    roughness: float
    clipping_ratio: float
    voiced: bool


def audio_policy(cfg: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(((cfg.get("perception") or {}).get("audio") or {}))
    sample_rate = _bounded_int(raw.get("sample_rate", 16000), 8000, 48000, "sample_rate")
    window_seconds = _bounded_float(raw.get("window_seconds", 1.0), 0.25, 10.0, "window_seconds")
    hop_seconds = _bounded_float(raw.get("hop_seconds", 0.5), 0.1, 10.0, "hop_seconds")
    if hop_seconds > window_seconds:
        raise ValueError("audio hop_seconds cannot exceed window_seconds")
    return {
        "policy_name": str(raw.get("policy_name") or "local-pcm-vad-events"),
        "policy_version": int(raw.get("policy_version", 1)),
        "sample_rate": sample_rate,
        "window_seconds": window_seconds,
        "hop_seconds": hop_seconds,
        "vad_threshold_db": _bounded_float(raw.get("vad_threshold_db", -48.0), -90.0, -5.0, "vad_threshold_db"),
        "max_analysis_seconds": _bounded_float(raw.get("max_analysis_seconds", 1800.0), 1.0, 86400.0, "max_analysis_seconds"),
        "ffmpeg_timeout_seconds": _bounded_float(raw.get("ffmpeg_timeout_seconds", 180.0), 1.0, 3600.0, "ffmpeg_timeout_seconds"),
    }


def analyze_audio_file(
    source: Path,
    cfg: Mapping[str, Any],
    *,
    video_id: int,
    duration_seconds: float = 0.0,
    source_fingerprint: Mapping[str, Any] | None = None,
    visual_segments: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract local PCM features without transcription or network calls."""

    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise AudioPerceptionError(f"audio source does not exist: {source}")
    policy = audio_policy(cfg)
    command = [
        str(cfg.get("ffmpeg_path") or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0?",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(policy["sample_rate"]),
        "-t",
        str(policy["max_analysis_seconds"]),
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=float(policy["ffmpeg_timeout_seconds"]),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioPerceptionError(f"local audio extraction failed: {exc}") from exc
    pcm = bytes(completed.stdout or b"")
    if completed.returncode != 0 and not pcm:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        if _is_no_audio_error(detail):
            return _no_audio_result(video_id, duration_seconds, source_fingerprint, policy)
        raise AudioPerceptionError(f"local audio extraction failed: {detail or completed.returncode}")
    return analyze_pcm(
        pcm,
        sample_rate=int(policy["sample_rate"]),
        video_id=video_id,
        duration_seconds=duration_seconds,
        source_fingerprint=source_fingerprint,
        visual_segments=visual_segments,
        policy=policy,
    )


def analyze_pcm(
    pcm: bytes,
    *,
    sample_rate: int,
    video_id: int,
    duration_seconds: float = 0.0,
    source_fingerprint: Mapping[str, Any] | None = None,
    visual_segments: Sequence[Mapping[str, Any]] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze signed 16-bit little-endian mono PCM deterministically."""

    resolved = dict(policy or {})
    resolved.setdefault("policy_name", "local-pcm-vad-events")
    resolved.setdefault("policy_version", 1)
    resolved.setdefault("window_seconds", 1.0)
    resolved.setdefault("hop_seconds", 0.5)
    resolved.setdefault("vad_threshold_db", -48.0)
    resolved.setdefault("max_analysis_seconds", 1800.0)
    if not pcm:
        return _no_audio_result(video_id, duration_seconds, source_fingerprint, resolved)
    samples = _pcm_samples(pcm)
    raw_duration_seconds = len(samples) / max(1, sample_rate)
    source_duration = float(duration_seconds or 0.0) or raw_duration_seconds
    max_analysis_seconds = float(resolved["max_analysis_seconds"])
    max_samples = max(1, int(round(sample_rate * max_analysis_seconds)))
    truncated = source_duration > max_analysis_seconds + 1e-6 or len(samples) > max_samples
    samples = samples[:max_samples]
    analyzed_duration = min(raw_duration_seconds, max_analysis_seconds)
    window_size = max(1, int(round(sample_rate * float(resolved["window_seconds"]))))
    hop_size = max(1, int(round(sample_rate * float(resolved["hop_seconds"]))))
    candidates: list[dict[str, Any]] = []
    event_counts = {event: 0 for event in EVENT_TYPES}
    recommendation_counts = {recommendation: 0 for recommendation in sorted(RECOMMENDATIONS)}
    offset = 0
    ordinal = 0
    while offset < len(samples):
        chunk = samples[offset : offset + window_size]
        if not chunk:
            break
        start = round(offset / sample_rate, 6)
        end = round(min(len(samples), offset + window_size) / sample_rate, 6)
        features = _features(chunk, float(resolved["vad_threshold_db"]))
        event_candidates = _event_candidates(features)
        best = event_candidates[0]
        event = str(best["event"])
        recommendation = _recommendation(event)
        event_counts[event] += 1
        recommendation_counts[recommendation] += 1
        segment_uuid, identity_source = _stable_segment_identity(
            video_id,
            start,
            end,
            visual_segments or (),
        )
        candidates.append(
            {
                "audio_window_uuid": str(uuid5(NAMESPACE_URL, f"video-vault-ai/audio-window/v1/{video_id}/{start}/{end}")),
                "ordinal": ordinal,
                "segment_uuid": segment_uuid,
                "segment_identity_source": identity_source,
                "start_seconds": start,
                "end_seconds": end,
                "features": {
                    "feature_version": AUDIO_FEATURE_VERSION,
                    "rms_db": features.rms_db,
                    "peak_db": features.peak_db,
                    "zero_crossing_rate": features.zero_crossing_rate,
                    "roughness": features.roughness,
                    "clipping_ratio": features.clipping_ratio,
                    "voiced": features.voiced,
                },
                "event_candidates": event_candidates,
                "event": event,
                "confidence": float(best["confidence"]),
                "natural_audio_recommendation": recommendation,
                "user_audio_decision": None,
                "decision_source": "recommendation_only",
            }
        )
        ordinal += 1
        if offset + window_size >= len(samples):
            break
        offset += hop_size
    partial = bool(truncated)
    return {
        "schema_version": AUDIO_PERCEPTION_VERSION,
        "status": "partial" if partial else "succeeded",
        "provider": "local",
        "model": AUDIO_FEATURE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": dict(source_fingerprint or {}),
        "timeline": {"timebase": "seconds", "start_seconds": 0.0, "duration_seconds": round(analyzed_duration, 6)},
        "policy": dict(resolved),
        "audit": {
            "local_only": True,
            "transcription_requested": False,
            "cloud_audio_requested": False,
            "user_decisions_overridden": False,
            "source_duration_seconds": round(source_duration, 6),
            "analyzed_duration_seconds": round(analyzed_duration, 6),
            "truncated": partial,
            "partial": partial,
            "needs_review_reason": "audio_analysis_capped_by_max_analysis_seconds" if partial else "",
        },
        "summary": {
            "windows_analyzed": len(candidates),
            "voiced_windows": sum(1 for item in candidates if item["features"]["voiced"]),
            "event_counts": event_counts,
            "recommendation_counts": recommendation_counts,
        },
        "candidates": candidates,
    }


def _pcm_samples(pcm: bytes) -> array:
    usable = pcm[: len(pcm) - (len(pcm) % 2)]
    values = array("h")
    values.frombytes(usable)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _features(samples: Sequence[int], vad_threshold_db: float) -> AudioFeatures:
    if not samples:
        return AudioFeatures(-120.0, -120.0, 0.0, 0.0, 0.0, False)
    normalized = [value / 32768.0 for value in samples]
    rms = math.sqrt(sum(value * value for value in normalized) / len(normalized))
    peak = max(abs(value) for value in normalized)
    rms_db = _db(rms)
    peak_db = _db(peak)
    crossings = sum(1 for left, right in zip(normalized, normalized[1:]) if (left < 0) != (right < 0))
    zcr = crossings / max(1, len(normalized) - 1)
    difference_energy = sum((right - left) ** 2 for left, right in zip(normalized, normalized[1:]))
    roughness = min(1.0, math.sqrt(difference_energy / max(1, len(normalized) - 1)) / 1.35)
    clipping = sum(1 for value in normalized if abs(value) >= 0.98) / len(normalized)
    return AudioFeatures(
        rms_db=round(rms_db, 6),
        peak_db=round(peak_db, 6),
        zero_crossing_rate=round(zcr, 6),
        roughness=round(roughness, 6),
        clipping_ratio=round(clipping, 6),
        voiced=rms_db >= vad_threshold_db and clipping < 0.02,
    )


def _event_candidates(features: AudioFeatures) -> list[dict[str, Any]]:
    if features.clipping_ratio >= 0.02 or features.peak_db >= -0.1:
        return [{"event": "invalid_noise", "confidence": 0.98, "evidence": ["clipping_or_overload"]}]
    if not features.voiced:
        return [{"event": "ambience", "confidence": 0.62, "evidence": ["below_vad_threshold"]}]
    speech = _clamp(
        0.35
        + (0.25 if 0.03 <= features.zero_crossing_rate <= 0.22 else 0.0)
        + (0.2 if 0.12 <= features.roughness <= 0.72 else 0.0)
        + (0.15 if -38.0 <= features.rms_db <= -10.0 else 0.0)
    )
    candidates = [{"event": "dialogue", "confidence": round(speech, 6), "evidence": ["vad", "speech_band"]}]
    if features.roughness >= 0.78 and features.zero_crossing_rate >= 0.18:
        candidates.append({"event": "matcha_whisking", "confidence": round(_clamp(0.58 + features.roughness * 0.35), 6), "evidence": ["sustained_high_roughness", "high_zero_crossing_rate"]})
    if features.roughness >= 0.6 and features.peak_db - features.rms_db >= 8.0:
        candidates.append({"event": "pouring", "confidence": round(_clamp(0.52 + features.roughness * 0.3), 6), "evidence": ["rough_transient"]})
    if features.roughness <= 0.24 and features.rms_db >= -42.0:
        candidates.append({"event": "transport", "confidence": round(_clamp(0.55 + (0.2 if features.zero_crossing_rate < 0.08 else 0.0)), 6), "evidence": ["low_roughness", "steady_energy"]})
    if features.roughness >= 0.48 and features.rms_db >= -34.0:
        candidates.append({"event": "grinding", "confidence": round(_clamp(0.5 + features.roughness * 0.35), 6), "evidence": ["mechanical_roughness", "elevated_energy"]})
    return sorted(candidates, key=lambda item: (-float(item["confidence"]), str(item["event"])))


def _stable_segment_identity(video_id: int, start: float, end: float, visual_segments: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    best: tuple[float, Mapping[str, Any]] | None = None
    for segment in visual_segments:
        segment_start = float(segment.get("start_seconds") or 0.0)
        segment_end = float(segment.get("end_seconds") or segment_start)
        overlap = max(0.0, min(end, segment_end) - max(start, segment_start))
        if overlap <= 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, segment)
    if best:
        stable = str(best[1].get("segment_uuid") or "").strip()
        if stable:
            return stable, "visual_segment_uuid"
        window_uuid = str(best[1].get("window_uuid") or "").strip()
        if window_uuid:
            return window_uuid, "visual_window_uuid_pending_publish"
    return str(uuid5(NAMESPACE_URL, f"video-vault-ai/audio-segment/v1/{video_id}/{start}/{end}")), "audio_window_uuid"


def _recommendation(event: str) -> str:
    if event == "invalid_noise":
        return "mute"
    if event in {"ambience", "transport"}:
        return "duck"
    return "keep"


def _no_audio_result(video_id: int, duration_seconds: float, source_fingerprint: Mapping[str, Any] | None, policy: Mapping[str, Any]) -> dict[str, Any]:
    source_duration = round(float(duration_seconds or 0.0), 6)
    return {
        "schema_version": AUDIO_PERCEPTION_VERSION,
        "status": "no_audio",
        "provider": "local",
        "model": AUDIO_FEATURE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": dict(source_fingerprint or {}),
        "timeline": {"timebase": "seconds", "start_seconds": 0.0, "duration_seconds": round(float(duration_seconds or 0.0), 6)},
        "policy": dict(policy),
        "audit": {
            "local_only": True,
            "transcription_requested": False,
            "cloud_audio_requested": False,
            "user_decisions_overridden": False,
            "source_duration_seconds": source_duration,
            "analyzed_duration_seconds": 0.0,
            "truncated": False,
            "partial": False,
            "needs_review_reason": "no_audio_track",
        },
        "summary": {"windows_analyzed": 0, "voiced_windows": 0, "event_counts": {event: 0 for event in EVENT_TYPES}, "recommendation_counts": {recommendation: 0 for recommendation in sorted(RECOMMENDATIONS)}},
        "candidates": [],
    }


def _is_no_audio_error(detail: str) -> bool:
    normalized = " ".join(str(detail or "").lower().split())
    return any(
        marker in normalized
        for marker in (
            "output file does not contain any stream",
            "output file #0 does not contain any stream",
            "matches no streams",
            "stream map",
        )
    ) and ("audio" in normalized or "stream" in normalized)


def _bounded_int(value: Any, lower: int, upper: int, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"audio {field} must be an integer") from exc
    if not lower <= number <= upper:
        raise ValueError(f"audio {field} must be between {lower} and {upper}")
    return number


def _bounded_float(value: Any, lower: float, upper: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"audio {field} must be numeric") from exc
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"audio {field} must be between {lower} and {upper}")
    return round(number, 6)


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-9))


def _clamp(value: float) -> float:
    return min(0.99, max(0.01, float(value)))


__all__ = [
    "AUDIO_FEATURE_VERSION",
    "AUDIO_PERCEPTION_VERSION",
    "AudioPerceptionError",
    "EVENT_TYPES",
    "analyze_audio_file",
    "analyze_pcm",
    "audio_policy",
]
