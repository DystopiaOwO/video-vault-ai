from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import json
import math
import re
import subprocess


SAMPLING_POLICY_NAME = "adaptive-balanced"
SAMPLING_POLICY_VERSION = 1
_FRAME_LINE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_SCORE_LINE = re.compile(r"lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)")


class SamplingError(RuntimeError):
    pass


@dataclass(frozen=True)
class SamplingPolicy:
    name: str
    version: int
    mode: str
    preset: str
    baseline_interval_seconds: float
    prescan_interval_seconds: float
    dense_interval_seconds: float
    scene_threshold: float
    motion_threshold: float
    min_interval_seconds: float
    max_frames_per_clip: int
    max_frames_per_minute: int
    visual_dedupe_threshold: float
    frame_height: int
    migrated_from_fixed_interval: bool


def resolved_sampling_policy(cfg: dict, override: dict | None = None) -> dict:
    sampling = dict(cfg.get("sampling") or {})
    override = dict(override or {})
    legacy_interval = _positive_float(cfg.get("frame_interval_seconds"), 5.0)
    default_mode = "fixed" if "sampling" not in cfg else "adaptive"
    mode = str(override.get("mode") or sampling.get("mode") or default_mode).lower()
    if mode not in {"fixed", "adaptive"}:
        raise SamplingError(f"unsupported sampling mode: {mode}")
    preset = str(override.get("preset") or sampling.get("preset") or "balanced").lower()
    if preset not in {"balanced", "dense"}:
        raise SamplingError(f"unsupported sampling preset: {preset}")
    dense = preset == "dense"
    baseline = _positive_float(
        override.get("baseline_interval_seconds"),
        _positive_float(sampling.get("baseline_interval_seconds"), 3.0 if dense else legacy_interval),
    )
    policy = SamplingPolicy(
        name=str(sampling.get("policy_name") or SAMPLING_POLICY_NAME),
        version=int(sampling.get("policy_version") or SAMPLING_POLICY_VERSION),
        mode=mode,
        preset=preset,
        baseline_interval_seconds=baseline,
        prescan_interval_seconds=_positive_float(
            sampling.get("prescan_interval_seconds"), 0.5
        ),
        dense_interval_seconds=_positive_float(
            sampling.get("dense_interval_seconds"), 0.5 if dense else 1.0
        ),
        scene_threshold=_bounded_float(sampling.get("scene_threshold"), 0.32, 0.0, 1.0),
        motion_threshold=_bounded_float(sampling.get("motion_threshold"), 0.06, 0.0, 1.0),
        min_interval_seconds=_positive_float(
            sampling.get("min_interval_seconds"), 0.25
        ),
        max_frames_per_clip=max(
            1, int(override.get("max_frames_per_clip") or sampling.get("max_frames_per_clip") or 180)
        ),
        max_frames_per_minute=max(
            1, int(sampling.get("max_frames_per_minute") or 30)
        ),
        visual_dedupe_threshold=_bounded_float(
            sampling.get("visual_dedupe_threshold"), 0.985, 0.0, 1.0
        ),
        frame_height=max(16, int(cfg.get("frame_height", 720))),
        migrated_from_fixed_interval=bool(
            sampling.get("migrated_from_fixed_interval")
        )
        or "sampling" not in cfg,
    )
    if policy.motion_threshold >= policy.scene_threshold:
        raise SamplingError("motion_threshold must be lower than scene_threshold")
    return asdict(policy)


def resolved_ai_model(cfg: dict) -> str:
    ai = cfg.get("ai") or {}
    provider = str(ai.get("provider") or "mock")
    if provider in {"local", "cloud"}:
        return str((ai.get(provider) or {}).get("model") or "")
    return str((ai.get("mock") or {}).get("model") or "rules")


def estimate_sampling_count(duration_seconds: float, policy: dict) -> int:
    duration = max(0.0, float(duration_seconds or 0))
    interval = _positive_float(policy.get("baseline_interval_seconds"), 5.0)
    baseline = len(
        _fixed_timestamp_range(duration, interval)
        if policy.get("mode") == "fixed"
        else _timestamp_range(duration, interval)
    )
    boundary = 1 if duration <= 0.001 else 2
    estimate = baseline if policy.get("mode") == "fixed" else baseline + boundary
    return min(_sampling_cap(duration, policy), max(1, estimate))


