from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import shutil
import sqlite3

from .database import _replace_segments_in_connection, connect, init_db

RUN_COLUMNS = {
    "run_uuid": "text",
    "project_id": "integer",
    "project_media_uuid": "text",
    "generation": "integer default 1",
    "requested_at": "text",
    "started_at": "text",
    "finished_at": "text",
    "published_at": "text",
    "error": "text",
    "input_snapshot_json": "text",
    "frame_manifest_json": "text",
    "staging_path": "text",
    "previous_success_run_uuid": "text",
    "base_revision": "integer",
    "provider_contract_json": "text default '{}'",
    "interrupted_at": "text",
    "published_revision": "integer",
}

PROJECT_MEDIA_RUN_COLUMNS = {
    "current_analysis_run_uuid": "text",
    "last_successful_analysis_run_uuid": "text",
    "analysis_generation": "integer default 0",
}

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "publishing"}
PERCEPTION_CONTRACT_VERSION = "perception-run-v2"


class PerceptionCancelled(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_perception_schema(db: Path) -> None:
    init_db(db)
    with connect(db) as con:
        _ensure_columns(con, "analysis_runs", RUN_COLUMNS)
        _ensure_columns(con, "project_videos", PROJECT_MEDIA_RUN_COLUMNS)
        con.execute(
            "create unique index if not exists idx_analysis_runs_uuid on analysis_runs(run_uuid)"
        )
        con.execute(
            "create index if not exists idx_analysis_runs_video_generation on analysis_runs(video_id, generation desc)"
        )
        con.execute(
            "create index if not exists idx_analysis_runs_project on analysis_runs(project_id, id desc)"
        )
        con.execute(
            """create unique index if not exists idx_analysis_runs_active_video
            on analysis_runs(video_id)
            where status in ('queued','running','publishing')"""
        )
        con.execute("update analysis_runs set status='succeeded' where status='done'")
        for row in con.execute(
            "select id from analysis_runs where run_uuid is null or run_uuid='' order by id"
        ).fetchall():
            con.execute(
                "update analysis_runs set run_uuid=?, requested_at=coalesce(requested_at, created_at), started_at=coalesce(started_at, created_at), finished_at=coalesce(finished_at, created_at), published_at=coalesce(published_at, created_at) where id=?",
                (str(uuid4()), int(row["id"])),
            )


def _ensure_columns(con, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in con.execute(f"pragma table_info({table})").fetchall()}
    for name, spec in columns.items():
        if name not in existing:
            con.execute(f"alter table {table} add column {name} {spec}")


def recover_interrupted_perception_runs(db: Path) -> int:
    ensure_perception_schema(db)
    with connect(db) as con:
        rows = con.execute(
            "select run_uuid from analysis_runs where status in ('queued','running','publishing')"
        ).fetchall()
        if not rows:
            return 0
        run_ids = [str(row["run_uuid"]) for row in rows]
        placeholders = ",".join("?" for _ in run_ids)
        con.execute(
            f"update analysis_runs set status='failed', finished_at=?, interrupted_at=?, error=coalesce(nullif(error,''), 'application restarted before completion') where run_uuid in ({placeholders})",
            (_now(), _now(), *run_ids),
        )
        con.execute(
            f"update project_videos set analysis_status='failed' where current_analysis_run_uuid in ({placeholders})",
            run_ids,
        )
        return len(run_ids)


def source_fingerprint(path: Path) -> dict:
    path = path.expanduser().resolve(strict=True)
    stat = path.stat()
    chunk_size = 1024 * 1024
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        first = handle.read(chunk_size)
        digest.update(first)
        if stat.st_size > chunk_size:
            handle.seek(max(0, stat.st_size - chunk_size))
            digest.update(handle.read(chunk_size))
    return {
        "path": str(path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sample_sha256": digest.hexdigest(),
    }


def extractor_snapshot(cfg: dict) -> dict:
    return {
        "extractor_version": 1,
        "frame_interval_seconds": float(cfg["frame_interval_seconds"]),
        "frame_height": int(cfg.get("frame_height", 720)),
        "ffmpeg_path": str(cfg.get("ffmpeg_path") or "ffmpeg"),
    }


def build_input_snapshot(video: dict, cfg: dict) -> dict:
    return {
        "source": source_fingerprint(Path(video["current_path"])),
        "extractor": extractor_snapshot(cfg),
        "duration_seconds": float(video.get("duration_seconds") or 0),
    }


def run_staging_dir(cfg: dict, run_uuid: str) -> Path:
    path = Path(cfg["library_root"]) / "05_index" / "perception_runs" / str(run_uuid)
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_perception_run(
    db: Path,
    cfg: dict,
    project_id: int,
    video: dict,
) -> dict:
    ensure_perception_schema(db)
    run_uuid = str(uuid4())
    created = _now()
    staging = run_staging_dir(cfg, run_uuid)
    video_id = int(video["id"])
    project_media_uuid = str(video.get("project_media_uuid") or "")
    input_error: OSError | None = None
    try:
        input_snapshot = build_input_snapshot(video, cfg)
    except OSError as exc:
        # Persist an auditable failed run even when the source disappears before
        # fingerprinting.  The original exception is re-raised after the row
        # and project run pointer have been committed.
        input_error = exc
        input_snapshot = {
            "source": {"path": str(Path(video["current_path"]).expanduser().resolve()), "missing": True},
            "extractor": extractor_snapshot(cfg),
            "duration_seconds": float(video.get("duration_seconds") or 0),
        }
    provider_contract = {
        "contract_version": PERCEPTION_CONTRACT_VERSION,
        "provider": str(cfg.get("ai", {}).get("provider", "mock")),
        "model": str(cfg.get("ai", {}).get("model", "")),
        "extractor": input_snapshot.get("extractor", {}),
    }
    with connect(db) as con:
        generation = int(
            con.execute(
                "select coalesce(max(generation), 0) + 1 as generation from analysis_runs where video_id=?",
                (video_id,),
            ).fetchone()["generation"]
        )
        previous = con.execute(
            "select run_uuid from analysis_runs where video_id=? and status='succeeded' order by generation desc, id desc limit 1",
            (video_id,),
        ).fetchone()
        previous_uuid = str(previous["run_uuid"]) if previous else ""
        try:
            con.execute(
                """insert into analysis_runs(
                    video_id, provider, model, status, raw_output_path, run_uuid, project_id,
                    project_media_uuid, generation, requested_at, started_at,
                    input_snapshot_json, staging_path, previous_success_run_uuid,
                    provider_contract_json, finished_at, error
                ) values(?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    str(cfg.get("ai", {}).get("provider", "mock")),
                    str(cfg.get("ai", {}).get("model", "")),
                    "failed" if input_error else "running",
                    run_uuid,
                    int(project_id),
                    project_media_uuid,
                    generation,
                    created,
                    created,
                    json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
                    str(staging),
                    previous_uuid,
                    json.dumps(provider_contract, ensure_ascii=False, sort_keys=True),
                    _now() if input_error else "",
                    str(input_error) if input_error else "",
                ),
            )
        except sqlite3.IntegrityError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"video {video_id} already has an active perception run") from exc
        con.execute(
            """update project_videos
            set current_analysis_run_uuid=?, analysis_generation=?, analysis_status=?
            where video_id=?""",
            (run_uuid, generation, "failed" if input_error else "analyzing", video_id),
        )
        con.execute(
            """update projects
            set status='needs_review', updated_at=current_timestamp
            where id in (select project_id from project_videos where video_id=?)""",
            (video_id,),
        )
    if input_error:
        raise input_error
    return analysis_run(db, run_uuid)


def analysis_run(db: Path, run_uuid: str) -> dict:
    ensure_perception_schema(db)
    with connect(db) as con:
        row = con.execute(
            "select * from analysis_runs where run_uuid=?",
            (str(run_uuid),),
        ).fetchone()
    if not row:
        raise ValueError(f"perception run not found: {run_uuid}")
    result = dict(row)
    for key in ("input_snapshot_json", "frame_manifest_json"):
        raw = result.pop(key, "") or ""
        result[key.removesuffix("_json")] = json.loads(raw) if raw else {}
    return result


def set_run_frame_manifest(db: Path, run_uuid: str, manifest: list[dict]) -> None:
    ensure_perception_schema(db)
    with connect(db) as con:
        con.execute(
            "update analysis_runs set frame_manifest_json=? where run_uuid=?",
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True), str(run_uuid)),
        )


def set_run_output_path(db: Path, run_uuid: str, path: Path) -> None:
    with connect(db) as con:
        con.execute(
            "update analysis_runs set raw_output_path=? where run_uuid=?",
            (str(path), str(run_uuid)),
        )


def expected_frame_count(video: dict, cfg: dict) -> int:
    duration = int(float(video.get("duration_seconds") or 0))
    interval = max(1, int(float(cfg["frame_interval_seconds"])))
    return len(range(0, max(duration, 1), interval))


def build_frame_manifest(paths: list[Path], cfg: dict) -> list[dict]:
    interval = float(cfg["frame_interval_seconds"])
    result = []
    for index, path in enumerate(sorted(paths)):
        stat = path.stat()
        result.append(
            {
                "frame_path": str(path.resolve()),
                "timestamp_seconds": round(index * interval, 6),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return result


def validate_run_inputs(run: dict, video: dict, cfg: dict, manifest: list[dict]) -> list[str]:
    errors: list[str] = []
    current_snapshot = build_input_snapshot(video, cfg)
    if run.get("input_snapshot") != current_snapshot:
        errors.append("source or extractor configuration changed after the run started")
    expected = expected_frame_count(video, cfg)
    if len(manifest) != expected:
        errors.append(f"frame manifest count mismatch: expected {expected}, got {len(manifest)}")
    timestamps = [round(float(row.get("timestamp_seconds") or 0), 6) for row in manifest]
    expected_timestamps = [
        round(index * float(cfg["frame_interval_seconds"]), 6)
        for index in range(expected)
    ]
    if timestamps != expected_timestamps:
        errors.append("frame manifest timestamps do not match the extractor configuration")
    for row in manifest:
        path = Path(str(row.get("frame_path") or ""))
        if not path.is_file():
            errors.append(f"frame missing: {path}")
            continue
        if int(row.get("size") or -1) != path.stat().st_size:
            errors.append(f"frame size changed: {path}")
    return errors


def capture_live_results(db: Path, video_id: int) -> dict:
    ensure_perception_schema(db)
    with connect(db) as con:
        migration_ids = [
            int(row["id"])
            for row in con.execute(
                "select id from segment_identity_migrations where video_id=? order by id",
                (int(video_id),),
            ).fetchall()
        ]
        video = con.execute("select status from videos where id=?", (int(video_id),)).fetchone()
        return {
            "video_id": int(video_id),
            "video_status": str(video["status"] or "") if video else "",
            "frames": [dict(row) for row in con.execute("select * from frames where video_id=? order by id", (int(video_id),)).fetchall()],
            "segments": [dict(row) for row in con.execute("select * from segments where video_id=? order by id", (int(video_id),)).fetchall()],
            "project_videos": [dict(row) for row in con.execute("select * from project_videos where video_id=? order by project_id", (int(video_id),)).fetchall()],
            "migration_ids": migration_ids,
        }


def publish_staged_results(
    db: Path,
    run_uuid: str,
    frame_results: list[dict],
    segment_results: list[dict],
) -> dict:
    ensure_perception_schema(db)
    run = analysis_run(db, run_uuid)
    if run.get("status") != "running":
        raise RuntimeError(f"run is not publishable: {run.get('status')}")
    video_id = int(run["video_id"])
    with connect(db) as con:
        _stage_run_results_in_connection(con, run_uuid, video_id, frame_results, segment_results)
        con.execute(
            "update analysis_runs set status='publishing' where run_uuid=?",
            (str(run_uuid),),
        )
        con.execute(
            "update project_videos set analysis_status='publishing' where video_id=? and current_analysis_run_uuid=?",
            (video_id, str(run_uuid)),
        )
        con.execute("delete from frames where video_id=?", (video_id,))
        for row in frame_results:
            tags = row.get("tags") or []
            tag_text = tags if isinstance(tags, str) else ",".join(str(tag) for tag in tags)
            con.execute(
                """insert into frames(
                    video_id, timestamp_seconds, frame_path, vision_summary, tags,
                    score_visual_quality, score_usefulness
                ) values(?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    float(row.get("timestamp_seconds") or 0),
                    str(row.get("frame_path") or ""),
                    str(row.get("summary") or row.get("vision_summary") or ""),
                    tag_text,
                    float(row.get("visual_quality_score") or row.get("score_visual_quality") or 0),
                    float(row.get("usefulness_score") or row.get("score_usefulness") or 0),
                ),
            )
        migration = _replace_segments_in_connection(con, video_id, segment_results)
        source_snapshot = run.get("input_snapshot", {}).get("source") if isinstance(run.get("input_snapshot"), dict) else None
        if source_snapshot:
            con.execute(
                "update project_videos set source_fingerprint_json=?, ownership_state='project_owned', migration_generation=coalesce(migration_generation, 0)+1 where video_id=? and current_analysis_run_uuid=?",
                (json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True), video_id, str(run_uuid)),
            )
        con.execute("update videos set status='analyzed' where id=?", (video_id,))
    return migration


def finalize_perception_run(db: Path, run_uuid: str) -> dict:
    ensure_perception_schema(db)
    run = analysis_run(db, run_uuid)
    if run.get("status") != "publishing":
        raise RuntimeError(f"run is not ready to finalize: {run.get('status')}")
    now = _now()
    with connect(db) as con:
        con.execute(
            "update analysis_runs set status='succeeded', finished_at=?, published_at=?, published_revision=(select project_revision from projects where id=analysis_runs.project_id), error='' where run_uuid=?",
            (now, now, str(run_uuid)),
        )
        con.execute(
            """update project_videos
            set last_successful_analysis_run_uuid=?, analysis_status='perceived', perceived_at=?
            where video_id=? and current_analysis_run_uuid=?""",
            (str(run_uuid), now, int(run["video_id"]), str(run_uuid)),
        )
    return analysis_run(db, run_uuid)


def mark_perception_run_terminal(db: Path, run_uuid: str, status: str, error: str = "") -> dict:
    if status not in {"failed", "cancelled", "interrupted"}:
        raise ValueError(status)
    ensure_perception_schema(db)
    run = analysis_run(db, run_uuid)
    with connect(db) as con:
        con.execute(
            "update analysis_runs set status=?, finished_at=?, interrupted_at=case when ?='interrupted' then ? else interrupted_at end, error=? where run_uuid=?",
            (status, _now(), status, _now() if status == "interrupted" else None, str(error), str(run_uuid)),
        )
        con.execute(
            "update project_videos set analysis_status=? where video_id=? and current_analysis_run_uuid=?",
            (status, int(run["video_id"]), str(run_uuid)),
        )
    return analysis_run(db, run_uuid)


def restore_live_results(
    db: Path,
    snapshot: dict,
    run_uuid: str,
    status: str,
    error: str,
) -> dict:
    ensure_perception_schema(db)
    run = analysis_run(db, run_uuid)
    video_id = int(snapshot["video_id"])
    with connect(db) as con:
        con.execute("delete from frames where video_id=?", (video_id,))
        _restore_rows(con, "frames", snapshot.get("frames", []))
        con.execute("delete from segments where video_id=?", (video_id,))
        _restore_rows(con, "segments", snapshot.get("segments", []))
        migration_ids = [int(value) for value in snapshot.get("migration_ids", [])]
        if migration_ids:
            placeholders = ",".join("?" for _ in migration_ids)
            con.execute(
                f"delete from segment_identity_migrations where video_id=? and id not in ({placeholders})",
                (video_id, *migration_ids),
            )
        else:
            con.execute(
                "delete from segment_identity_migrations where video_id=?",
                (video_id,),
            )
        con.execute(
            "update videos set status=? where id=?",
            (str(snapshot.get("video_status") or ""), video_id),
        )
        con.execute("delete from project_videos where video_id=?", (video_id,))
        _restore_rows(con, "project_videos", snapshot.get("project_videos", []))
        con.execute(
            """update project_videos
            set current_analysis_run_uuid=?, analysis_generation=?, analysis_status=?
            where video_id=?""",
            (str(run_uuid), int(run.get("generation") or 0), status, video_id),
        )
        con.execute(
            "update analysis_runs set status=?, finished_at=?, published_at=null, error=? where run_uuid=?",
            (status, _now(), str(error), str(run_uuid)),
        )
    return analysis_run(db, run_uuid)


def _restore_rows(con, table: str, rows: list[dict]) -> None:
    for row in rows:
        columns = list(row)
        con.execute(
            f"insert into {table}({', '.join(columns)}) values({', '.join('?' for _ in columns)})",
            tuple(row[column] for column in columns),
        )


def _stage_run_results_in_connection(
    con,
    run_uuid: str,
    video_id: int,
    frame_results: list[dict],
    segment_results: list[dict],
) -> None:
    """Keep a durable run-scoped copy before publishing live rows."""
    con.execute("delete from analysis_run_frames where run_uuid=?", (str(run_uuid),))
    con.execute("delete from analysis_run_segments where run_uuid=?", (str(run_uuid),))
    for ordinal, row in enumerate(frame_results):
        con.execute(
            "insert into analysis_run_frames(run_uuid, ordinal, video_id, timestamp_seconds, frame_path, payload_json) values(?,?,?,?,?,?)",
            (
                str(run_uuid), ordinal, int(video_id), float(row.get("timestamp_seconds") or 0),
                str(row.get("frame_path") or ""), json.dumps(row, ensure_ascii=False, sort_keys=True),
            ),
        )
    for ordinal, row in enumerate(segment_results):
        con.execute(
            "insert into analysis_run_segments(run_uuid, ordinal, video_id, segment_uuid, payload_json) values(?,?,?,?,?)",
            (
                str(run_uuid), ordinal, int(video_id), str(row.get("segment_uuid") or ""),
                json.dumps(row, ensure_ascii=False, sort_keys=True),
            ),
        )


def perception_states_for_project(db: Path, project_id: int) -> dict[int, dict]:
    ensure_perception_schema(db)
    with connect(db) as con:
        rows = con.execute(
            """select
                pv.video_id,
                pv.current_analysis_run_uuid,
                pv.last_successful_analysis_run_uuid,
                coalesce(pv.analysis_generation, 0) as analysis_generation,
                pv.analysis_status,
                current.status as current_status,
                current.error as current_error,
                current.generation as current_generation,
                current.requested_at as current_requested_at,
                current.finished_at as current_finished_at,
                current.input_snapshot_json as current_input_snapshot_json,
                success.generation as last_success_generation,
                success.finished_at as last_success_finished_at,
                exists(select 1 from segments s where s.video_id=pv.video_id) as has_segments
            from project_videos pv
            left join analysis_runs current on current.run_uuid=pv.current_analysis_run_uuid
            left join analysis_runs success on success.run_uuid=pv.last_successful_analysis_run_uuid
            where pv.project_id=?
            order by pv.sort_order, pv.video_id""",
            (int(project_id),),
        ).fetchall()
    result: dict[int, dict] = {}
    for row in rows:
        data = dict(row)
        current_uuid = str(data.get("current_analysis_run_uuid") or "")
        current_status = str(data.get("current_status") or data.get("analysis_status") or "")
        legacy_current = not current_uuid and current_status in {"perceived", "analyzed"} and bool(data.get("has_segments"))
        data["analysis_current"] = legacy_current or (
            bool(current_uuid)
            and current_status == "succeeded"
            and current_uuid == str(data.get("last_successful_analysis_run_uuid") or "")
        )
        raw_snapshot = str(data.pop("current_input_snapshot_json", "") or "")
        data["current_input_snapshot"] = json.loads(raw_snapshot) if raw_snapshot else {}
        data["stale_fallback_available"] = bool(
            data.get("last_successful_analysis_run_uuid")
            and not data["analysis_current"]
        )
        result[int(data["video_id"])] = data
    return result


def perception_jobs(db: Path, project_id: int, limit: int = 20) -> list[dict]:
    ensure_perception_schema(db)
    with connect(db) as con:
        rows = con.execute(
            """select distinct analysis_runs.* from analysis_runs
            where project_id=?
               or video_id in (select video_id from project_videos where project_id=?)
            order by id desc limit ?""",
            (int(project_id), int(project_id), int(limit)),
        ).fetchall()
    jobs = []
    for row in rows:
        item = dict(row)
        status = str(item.get("status") or "")
        ui_status = {"succeeded": "done", "cancelled": "stopped"}.get(status, status)
        jobs.append(
            {
                "kind": "內容感知",
                "status": ui_status,
                "message": _run_message(item),
                "done": 1 if status in TERMINAL_STATUSES else 0,
                "total": 1,
                "percent": 100 if status in TERMINAL_STATUSES else (95 if status == "publishing" else 10),
                "updated_at": float(item.get("id") or 0),
                "project_id": int(project_id),
                "run_uuid": str(item.get("run_uuid") or ""),
                "generation": int(item.get("generation") or 0),
                "video_id": int(item.get("video_id") or 0),
                "persistent": True,
            }
        )
    return jobs


def _run_message(row: dict) -> str:
    status = str(row.get("status") or "")
    generation = int(row.get("generation") or 0)
    if status == "running":
        return f"感知 generation {generation} 執行中"
    if status == "publishing":
        return f"感知 generation {generation} 發布中"
    if status == "succeeded":
        return f"感知 generation {generation} 已發布"
    if status == "cancelled":
        return f"感知 generation {generation} 已取消"
    return f"感知 generation {generation} 失敗：{row.get('error') or ''}".rstrip("：")


def snapshot_metadata_paths(paths: list[Path], backup_root: Path) -> list[dict]:
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, target in enumerate(paths):
        target = target.resolve(strict=False)
        backup = backup_root / f"item_{index:03d}"
        exists = target.exists()
        is_dir = target.is_dir() if exists else False
        if exists:
            if is_dir:
                shutil.copytree(target, backup)
            else:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        manifest.append(
            {
                "target": str(target),
                "backup": str(backup),
                "existed": exists,
                "is_dir": is_dir,
            }
        )
    return manifest


def restore_metadata_paths(manifest: list[dict]) -> None:
    for item in reversed(manifest):
        target = Path(item["target"])
        backup = Path(item["backup"])
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if not item.get("existed"):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.get("is_dir"):
            shutil.copytree(backup, target)
        else:
            shutil.copy2(backup, target)
