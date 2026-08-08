"""Durable, serialized accounting for project-scoped cloud-review attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4
import json
import sqlite3
from datetime import datetime, timezone

from .database import connect, init_db


class CloudReviewBudgetExceeded(RuntimeError):
    """No provider attempt can be admitted without exceeding a hard cap."""

    code = "cloud_review_budget_exceeded"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_cloud_review_ledger(db: Path) -> None:
    init_db(db)
    with connect(db) as con:
        con.execute(
            """create table if not exists cloud_review_budget_ledger (
                id integer primary key,
                reservation_uuid text not null unique,
                project_id integer not null,
                video_id integer not null,
                run_uuid text not null,
                window_uuid text not null,
                attempt_number integer not null,
                frame_count integer not null,
                estimated_cost_usd real not null,
                status text not null default 'reserved',
                created_at text not null,
                finalized_at text,
                error text default '',
                audit_json text default '{}'
            )"""
        )
        con.execute(
            "create index if not exists idx_cloud_review_ledger_scope "
            "on cloud_review_budget_ledger(project_id, run_uuid, video_id)"
        )


def _scope_clause(scope_run_uuids: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    values = tuple(sorted({str(value) for value in scope_run_uuids if str(value)}))
    if not values:
        return "1=0", ()
    return f"run_uuid in ({','.join('?' for _ in values)})", values


def _usage_from_rows(rows: Sequence[sqlite3.Row]) -> dict:
    usage = {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0, "by_clip": {}}
    for row in rows:
        calls = 1
        frames = int(row["frame_count"] or 0)
        cost = float(row["estimated_cost_usd"] or 0.0)
        clip_id = str(row["video_id"])
        usage["calls"] += calls
        usage["frames"] += frames
        usage["estimated_cost_usd"] = round(usage["estimated_cost_usd"] + cost, 6)
        clip = usage["by_clip"].setdefault(clip_id, {"calls": 0, "frames": 0, "estimated_cost_usd": 0.0})
        clip["calls"] += calls
        clip["frames"] += frames
        clip["estimated_cost_usd"] = round(clip["estimated_cost_usd"] + cost, 6)
    return usage


def _seed_baselines(
    con: sqlite3.Connection,
    project_id: int,
    baseline_by_run: Mapping[str, Mapping[str, Any] | None],
) -> None:
    """Import pre-ledger persisted audits once per current run.

    A baseline row is immutable.  Later attempts are separate rows, so a
    crash before publishing cannot erase or duplicate the already reserved
    spend.
    """

    for run_uuid, audit in baseline_by_run.items():
        if not isinstance(audit, Mapping):
            continue
        if bool(audit.get("ledger_backed")):
            continue
        real_row = con.execute(
            "select 1 from cloud_review_budget_ledger "
            "where project_id=? and run_uuid=? and status != 'baseline' limit 1",
            (int(project_id), str(run_uuid)),
        ).fetchone()
        if real_row:
            continue
        usage = audit.get("usage") if isinstance(audit.get("usage"), Mapping) else {}
        by_clip = usage.get("by_clip") if isinstance(usage, Mapping) else {}
        if not isinstance(by_clip, Mapping):
            continue
        for video_id, raw in by_clip.items():
            if not isinstance(raw, Mapping):
                continue
            calls = max(0, int(raw.get("calls") or 0))
            frames = max(0, int(raw.get("frames") or 0))
            cost = max(0.0, float(raw.get("estimated_cost_usd") or 0.0))
            for index in range(calls):
                reservation_uuid = f"baseline:{run_uuid}:{video_id}:{index}"
                frame_count = frames // calls if calls else 0
                if index < frames % calls:
                    frame_count += 1
                con.execute(
                    """insert or ignore into cloud_review_budget_ledger(
                        reservation_uuid, project_id, video_id, run_uuid, window_uuid,
                        attempt_number, frame_count, estimated_cost_usd, status, created_at, audit_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        reservation_uuid,
                        int(project_id),
                        int(video_id),
                        str(run_uuid),
                        "__persisted_audit__",
                        0,
                        frame_count,
                        round(cost / calls, 6) if calls else 0.0,
                        "baseline",
                        _now(),
                        json.dumps({"source": "persisted_cloud_review_audit"}, ensure_ascii=False),
                    ),
                )


