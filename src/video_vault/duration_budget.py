"""Deterministic target-duration selection for project story plans."""

from __future__ import annotations

from typing import Any, Mapping


DURATION_BUDGET_VERSION = "duration-budget-v1"


def apply_duration_budget(
    groups: list[dict[str, Any]],
    target_duration_seconds: float,
    *,
    locked_segments: Mapping[str, Mapping[str, Any]] | None = None,
    tolerance_seconds: float | None = None,
) -> dict[str, Any]:
    """Select clips without silently dropping locked decisions.

    The selection order is stable: group order, score descending, then clip ID,
    start time and stable segment ID. A group gets one representative before
    spare budget is distributed, so a target does not erase an entire chapter.
    """
    target = max(0.0, float(target_duration_seconds or 0))
    tolerance = max(0.5, float(tolerance_seconds if tolerance_seconds is not None else max(1.0, target * 0.05)))
    locked_segments = locked_segments or {}
    entries: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups, 1):
        group["order"] = int(group.get("order") or group_index)
        for segment in group.get("segments", []) or []:
            segment_id = str(segment.get("segment_id") or "")
            start = float(segment.get("start_seconds") or 0)
            end = float(segment.get("end_seconds") or start)
            speed = max(0.25, float(segment.get("speed") or 1.0))
            duration = max(0.1, (end - start) / speed)
            state = locked_segments.get(segment_id) or {}
            is_locked = bool(state.get("locked") or segment.get("locked"))
            manually_excluded = state.get("included") is False
            segment["estimated_output_seconds"] = round(duration, 3)
            segment["include"] = not manually_excluded
            segment["selection_reason"] = "locked_include" if is_locked and not manually_excluded else "candidate"
            entries.append({
                "group": group,
                "segment": segment,
                "group_index": group_index,
                "segment_id": segment_id,
                "duration": duration,
                "score": float(segment.get("score") or 0),
                "locked": is_locked,
                "excluded": manually_excluded,
            })

    if target <= 0:
        for entry in entries:
            entry["segment"]["include"] = not entry["excluded"]
            if entry["excluded"]:
                entry["segment"]["selection_reason"] = "locked_exclude"
        selected = [entry for entry in entries if entry["segment"]["include"]]
        return _summary(groups, selected, entries, target, tolerance, conflict=False)

    selected: list[dict[str, Any]] = [entry for entry in entries if entry["locked"] and not entry["excluded"]]
    selected_keys = {id(entry["segment"]) for entry in selected}
    for entry in entries:
        if id(entry["segment"]) not in selected_keys:
            entry["segment"]["include"] = False
            entry["segment"]["selection_reason"] = "over_target_budget"

    locked_duration = sum(entry["duration"] for entry in selected)
    conflict = locked_duration > target + tolerance

    # First cover every group that has an available candidate. Locked choices
    # already count as coverage; the sort keys make this deterministic.
    for group in sorted(groups, key=lambda item: (int(item.get("order") or 999), str(item.get("label") or ""))):
        candidates = [
            entry for entry in entries
            if entry["group"] is group and not entry["excluded"] and id(entry["segment"]) not in selected_keys
        ]
        if any(entry["group"] is group for entry in selected):
            continue
        candidate = _ranked(candidates)[0] if candidates else None
        if candidate is not None:
            selected.append(candidate)
            selected_keys.add(id(candidate["segment"]))
            candidate["segment"]["include"] = True
            candidate["segment"]["selection_reason"] = "group_coverage"

    # Fill remaining budget with the strongest deterministic candidates.
    for entry in _ranked([item for item in entries if id(item["segment"]) not in selected_keys and not item["excluded"]]):
        current = sum(item["duration"] for item in selected)
        if current + entry["duration"] <= target + tolerance:
            selected.append(entry)
            selected_keys.add(id(entry["segment"]))
            entry["segment"]["include"] = True
            entry["segment"]["selection_reason"] = "score_within_budget"

    for entry in entries:
        if entry["excluded"]:
            entry["segment"]["include"] = False
            entry["segment"]["selection_reason"] = "locked_exclude"
    return _summary(groups, selected, entries, target, tolerance, conflict)


def _ranked(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda item: (
            -float(item["score"]),
            int(item["group_index"]),
            str(item["segment"].get("clip_id") or ""),
            float(item["segment"].get("start_seconds") or 0),
            str(item["segment_id"]),
        ),
    )


def _summary(groups, selected, entries, target, tolerance, conflict):
    selected_ids = {str(item["segment_id"]) for item in selected}
    group_rows = []
    for index, group in enumerate(groups, 1):
        group_entries = [item for item in entries if item["group"] is group]
        actual = sum(item["duration"] for item in group_entries if str(item["segment_id"]) in selected_ids)
        available = sum(item["duration"] for item in group_entries if not item["excluded"])
        budget = target * available / max(0.001, sum(item["duration"] for item in entries if not item["excluded"])) if target else available
        group_rows.append({
            "group_id": str(group.get("label") or f"group_{index:03d}"),
            "order": int(group.get("order") or index),
            "budget_seconds": round(budget, 3),
            "actual_seconds": round(actual, 3),
            "covered": bool(actual > 0),
        })
    omitted = [
        {"segment_id": str(item["segment_id"]), "reason": str(item["segment"].get("selection_reason") or "omitted")}
        for item in entries if str(item["segment_id"]) not in selected_ids
    ]
    estimated = sum(item["duration"] for item in selected)
    return {
        "contract_version": DURATION_BUDGET_VERSION,
        "target_seconds": round(target, 3),
        "estimated_seconds": round(estimated, 3),
        "tolerance_seconds": round(tolerance, 3),
        "within_tolerance": target <= 0 or abs(estimated - target) <= tolerance,
        "locked_duration_conflict": bool(conflict),
        "groups": group_rows,
        "omitted_segments": omitted,
    }


__all__ = ["DURATION_BUDGET_VERSION", "apply_duration_budget"]
