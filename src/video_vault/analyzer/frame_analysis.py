from __future__ import annotations

from pathlib import Path
import hashlib

TAGS = ["coffee", "matcha", "roasting", "travel", "food", "landscape", "closeup", "hands", "steam", "dripping"]
PROMPT_VERSION = "perception_zh_tw_v2"


def cache_key(frame: Path, provider: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    digest = hashlib.sha256(frame.read_bytes()).hexdigest()[:24]
    return f"{provider}_{model}_{prompt_version}_{digest}".replace(":", "_").replace("/", "_")


def merge_frames_to_segments(
    frames: list[dict],
    interval: float,
    duration_seconds: float | None = None,
) -> list[dict]:
    ordered = sorted(frames, key=lambda frame: float(frame["timestamp_seconds"]))
    useful = [f for f in ordered if f["usefulness_score"] >= 0.45]
    segments = []
    current = []
    for frame in useful:
        if current and frame["timestamp_seconds"] - current[-1]["timestamp_seconds"] > interval * 1.5:
            segments.append(_segment(current, ordered, interval, duration_seconds))
            current = []
        current.append(frame)
    if current:
        segments.append(_segment(current, ordered, interval, duration_seconds))
    return segments


def _segment(
    group: list[dict],
    all_frames: list[dict],
    interval: float,
    duration_seconds: float | None,
) -> dict:
    tags = sorted({tag for frame in group for tag in frame["tags"]})
    score = sum(f["usefulness_score"] for f in group) / len(group)
    use = _suggested_use(tags, score)
    first_timestamp = float(group[0]["timestamp_seconds"])
    last_timestamp = float(group[-1]["timestamp_seconds"])
    previous = next(
        (
            float(frame["timestamp_seconds"])
            for frame in reversed(all_frames)
            if float(frame["timestamp_seconds"]) < first_timestamp
        ),
        None,
    )
    following = next(
        (
            float(frame["timestamp_seconds"])
            for frame in all_frames
            if float(frame["timestamp_seconds"]) > last_timestamp
        ),
        None,
    )
    start_seconds = (
        (previous + first_timestamp) / 2
        if previous is not None
        else max(0.0, first_timestamp - interval / 2)
    )
    end_seconds = (
        (last_timestamp + following) / 2
        if following is not None
        else last_timestamp + interval / 2
    )
    if duration_seconds is not None:
        end_seconds = min(end_seconds, max(0.0, float(duration_seconds)))
    return {
        "start_seconds": round(max(0.0, start_seconds), 6),
        "end_seconds": round(max(0.0, end_seconds), 6),
        "segment_type": "shorts" if use in {"Shorts", "短影音"} else "b_roll",
        "title": f"{use}候選片段",
        "reason": "; ".join(f["summary"] for f in group[:2]),
        "tags": tags,
        "score": round(score, 2),
        "suggested_use": use,
    }


def _suggested_use(tags: list[str], score: float) -> str:
    if score >= 0.75 or {"steam", "dripping", "hands"} & set(tags):
        return "短影音"
    if "closeup" in tags:
        return "產品特寫"
    return "補畫面"


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