def build_sampling_plan(
    video: Path,
    duration_seconds: float,
    cfg: dict,
    override: dict | None = None,
) -> dict:
    policy = resolved_sampling_policy(cfg, override)
    duration = max(0.0, float(duration_seconds or 0))
    if policy["mode"] == "fixed":
        candidates = [
            {"timestamp_seconds": timestamp, "reasons": ["baseline"], "activity_score": 0.0}
            for timestamp in _fixed_timestamp_range(
                duration, policy["baseline_interval_seconds"]
            )
        ]
        samples = _apply_cap(candidates, _sampling_cap(duration, policy))
        return _plan_payload(policy, duration, samples, {"baseline": len(candidates), "scene": 0, "motion": 0, "boundary": 0})

    candidates: list[dict] = []
    counts = {"baseline": 0, "scene": 0, "motion": 0, "boundary": 0}
    for timestamp in _timestamp_range(duration, policy["baseline_interval_seconds"]):
        _candidate(candidates, counts, timestamp, "baseline", 0.0, duration)
    _candidate(candidates, counts, 0.0, "boundary", 0.0, duration)
    if duration > 0.001:
        _candidate(candidates, counts, max(0.0, duration - 0.05), "boundary", 0.0, duration)

    for timestamp, score in _scan_activity(video, cfg, policy):
        if timestamp < 0 or timestamp > duration + 0.01:
            continue
        if score >= float(policy["scene_threshold"]):
            guard = max(float(policy["min_interval_seconds"]) * 1.5, 0.35)
            _candidate(candidates, counts, timestamp - guard, "scene", score, duration)
            _candidate(candidates, counts, timestamp + guard, "scene", score, duration)
        elif score >= float(policy["motion_threshold"]):
            dense = float(policy["dense_interval_seconds"])
            _candidate(candidates, counts, timestamp - dense / 2, "motion", score, duration)
            _candidate(candidates, counts, timestamp, "motion", score, duration)
            _candidate(candidates, counts, timestamp + dense / 2, "motion", score, duration)

    samples = _temporal_dedupe(candidates, float(policy["min_interval_seconds"]))
    samples = _apply_cap(samples, _sampling_cap(duration, policy))
    return _plan_payload(policy, duration, samples, counts)


def dedupe_visual_samples(
    paths: list[Path],
    samples: list[dict],
    cfg: dict,
    policy: dict,
) -> tuple[list[Path], list[dict], dict]:
    threshold = float(policy.get("visual_dedupe_threshold") or 0)
    if threshold <= 0 or len(paths) < 2:
        return paths, samples, {"status": "disabled", "removed": 0}
    signatures: list[bytes] = []
    try:
        for path in paths:
            signatures.append(_visual_signature(path, cfg))
    except (OSError, subprocess.SubprocessError, SamplingError):
        return paths, samples, {"status": "unavailable", "removed": 0}

    kept_paths: list[Path] = []
    kept_samples: list[dict] = []
    kept_signatures: list[bytes] = []
    removed = 0
    for path, sample, signature in zip(paths, samples, signatures, strict=True):
        reasons = set(sample.get("reasons") or [])
        # Scene guards are structural evidence: even a visually similar frame
        # immediately before a cut must remain paired with its after-cut frame.
        preserve = bool({"boundary", "scene"} & reasons)
        duplicate_index = None
        if (
            not preserve
            and kept_signatures
            and _signature_similarity(signature, kept_signatures[-1]) >= threshold
        ):
            duplicate_index = len(kept_signatures) - 1
        if duplicate_index is None:
            kept_paths.append(path)
            kept_samples.append(dict(sample))
            kept_signatures.append(signature)
            continue
        removed += 1
        merged = set(kept_samples[duplicate_index].get("reasons") or [])
        merged.update(reasons)
        kept_samples[duplicate_index]["reasons"] = sorted(merged)
        kept_samples[duplicate_index]["visually_deduped_count"] = int(
            kept_samples[duplicate_index].get("visually_deduped_count") or 0
        ) + 1
        path.unlink(missing_ok=True)
    return kept_paths, kept_samples, {"status": "applied", "removed": removed}