def usage_for_scope(
    db: Path,
    project_id: int,
    scope_run_uuids: Sequence[str],
    baseline_by_run: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> dict:
    ensure_cloud_review_ledger(db)
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        _seed_baselines(con, int(project_id), baseline_by_run or {})
        clause, values = _scope_clause(scope_run_uuids)
        rows = con.execute(
            f"select video_id, frame_count, estimated_cost_usd from cloud_review_budget_ledger where project_id=? and {clause}",
            (int(project_id), *values),
        ).fetchall()
        con.commit()
    return _usage_from_rows(rows)


def reserve_attempt(
    db: Path,
    *,
    project_id: int,
    video_id: int,
    run_uuid: str,
    window_uuid: str,
    frame_count: int,
    estimated_cost_usd: float,
    policy: Mapping[str, Any],
    scope_run_uuids: Sequence[str],
    baseline_by_run: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> dict:
    """Atomically admit exactly one imminent provider attempt."""

    ensure_cloud_review_ledger(db)
    frame_count = max(0, int(frame_count))
    estimated_cost_usd = max(0.0, float(estimated_cost_usd))
    scope = tuple(sorted({str(value) for value in scope_run_uuids if str(value)}))
    reservation_uuid = str(uuid4())
    with connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        _seed_baselines(con, int(project_id), baseline_by_run or {})
        clause, values = _scope_clause(scope)
        rows = con.execute(
            f"select video_id, frame_count, estimated_cost_usd from cloud_review_budget_ledger where project_id=? and {clause}",
            (int(project_id), *values),
        ).fetchall()
        usage = _usage_from_rows(rows)
        clip_rows = [row for row in rows if int(row["video_id"]) == int(video_id)]
        clip_usage = _usage_from_rows(clip_rows)
        next_calls = usage["calls"] + 1
        next_frames = usage["frames"] + frame_count
        next_cost = usage["estimated_cost_usd"] + estimated_cost_usd
        next_clip_calls = clip_usage["calls"] + 1
        next_clip_frames = clip_usage["frames"] + frame_count
        next_clip_cost = clip_usage["estimated_cost_usd"] + estimated_cost_usd
        if (
            next_clip_calls > int(policy["max_calls_per_clip"])
            or next_clip_frames > int(policy["max_frames_per_clip"])
        ):
            con.rollback()
            raise CloudReviewBudgetExceeded("cloud review clip call/frame budget exceeded")
        if next_clip_cost > float(policy["max_estimated_cost_usd_per_clip"]):
            con.rollback()
            raise CloudReviewBudgetExceeded("cloud review clip cost budget exceeded")
        if (
            next_calls > int(policy["max_calls_per_project"])
            or next_frames > int(policy["max_frames_per_project"])
        ):
            con.rollback()
            raise CloudReviewBudgetExceeded("cloud review project call/frame budget exceeded")
        if next_cost > float(policy["max_estimated_cost_usd_per_project"]):
            con.rollback()
            raise CloudReviewBudgetExceeded("cloud review project cost budget exceeded")
        prior_attempts = con.execute(
            "select count(*) as count from cloud_review_budget_ledger where project_id=? and "
            "video_id=? and window_uuid=? and run_uuid=?",
            (int(project_id), int(video_id), str(window_uuid), str(run_uuid)),
        ).fetchone()
        attempt_number = int(prior_attempts["count"] or 0) + 1
        con.execute(
            """insert into cloud_review_budget_ledger(
                reservation_uuid, project_id, video_id, run_uuid, window_uuid,
                attempt_number, frame_count, estimated_cost_usd, status, created_at
            ) values(?,?,?,?,?,?,?,?,?,?)""",
            (
                reservation_uuid,
                int(project_id),
                int(video_id),
                str(run_uuid),
                str(window_uuid),
                attempt_number,
                frame_count,
                round(estimated_cost_usd, 6),
                "reserved",
                _now(),
            ),
        )
        con.commit()
    return {
        "reservation_uuid": reservation_uuid,
        "project_id": int(project_id),
        "video_id": int(video_id),
        "run_uuid": str(run_uuid),
        "window_uuid": str(window_uuid),
        "attempt_number": attempt_number,
        "frame_count": frame_count,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
    }


def finalize_attempt(db: Path, reservation_uuid: str, status: str, error: str = "") -> None:
    ensure_cloud_review_ledger(db)
    with connect(db) as con:
        con.execute(
            "update cloud_review_budget_ledger set status=?, finalized_at=?, error=? where reservation_uuid=? and status='reserved'",
            (str(status), _now(), str(error), str(reservation_uuid)),
        )


def ledger_rows_for_scope(db: Path, project_id: int, scope_run_uuids: Sequence[str]) -> list[dict]:
    ensure_cloud_review_ledger(db)
    clause, values = _scope_clause(scope_run_uuids)
    with connect(db) as con:
        rows = con.execute(
            f"select * from cloud_review_budget_ledger where project_id=? and {clause} order by id",
            (int(project_id), *values),
        ).fetchall()
    return [dict(row) for row in rows]
