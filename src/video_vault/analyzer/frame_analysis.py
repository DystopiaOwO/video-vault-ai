from __future__ import annotations

from pathlib import Path
import hashlib

TAGS = ["coffee", "matcha", "roasting", "travel", "food", "landscape", "closeup", "hands", "steam", "dripping"]


def cache_key(frame: Path, provider: str, model: str) -> str:
    digest = hashlib.sha256(frame.read_bytes()).hexdigest()[:24]
    return f"{provider}_{model}_{digest}".replace(":", "_").replace("/", "_")


def merge_frames_to_segments(frames: list[dict], interval: float) -> list[dict]:
    useful = [f for f in frames if f["usefulness_score"] >= 0.45]
    segments = []
    current = []
    for frame in useful:
        if current and frame["timestamp_seconds"] - current[-1]["timestamp_seconds"] > interval * 1.5:
            segments.append(_segment(current, interval))
            current = []
        current.append(frame)
    if current:
        segments.append(_segment(current, interval))
    return segments


def _segment(group: list[dict], interval: float) -> dict:
    tags = sorted({tag for frame in group for tag in frame["tags"]})
    score = sum(f["usefulness_score"] for f in group) / len(group)
    use = _suggested_use(tags, score)
    return {
        "start_seconds": max(0, group[0]["timestamp_seconds"] - interval / 2),
        "end_seconds": group[-1]["timestamp_seconds"] + interval / 2,
        "segment_type": "shorts" if use == "Shorts" else "b_roll",
        "title": f"{use} candidate",
        "reason": "; ".join(f["summary"] for f in group[:2]),
        "tags": tags,
        "score": round(score, 2),
        "suggested_use": use,
    }


def _suggested_use(tags: list[str], score: float) -> str:
    if score >= 0.75 or {"steam", "dripping", "hands"} & set(tags):
        return "Shorts"
    if "closeup" in tags:
        return "Product closeup"
    return "B-roll"