def sampling_contract_hash(source: dict, policy: dict, samples: list[dict]) -> str:
    payload = {
        "source": {
            key: source[key]
            for key in ("size", "sample_sha256", "sha256", "content_sha256")
            if key in source
        },
        "policy": policy,
        "samples": [
            {
                "timestamp_seconds": round(float(sample["timestamp_seconds"]), 6),
                "reasons": sorted(str(reason) for reason in sample.get("reasons") or []),
            }
            for sample in samples
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _scan_activity(video: Path, cfg: dict, policy: dict) -> list[tuple[float, float]]:
    fps = 1.0 / float(policy["prescan_interval_seconds"])
    command = [
        str(cfg.get("ffmpeg_path") or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video),
        "-an",
        "-vf",
        f"fps={fps:.6f},scale=160:-2,select='gte(scene,0)',metadata=print",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error")[-800:]
        raise SamplingError(f"adaptive sampling prescan failed: {detail}")
    rows: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (result.stderr or "").splitlines():
        frame = _FRAME_LINE.search(line)
        if frame:
            pending = float(frame.group(1))
            continue
        score = _SCORE_LINE.search(line)
        if score and pending is not None:
            rows.append((pending, float(score.group(1))))
            pending = None
    if not rows:
        raise SamplingError("adaptive sampling prescan returned no video frames")
    return rows


def _visual_signature(path: Path, cfg: dict) -> bytes:
    command = [
        str(cfg.get("ffmpeg_path") or "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=16:16,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(command, check=True, capture_output=True)
    if len(result.stdout) != 256:
        raise SamplingError("visual signature extraction returned incomplete data")
    return result.stdout


def _signature_similarity(first: bytes, second: bytes) -> float:
    if len(first) != len(second) or not first:
        return 0.0
    difference = sum(abs(a - b) for a, b in zip(first, second, strict=True))
    return max(0.0, 1.0 - difference / (255.0 * len(first)))


def _candidate(
    candidates: list[dict],
    counts: dict[str, int],
    timestamp: float,
    reason: str,
    score: float,
    duration: float,
) -> None:
    end_guard = min(0.25, max(0.05, duration * 0.05))
    timestamp = min(max(0.0, float(timestamp)), max(0.0, duration - end_guard))
    candidates.append(
        {
            "timestamp_seconds": round(timestamp, 6),
            "reasons": [reason],
            "activity_score": round(float(score), 6),
        }
    )
    counts[reason] += 1


def _timestamp_range(duration: float, interval: float) -> list[float]:
    if duration <= 0.001:
        return [0.0]
    count = max(1, int(math.ceil(duration / interval)))
    return [round(index * interval, 6) for index in range(count) if index * interval < duration]


def _fixed_timestamp_range(duration: float, interval: float) -> list[float]:
    # Preserve the original fixed extractor's integer-duration boundary for
    # migrated configs while still allowing explicit fractional intervals.
    limit = max(int(duration), 1)
    count = max(1, int(math.ceil(limit / interval)))
    return [
        round(index * interval, 6)
        for index in range(count)
        if index * interval < limit
    ]


def _temporal_dedupe(candidates: list[dict], min_interval: float) -> list[dict]:
    result: list[dict] = []
    for candidate in sorted(candidates, key=lambda row: float(row["timestamp_seconds"])):
        if result and float(candidate["timestamp_seconds"]) - float(result[-1]["timestamp_seconds"]) < min_interval:
            previous = result[-1]
            previous["reasons"] = sorted(set(previous["reasons"]) | set(candidate["reasons"]))
            previous["activity_score"] = max(
                float(previous.get("activity_score") or 0),
                float(candidate.get("activity_score") or 0),
            )
            if "boundary" in candidate["reasons"]:
                previous["timestamp_seconds"] = candidate["timestamp_seconds"]
            continue
        result.append(dict(candidate))
    return result


def _apply_cap(samples: list[dict], cap: int) -> list[dict]:
    if len(samples) <= cap:
        return samples
    priority = {"boundary": 0, "scene": 1, "motion": 2, "baseline": 3}
    selected = sorted(
        samples,
        key=lambda row: (
            min(priority.get(reason, 9) for reason in row.get("reasons") or ["baseline"]),
            -float(row.get("activity_score") or 0),
            float(row["timestamp_seconds"]),
        ),
    )[:cap]
    return sorted(selected, key=lambda row: float(row["timestamp_seconds"]))


def _sampling_cap(duration: float, policy: dict) -> int:
    minute_windows = max(1, int(math.ceil(max(duration, 0.001) / 60.0)))
    return min(
        int(policy["max_frames_per_clip"]),
        int(policy["max_frames_per_minute"]) * minute_windows,
    )


def _plan_payload(
    policy: dict,
    duration: float,
    samples: list[dict],
    counts: dict[str, int],
) -> dict:
    return {
        "schema_version": 1,
        "policy": policy,
        "duration_seconds": round(duration, 6),
        "candidate_counts": counts,
        "pre_dedupe_candidate_count": sum(counts.values()),
        "estimated_vision_calls": len(samples),
        "actual_vision_calls": 0,
        "visual_dedupe": {"status": "pending", "removed": 0},
        "samples": samples,
    }


def _positive_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return result if result > 0 else default


def _bounded_float(value: object, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


__all__ = [
    "SAMPLING_POLICY_NAME",
    "SAMPLING_POLICY_VERSION",
    "SamplingError",
    "build_sampling_plan",
    "dedupe_visual_samples",
    "estimate_sampling_count",
    "resolved_ai_model",
    "resolved_sampling_policy",
    "sampling_contract_hash",
]
