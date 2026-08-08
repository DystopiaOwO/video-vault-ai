from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
import json
import re
import sqlite3

SCHEMA = """
create table if not exists videos (
  id integer primary key,
  original_path text unique,
  current_path text,
  filename text,
  category text,
  created_at text,
  imported_at text default current_timestamp,
  duration_seconds real default 0,
  width integer default 0,
  height integer default 0,
  fps real default 0,
  codec text default '',
  file_size integer default 0,
  proxy_path text,
  user_summary text default '',
  user_summary_updated_at text,
  status text default 'new'
);
create table if not exists frames (
  id integer primary key,
  video_id integer,
  timestamp_seconds real,
  frame_path text,
  vision_summary text,
  tags text,
  score_visual_quality real,
  score_usefulness real,
  window_uuid text,
  window_confidence real default 0
);
create table if not exists segments (
  id integer primary key,
  segment_uuid text,
  revision integer default 1,
  video_id integer,
  start_seconds real,
  end_seconds real,
  segment_type text,
  title text,
  reason text,
  tags text,
  score real,
  suggested_use text,
  window_uuid text,
  action text default '',
  shot_role text default '',
  technical_quality_json text default '{}',
  duplicate_group text default '',
  natural_audio_recommendation text default 'unknown',
  confidence real default 0
);
create table if not exists analysis_runs (
  id integer primary key,
  video_id integer,
  provider text,
  model text,
  created_at text default current_timestamp,
  status text,
  raw_output_path text,
  sampling_manifest_json text default '{}',
  window_manifest_json text default '[]',
  window_results_json text default '[]',
  window_validation_json text default '{}'
);
create table if not exists analysis_run_frames (
  run_uuid text not null,
  ordinal integer not null,
  video_id integer not null,
  timestamp_seconds real not null,
  frame_path text not null,
  payload_json text not null,
  primary key(run_uuid, ordinal)
);
create table if not exists analysis_run_segments (
  run_uuid text not null,
  ordinal integer not null,
  video_id integer not null,
  segment_uuid text,
  payload_json text not null,
  primary key(run_uuid, ordinal)
);
create table if not exists segment_identity_migrations (
  id integer primary key,
  video_id integer not null,
  created_at text default current_timestamp,
  report_json text not null
);
create table if not exists bgm_tracks (
  id integer primary key,
  title text,
  artist text,
  file_path text unique,
  source_url text,
  license_name text,
  license_url text,
  attribution_required integer default 0,
  attribution_text text,
  attribution_status text default 'unknown',
  license_status text default 'unverified',
  license_verified_at text,
  license_source_url text,
  verification_source text,
  verification_provenance text,
  mood text,
  duration_seconds real default 0,
  added_at text default current_timestamp
);
create table if not exists projects (
  id integer primary key,
  name text not null,
  kind text default 'auto',
  category text default 'unknown',
  content_type text default 'diary_montage',
  platform text default 'YouTube',
  target_duration_seconds real default 0,
  status text default 'draft',
  project_revision integer not null default 1,
  current_story_generation_uuid text default '',
  last_successful_story_generation_uuid text default '',
  created_at text default current_timestamp,
  updated_at text default current_timestamp
);
create table if not exists project_videos (
  project_id integer not null,
  video_id integer not null,
  project_media_uuid text,
  display_name text,
  category_override text,
  summary_override text,
  user_summary text default '',
  user_summary_updated_at text,
  summary_migration_state text default 'none',
  analysis_status text,
  perception_revision integer default 0,
  perceived_at text,
  source_fingerprint_json text default '{}',
  ownership_state text default 'project_owned',
  migration_generation integer default 0,
  sort_order integer default 0,
  primary key(project_id, video_id)
);
create table if not exists project_bgm (
  project_id integer not null,
  bgm_id integer not null,
  primary key(project_id, bgm_id)
);
create table if not exists story_generations (
  id integer primary key,
  story_generation_uuid text unique not null,
  project_id integer not null,
  generation integer not null,
  status text not null,
  base_project_revision integer not null,
  input_hash text not null,
  input_snapshot_json text not null,
  provider text not null,
  model text not null,
  prompt_version text not null,
  schema_version integer not null,
  creator_profile_version integer not null,
  project_story_profile_version integer not null,
  raw_response_json text default '{}',
  normalized_response_json text default '{}',
  review_state_json text default '{}',
  validation_json text default '{}',
  created_at text default current_timestamp,
  finished_at text,
  published_revision integer,
  previous_successful_generation_uuid text default '',
  error text default ''
);
create index if not exists idx_story_generations_project on story_generations(project_id, generation desc, id desc);
"""

IDENTITY_NAMESPACE = uuid5(NAMESPACE_URL, "video-vault-ai/stable-identities/v1")


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db(db: Path) -> None:
    with connect(db) as con:
        con.executescript(SCHEMA)
        _ensure_columns(
            con,
            "projects",
            {
                "category": "text default 'unknown'",
                "content_type": "text default 'diary_montage'",
                "platform": "text default 'YouTube'",
                "target_duration_seconds": "real default 0",
                "project_revision": "integer not null default 1",
                "current_story_generation_uuid": "text default ''",
                "last_successful_story_generation_uuid": "text default ''",
            },
        )
        con.execute("update projects set project_revision=1 where project_revision is null or project_revision < 1")
        _ensure_columns(
            con,
            "videos",
            {
                "user_summary": "text default ''",
                "user_summary_updated_at": "text",
            },
        )
        _ensure_columns(
            con,
            "segments",
            {
                "segment_uuid": "text",
                "revision": "integer default 1",
                "window_uuid": "text",
                "action": "text default ''",
                "shot_role": "text default ''",
                "technical_quality_json": "text default '{}'",
                "duplicate_group": "text default ''",
                "natural_audio_recommendation": "text default 'unknown'",
                "confidence": "real default 0",
            },
        )
        _ensure_columns(
            con,
            "frames",
            {
                "window_uuid": "text",
                "window_confidence": "real default 0",
            },
        )
        _ensure_columns(
            con,
            "project_videos",
            {
                "project_media_uuid": "text",
                "display_name": "text",
                "category_override": "text",
                "summary_override": "text",
                "user_summary": "text default ''",
                "user_summary_updated_at": "text",
                "summary_migration_state": "text default 'none'",
                "analysis_status": "text",
                "perception_revision": "integer default 0",
                "perceived_at": "text",
                "source_fingerprint_json": "text default '{}'",
                "ownership_state": "text default 'project_owned'",
                "migration_generation": "integer default 0",
            },
        )
        _ensure_columns(
            con,
            "analysis_runs",
            {
                "base_revision": "integer",
                "provider_contract_json": "text default '{}'",
                "sampling_manifest_json": "text default '{}'",
                "window_manifest_json": "text default '[]'",
                "window_results_json": "text default '[]'",
                "window_validation_json": "text default '{}'",
                "interrupted_at": "text",
                "published_revision": "integer",
            },
        )
        _ensure_columns(
            con,
            "bgm_tracks",
            {
                "attribution_status": "text default 'unknown'",
                "license_status": "text default 'unverified'",
                "license_verified_at": "text",
                "license_source_url": "text",
                "verification_source": "text",
                "verification_provenance": "text",
            },
        )
        _migrate_bgm_license_state(con)
        for row in con.execute(
            "select id, video_id from segments where segment_uuid is null or segment_uuid=''"
        ).fetchall():
            con.execute(
                "update segments set segment_uuid=?, revision=coalesce(revision, 1) where id=?",
                (_legacy_segment_uuid(int(row["video_id"]), int(row["id"])), int(row["id"])),
            )
        for row in con.execute(
            "select project_id, video_id from project_videos where project_media_uuid is null or project_media_uuid=''"
        ).fetchall():
            con.execute(
                "update project_videos set project_media_uuid=? where project_id=? and video_id=?",
                (
                    _project_media_uuid(int(row["project_id"]), int(row["video_id"])),
                    int(row["project_id"]),
                    int(row["video_id"]),
                ),
            )
        _backfill_project_media_snapshots(con)
        _migrate_legacy_summary_ownership(con)
        con.execute(
            "create unique index if not exists idx_segments_segment_uuid on segments(segment_uuid)"
        )
        con.execute(
            "create unique index if not exists idx_project_videos_media_uuid on project_videos(project_id, project_media_uuid)"
        )


def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in con.execute(f"pragma table_info({table})").fetchall()}
    for name, spec in columns.items():
        if name not in existing:
            con.execute(f"alter table {table} add column {name} {spec}")


def _backfill_project_media_snapshots(con: sqlite3.Connection) -> None:
    con.execute(
        """update project_videos
        set display_name=coalesce(nullif(display_name, ''), (select filename from videos where id=project_videos.video_id)),
            category_override=coalesce(nullif(category_override, ''), (select category from videos where id=project_videos.video_id), 'unknown'),
            analysis_status=coalesce(nullif(analysis_status, ''), (select status from videos where id=project_videos.video_id), 'new'),
            user_summary=coalesce(user_summary, ''),
            summary_migration_state=coalesce(nullif(summary_migration_state, ''), 'none'),
            perception_revision=coalesce(perception_revision, 0)"""
    )
    con.execute(
        """update project_videos
        set ownership_state=coalesce(nullif(ownership_state, ''), 'project_owned'),
            source_fingerprint_json=coalesce(nullif(source_fingerprint_json, ''), '{}'),
            migration_generation=coalesce(migration_generation, 0)"""
    )


def _migrate_legacy_summary_ownership(con: sqlite3.Connection) -> None:
    """Conservatively recover user-authored text from legacy summary_override.

    #37 used summary_override both as an AI snapshot and as the editable field.
    We migrate only values that clearly differ from the first AI observation, or
    values saved before frames existed. Identical repeated observations remain
    flagged for review instead of being guessed as user-authored.
    """
    rows = con.execute(
        """select project_id, video_id, summary_override, user_summary,
                  coalesce(summary_migration_state, 'none') as migration_state
        from project_videos
        where coalesce(summary_override, '')<>''
          and coalesce(user_summary, '')=''
          and coalesce(summary_migration_state, 'none')='none'"""
    ).fetchall()
    for row in rows:
        legacy = str(row["summary_override"] or "").strip()
        frame_summaries = [
            str(frame["vision_summary"] or "").strip()
            for frame in con.execute(
                """select vision_summary from frames
                where video_id=? and coalesce(vision_summary, '')<>''
                order by timestamp_seconds, id""",
                (int(row["video_id"]),),
            ).fetchall()
        ]
        if not frame_summaries or legacy != frame_summaries[0]:
            con.execute(
                """update project_videos
                set user_summary=?, user_summary_updated_at=coalesce(user_summary_updated_at, current_timestamp),
                    summary_migration_state='migrated'
                where project_id=? and video_id=?""",
                (legacy, int(row["project_id"]), int(row["video_id"])),
            )
        elif len(set(frame_summaries)) == 1:
            con.execute(
                """update project_videos set summary_migration_state='review'
                where project_id=? and video_id=?""",
                (int(row["project_id"]), int(row["video_id"])),
            )
        else:
            con.execute(
                """update project_videos set summary_migration_state='legacy_ai_snapshot'
                where project_id=? and video_id=?""",
                (int(row["project_id"]), int(row["video_id"])),
            )


def _migrate_bgm_license_state(con: sqlite3.Connection) -> None:
    """Map legacy attribution booleans without treating every zero as safe."""
    rows = con.execute(
        "select id, license_name, license_url, source_url, attribution_required, attribution_status, license_status, verification_source from bgm_tracks"
    ).fetchall()
    for row in rows:
        attribution_status = str(row["attribution_status"] or "").strip()
        license_status = str(row["license_status"] or "").strip()
        verification_source = str(row["verification_source"] or "").strip()
        if verification_source not in {"", "legacy_migration"}:
            continue
        if attribution_status and license_status and license_status not in {"unverified", ""}:
            continue
        name = str(row["license_name"] or "").strip().lower()
        license_url = str(row["license_url"] or "").strip()
        source_url = str(row["source_url"] or "").strip()
        if any(token in name for token in ("cc0", "public domain", "public-domain", "自有", "self-owned")):
            next_attribution = "not_required"
            next_license = "verified" if name and (license_url or source_url) else "unverified"
        elif int(row["attribution_required"] or 0):
            next_attribution = "required"
            next_license = "verified" if name and license_url else "unverified"
        else:
            next_attribution = "unknown"
            next_license = "unverified"
        con.execute(
            "update bgm_tracks set attribution_status=case when attribution_status is null or attribution_status='' or attribution_status='unknown' then ? else attribution_status end, license_status=case when license_status is null or license_status='' or license_status='unverified' then ? else license_status end, verification_source=coalesce(nullif(verification_source, ''), 'legacy_migration'), verification_provenance=coalesce(nullif(verification_provenance, ''), 'derived from legacy license fields') where id=?",
            (next_attribution, next_license, int(row["id"])),
        )


def _legacy_segment_uuid(video_id: int, row_id: int) -> str:
    return str(uuid5(IDENTITY_NAMESPACE, f"video:{video_id}:legacy-segment-row:{row_id}"))


def _project_media_uuid(project_id: int, video_id: int) -> str:
    return str(uuid5(IDENTITY_NAMESPACE, f"project:{project_id}:video:{video_id}"))


def upsert_video(db: Path, row: dict) -> int:
    keys = sorted(row)
    values = [row[k] for k in keys]
    updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "original_path")
    sql = f"""
    insert into videos ({", ".join(keys)}) values ({", ".join("?" for _ in keys)})
    on conflict(original_path) do update set {updates}
    """
    with connect(db) as con:
        con.execute(sql, values)
        return int(con.execute("select id from videos where original_path=?", (row["original_path"],)).fetchone()["id"])


def add_frame(db: Path, video_id: int, frame_path: Path, timestamp: float = 0) -> None:
    with connect(db) as con:
        existing = con.execute("select id from frames where video_id=? and frame_path=?", (video_id, str(frame_path))).fetchone()
        if existing:
            con.execute("update frames set timestamp_seconds=? where id=?", (timestamp, existing["id"]))
            return
        con.execute(
            "insert into frames(video_id,timestamp_seconds,frame_path) values(?,?,?)",
            (video_id, timestamp, str(frame_path)),
        )


def update_frame_analysis(db: Path, frame_id: int, result: dict) -> None:
    with connect(db) as con:
        con.execute(
            """update frames set vision_summary=?, tags=?, score_visual_quality=?, score_usefulness=?, window_uuid=?, window_confidence=?
            where id=?""",
            (
                result["summary"],
                ",".join(result["tags"]),
                result["visual_quality_score"],
                result["usefulness_score"],
                str(result.get("window_uuid") or ""),
                float(result.get("window_confidence") or 0),
                frame_id,
            ),
        )


def add_analysis(db: Path, video_id: int, provider: str, model: str, result: dict, raw_path: Path) -> dict:
    init_db(db)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(db) as con:
        report = _replace_segments_in_connection(con, video_id, result["segments"])
        con.execute(
            "insert into analysis_runs(video_id,provider,model,status,raw_output_path) values(?,?,?,?,?)",
            (video_id, provider, model, "done", str(raw_path)),
        )
        con.execute("update videos set status='analyzed' where id=?", (video_id,))
        return report


def replace_segments(db: Path, video_id: int, incoming_segments: list[dict]) -> dict:
    init_db(db)
    with connect(db) as con:
        report = _replace_segments_in_connection(con, video_id, incoming_segments)
        con.execute("update videos set status='analyzed' where id=?", (video_id,))
        return report


def _replace_segments_in_connection(
    con: sqlite3.Connection,
    video_id: int,
    incoming_segments: list[dict],
) -> dict:
    previous = [dict(row) for row in con.execute(
        "select * from segments where video_id=? order by start_seconds, id",
        (video_id,),
    ).fetchall()]
    incoming = [_normalize_segment(row) for row in incoming_segments]
    assigned, report = _assign_segment_identities(video_id, previous, incoming)
    con.execute("delete from segments where video_id=?", (video_id,))
    for seg in assigned:
        con.execute(
            """insert into segments(
                segment_uuid,revision,video_id,start_seconds,end_seconds,segment_type,title,reason,tags,score,suggested_use,
                window_uuid,action,shot_role,technical_quality_json,duplicate_group,natural_audio_recommendation,confidence
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seg["segment_uuid"],
                seg["revision"],
                video_id,
                seg["start_seconds"],
                seg["end_seconds"],
                seg["segment_type"],
                seg["title"],
                seg["reason"],
                seg["tags"],
                seg["score"],
                seg["suggested_use"],
                seg.get("window_uuid", ""),
                seg.get("action", ""),
                seg.get("shot_role", ""),
                seg.get("technical_quality_json", "{}"),
                seg.get("duplicate_group", ""),
                seg.get("natural_audio_recommendation", "unknown"),
                seg.get("confidence", 0),
            ),
        )
    con.execute(
        "insert into segment_identity_migrations(video_id, report_json) values(?, ?)",
        (video_id, json.dumps(report, ensure_ascii=False, sort_keys=True)),
    )
    return report


def _normalize_segment(row: dict) -> dict:
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tag_text = ",".join(part.strip() for part in tags.split(",") if part.strip())
    else:
        tag_text = ",".join(str(part).strip() for part in tags if str(part).strip())
    start = round(float(row.get("start_seconds") or 0), 6)
    end = round(max(start, float(row.get("end_seconds") or start)), 6)
    return {
        "start_seconds": start,
        "end_seconds": end,
        "segment_type": str(row.get("segment_type") or ""),
        "title": str(row.get("title") or ""),
        "reason": str(row.get("reason") or ""),
        "tags": tag_text,
        "score": float(row.get("score") or 0),
        "suggested_use": str(row.get("suggested_use") or ""),
        "window_uuid": str(row.get("window_uuid") or ""),
        "action": str(row.get("action") or ""),
        "shot_role": str(row.get("shot_role") or ""),
        "technical_quality_json": (
            json.dumps(row.get("technical_quality"), ensure_ascii=False, sort_keys=True)
            if isinstance(row.get("technical_quality"), dict)
            else str(row.get("technical_quality_json") or "{}")
        ),
        "duplicate_group": str(row.get("duplicate_group") or ""),
        "natural_audio_recommendation": str(row.get("natural_audio_recommendation") or "unknown"),
        "confidence": float(row.get("confidence") or row.get("score") or 0),
    }


def _assign_segment_identities(
    video_id: int,
    previous: list[dict],
    incoming: list[dict],
) -> tuple[list[dict], dict]:
    pair_scores: list[tuple[float, int, int]] = []
    overlap_old: dict[int, list[int]] = {index: [] for index in range(len(previous))}
    overlap_new: dict[int, list[int]] = {index: [] for index in range(len(incoming))}
    scored_by_new: dict[int, list[tuple[float, int]]] = {index: [] for index in range(len(incoming))}
    for old_index, old in enumerate(previous):
        for new_index, new in enumerate(incoming):
            overlap = _overlap_seconds(old, new)
            iou = _temporal_iou(old, new)
            if overlap > 0 and iou >= 0.12:
                overlap_old[old_index].append(new_index)
                overlap_new[new_index].append(old_index)
            score = _identity_score(old, new)
            if score >= 0.45 and (iou >= 0.2 or overlap > 0):
                pair_scores.append((score, old_index, new_index))
                scored_by_new[new_index].append((score, old_index))

    matched_old: set[int] = set()
    matched_new: set[int] = set()
    matches: dict[int, tuple[int, float]] = {}
    for score, old_index, new_index in sorted(pair_scores, reverse=True):
        if old_index in matched_old or new_index in matched_new:
            continue
        matched_old.add(old_index)
        matched_new.add(new_index)
        matches[new_index] = (old_index, score)

    split_old = {index for index, children in overlap_old.items() if len(children) > 1}
    merge_new = {index for index, parents in overlap_new.items() if len(parents) > 1}
    ambiguous = []
    for new_index, candidates in scored_by_new.items():
        ranked = sorted(candidates, reverse=True)
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            ambiguous.append(
                {
                    "incoming_index": new_index,
                    "candidate_segment_uuids": [
                        str(previous[old_index].get("segment_uuid") or "")
                        for _, old_index in ranked[:3]
                    ],
                    "scores": [round(score, 6) for score, _ in ranked[:3]],
                }
            )

    assigned: list[dict] = []
    matched_report = []
    new_report = []
    child_rank: dict[int, int] = {}
    for new_index, new in enumerate(incoming):
        item = dict(new)
        if new_index in matches:
            old_index, score = matches[new_index]
            old = previous[old_index]
            item["segment_uuid"] = str(old["segment_uuid"])
            item["revision"] = int(old.get("revision") or 1) + 1
            kind = "one_to_one"
            if old_index in split_old:
                kind = "split_primary"
            if new_index in merge_new:
                kind = "merge_primary" if kind == "one_to_one" else "split_merge_primary"
            matched_report.append(
                {
                    "segment_uuid": item["segment_uuid"],
                    "kind": kind,
                    "score": round(score, 6),
                    "previous": _segment_snapshot(old),
                    "current": _segment_snapshot(item),
                }
            )
        else:
            split_parents = overlap_new.get(new_index, [])
            if len(split_parents) == 1 and split_parents[0] in split_old:
                parent_index = split_parents[0]
                child_rank[parent_index] = child_rank.get(parent_index, 0) + 1
                parent_uuid = str(previous[parent_index]["segment_uuid"])
                item["segment_uuid"] = _split_child_uuid(
                    parent_uuid,
                    child_rank[parent_index],
                    item,
                )
                reason = "split_child"
            else:
                item["segment_uuid"] = str(uuid4())
                reason = "new_segment"
            item["revision"] = 1
            new_report.append({"reason": reason, **_segment_snapshot(item)})
        assigned.append(item)

    assigned_by_index = {index: row for index, row in enumerate(assigned)}
    splits = []
    for old_index in sorted(split_old):
        children = overlap_old[old_index]
        splits.append(
            {
                "previous_segment_uuid": str(previous[old_index]["segment_uuid"]),
                "current_segment_uuids": [
                    str(assigned_by_index[index]["segment_uuid"]) for index in children
                ],
                "requires_review": True,
            }
        )
    merges = []
    for new_index in sorted(merge_new):
        merges.append(
            {
                "previous_segment_uuids": [
                    str(previous[index]["segment_uuid"])
                    for index in overlap_new[new_index]
                ],
                "current_segment_uuid": str(assigned_by_index[new_index]["segment_uuid"]),
                "requires_review": True,
            }
        )

    assigned_uuids = {str(row["segment_uuid"]) for row in assigned}
    removed = [
        _segment_snapshot(row)
        for row in previous
        if str(row.get("segment_uuid") or "") not in assigned_uuids
    ]
    report = {
        "schema_version": 1,
        "video_id": video_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matched": matched_report,
        "new": new_report,
        "removed": removed,
        "splits": splits,
        "merges": merges,
        "ambiguous": ambiguous,
        "requires_review": bool(splits or merges or ambiguous or removed),
    }
    return assigned, report


def _split_child_uuid(parent_uuid: str, rank: int, segment: dict) -> str:
    namespace = UUID(parent_uuid)
    key = (
        f"split:{rank}:"
        f"{float(segment.get('start_seconds') or 0):.3f}:"
        f"{float(segment.get('end_seconds') or 0):.3f}"
    )
    return str(uuid5(namespace, key))


def _segment_snapshot(row: dict) -> dict:
    return {
        "segment_uuid": str(row.get("segment_uuid") or ""),
        "revision": int(row.get("revision") or 1),
        "start_seconds": round(float(row.get("start_seconds") or 0), 6),
        "end_seconds": round(float(row.get("end_seconds") or 0), 6),
        "segment_type": str(row.get("segment_type") or ""),
        "title": str(row.get("title") or ""),
        "tags": str(row.get("tags") or ""),
    }


def _identity_score(old: dict, new: dict) -> float:
    iou = _temporal_iou(old, new)
    old_duration = max(0.001, float(old.get("end_seconds") or 0) - float(old.get("start_seconds") or 0))
    new_duration = max(0.001, float(new.get("end_seconds") or 0) - float(new.get("start_seconds") or 0))
    old_midpoint = (float(old.get("start_seconds") or 0) + float(old.get("end_seconds") or 0)) / 2
    new_midpoint = (float(new.get("start_seconds") or 0) + float(new.get("end_seconds") or 0)) / 2
    midpoint_scale = max(1.0, old_duration, new_duration)
    midpoint_similarity = max(0.0, 1.0 - abs(old_midpoint - new_midpoint) / midpoint_scale)
    old_tokens = _segment_tokens(old)
    new_tokens = _segment_tokens(new)
    union = old_tokens | new_tokens
    semantic = len(old_tokens & new_tokens) / len(union) if union else 1.0
    return 0.75 * iou + 0.2 * midpoint_similarity + 0.05 * semantic


def _segment_tokens(row: dict) -> set[str]:
    text = " ".join(
        [
            str(row.get("segment_type") or ""),
            str(row.get("title") or ""),
            str(row.get("tags") or "").replace(",", " "),
        ]
    ).lower()
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]+", text) if token}


def _overlap_seconds(left: dict, right: dict) -> float:
    start = max(float(left.get("start_seconds") or 0), float(right.get("start_seconds") or 0))
    end = min(float(left.get("end_seconds") or 0), float(right.get("end_seconds") or 0))
    return max(0.0, end - start)


def _temporal_iou(left: dict, right: dict) -> float:
    overlap = _overlap_seconds(left, right)
    if overlap <= 0:
        return 0.0
    start = min(float(left.get("start_seconds") or 0), float(right.get("start_seconds") or 0))
    end = max(float(left.get("end_seconds") or 0), float(right.get("end_seconds") or 0))
    union = max(0.001, end - start)
    return overlap / union


def latest_segment_identity_migration(db: Path, video_id: int) -> dict:
    init_db(db)
    with connect(db) as con:
        row = con.execute(
            "select report_json from segment_identity_migrations where video_id=? order by id desc limit 1",
            (video_id,),
        ).fetchone()
    return json.loads(row["report_json"]) if row else {}


def videos(db: Path) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from videos order by id").fetchall()


def segments(db: Path, video_id: int) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from segments where video_id=? order by start_seconds", (video_id,)).fetchall()


def frames(db: Path, video_id: int) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from frames where video_id=? order by timestamp_seconds", (video_id,)).fetchall()


def update_project_media_summary(db: Path, project_id: int, video_id: int, summary: str) -> bool:
    init_db(db)
    with connect(db) as con:
        row = con.execute(
            "select 1 from project_videos where project_id=? and video_id=?",
            (int(project_id), int(video_id)),
        ).fetchone()
        if not row:
            return False
        con.execute(
            """update project_videos
            set user_summary=?, user_summary_updated_at=current_timestamp,
                summary_migration_state='native'
            where project_id=? and video_id=?""",
            (str(summary).strip(), int(project_id), int(video_id)),
        )
        con.execute("update projects set updated_at=current_timestamp where id=?", (int(project_id),))
        return True


def update_video_summary(db: Path, video_id: int, summary: str, project_id: int | None = None) -> bool:
    """Save user-authored context without mutating AI frame perception."""
    init_db(db)
    if project_id is not None:
        return update_project_media_summary(db, int(project_id), int(video_id), summary)
    with connect(db) as con:
        owners = [
            int(row["project_id"])
            for row in con.execute(
                "select project_id from project_videos where video_id=? order by project_id",
                (int(video_id),),
            ).fetchall()
        ]
        if len(owners) > 1:
            return False
        if len(owners) == 1:
            con.execute(
                """update project_videos
                set user_summary=?, user_summary_updated_at=current_timestamp,
                    summary_migration_state='native'
                where project_id=? and video_id=?""",
                (str(summary).strip(), owners[0], int(video_id)),
            )
            con.execute("update projects set updated_at=current_timestamp where id=?", (owners[0],))
            return True
        exists = con.execute("select 1 from videos where id=?", (int(video_id),)).fetchone()
        if not exists:
            return False
        con.execute(
            "update videos set user_summary=?, user_summary_updated_at=current_timestamp where id=?",
            (str(summary).strip(), int(video_id)),
        )
        return True


def update_project_media_metadata(
    db: Path,
    project_media_uuid: str,
    *,
    display_name: str | None = None,
    category: str | None = None,
    analysis_status: str | None = None,
    increment_perception_revision: bool = False,
) -> bool:
    init_db(db)
    updates: list[str] = []
    values: list[object] = []
    if display_name is not None:
        updates.append("display_name=?")
        values.append(str(display_name))
    if category is not None:
        updates.append("category_override=?")
        values.append(str(category))
    if analysis_status is not None:
        updates.append("analysis_status=?")
        values.append(str(analysis_status))
    if increment_perception_revision:
        updates.extend(["perception_revision=coalesce(perception_revision, 0)+1", "perceived_at=current_timestamp"])
    if not updates:
        return False
    with connect(db) as con:
        owner = con.execute(
            "select project_id from project_videos where project_media_uuid=?",
            (str(project_media_uuid),),
        ).fetchone()
        if not owner:
            return False
        con.execute(
            f"update project_videos set {', '.join(updates)} where project_media_uuid=?",
            (*values, str(project_media_uuid)),
        )
        con.execute("update projects set updated_at=current_timestamp where id=?", (int(owner["project_id"]),))
        return True


def project_ids_for_video(db: Path, video_id: int) -> list[int]:
    init_db(db)
    with connect(db) as con:
        return [
            int(row["project_id"])
            for row in con.execute(
                "select project_id from project_videos where video_id=? order by project_id",
                (int(video_id),),
            ).fetchall()
        ]


def set_video_status(db: Path, video_id: int, status: str) -> None:
    with connect(db) as con:
        con.execute("update videos set status=? where id=?", (status, video_id))


def update_video_file(db: Path, video_id: int, path: Path, category: str) -> None:
    with connect(db) as con:
        con.execute("update videos set current_path=?, filename=?, category=? where id=?", (str(path), path.name, category, video_id))


def write_json_index(db: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    data = [dict(v) for v in videos(db)]
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def add_bgm_track(db: Path, row: dict) -> int:
    keys = sorted(row)
    values = [row[k] for k in keys]
    updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "file_path")
    with connect(db) as con:
        con.execute(
            f"""insert into bgm_tracks ({", ".join(keys)}) values ({", ".join("?" for _ in keys)})
            on conflict(file_path) do update set {updates}""",
            values,
        )
        return int(con.execute("select id from bgm_tracks where file_path=?", (row["file_path"],)).fetchone()["id"])


def bgm_tracks(db: Path) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from bgm_tracks order by added_at desc, id desc").fetchall()


def add_project_bgm(db: Path, project_id: int, bgm_id: int) -> None:
    with connect(db) as con:
        con.execute("insert or ignore into project_bgm(project_id, bgm_id) values(?, ?)", (project_id, bgm_id))
        con.execute("update projects set updated_at=current_timestamp where id=?", (project_id,))


def project_bgm_tracks(db: Path, project_id: int) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute(
            """select b.*
            from project_bgm pb
            join bgm_tracks b on b.id=pb.bgm_id
            where pb.project_id=?
            order by b.added_at desc, b.id desc""",
            (project_id,),
        ).fetchall()


def create_project_row(db: Path, name: str, kind: str = "auto", category: str = "unknown", content_type: str = "diary_montage", platform: str = "YouTube", target_duration_seconds: float = 0) -> int:
    with connect(db) as con:
        cur = con.execute(
            "insert into projects(name, kind, category, content_type, platform, target_duration_seconds) values(?, ?, ?, ?, ?, ?)",
            (name, kind, category, content_type, platform, target_duration_seconds),
        )
        return int(cur.lastrowid)


def set_project_videos(db: Path, project_id: int, video_ids: list[int]) -> None:
    init_db(db)
    ordered_ids = [int(video_id) for video_id in video_ids]
    with connect(db) as con:
        existing = {
            int(row["video_id"]): str(row["project_media_uuid"] or "")
            for row in con.execute(
                "select video_id, project_media_uuid from project_videos where project_id=?",
                (project_id,),
            ).fetchall()
        }
        if ordered_ids:
            placeholders = ",".join("?" for _ in ordered_ids)
            con.execute(
                f"delete from project_videos where project_id=? and video_id not in ({placeholders})",
                (project_id, *ordered_ids),
            )
        else:
            con.execute("delete from project_videos where project_id=?", (project_id,))
        for order, video_id in enumerate(ordered_ids, 1):
            media_uuid = existing.get(video_id) or _project_media_uuid(project_id, video_id)
            snapshot = con.execute(
                """select filename, category, status,
                    coalesce(user_summary, '') as user_summary,
                    user_summary_updated_at
                from videos where id=?""",
                (video_id,),
            ).fetchone()
            if not snapshot:
                raise ValueError(f"video not found: {video_id}")
            con.execute(
                """insert into project_videos(
                    project_id, video_id, project_media_uuid, display_name, category_override,
                    summary_override, user_summary, user_summary_updated_at, summary_migration_state,
                    analysis_status, perception_revision, sort_order
                ) values(?, ?, ?, ?, ?, '', ?, ?, 'none', ?, 0, ?)
                on conflict(project_id, video_id) do update set
                  sort_order=excluded.sort_order,
                  project_media_uuid=coalesce(nullif(project_videos.project_media_uuid, ''), excluded.project_media_uuid)""",
                (
                    project_id,
                    video_id,
                    media_uuid,
                    str(snapshot["filename"] or ""),
                    str(snapshot["category"] or "unknown"),
                    str(snapshot["user_summary"] or ""),
                    snapshot["user_summary_updated_at"],
                    str(snapshot["status"] or "new"),
                    order,
                ),
            )
        con.execute("update projects set updated_at=current_timestamp where id=?", (project_id,))


def projects(db: Path) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute(
            """select p.*, count(pv.video_id) as video_count
            from projects p
            left join project_videos pv on pv.project_id=p.id
            group by p.id
            order by p.updated_at desc, p.id desc"""
        ).fetchall()


def project(db: Path, project_id: int) -> sqlite3.Row | None:
    with connect(db) as con:
        return con.execute("select * from projects where id=?", (project_id,)).fetchone()


def project_videos(db: Path, project_id: int) -> list[sqlite3.Row]:
    init_db(db)
    with connect(db) as con:
        return con.execute(
            """select
              v.id,
              v.original_path,
              v.current_path,
              coalesce(nullif(pv.display_name, ''), v.filename) as filename,
              coalesce(nullif(pv.category_override, ''), v.category, 'unknown') as category,
              v.created_at,
              v.imported_at,
              v.duration_seconds,
              v.width,
              v.height,
              v.fps,
              v.codec,
              v.file_size,
              v.proxy_path,
              coalesce(nullif(pv.analysis_status, ''), v.status, 'new') as status,
              pv.project_id,
              pv.project_media_uuid,
              pv.display_name as project_display_name,
              pv.category_override as project_category,
              coalesce(nullif(pv.user_summary, ''), (
                select coalesce(vision_summary, '') from frames
                where frames.video_id=pv.video_id
                order by timestamp_seconds, id limit 1
              ), '') as project_summary,
              coalesce(pv.user_summary, '') as user_summary,
              pv.user_summary_updated_at,
              coalesce(pv.summary_migration_state, 'none') as user_summary_migration_state,
              pv.summary_override as legacy_summary_override,
              pv.analysis_status as project_analysis_status,
              coalesce(pv.perception_revision, 0) as perception_revision,
              coalesce(pv.source_fingerprint_json, '{}') as source_fingerprint_json,
              coalesce(pv.ownership_state, 'project_owned') as ownership_state,
              coalesce(pv.migration_generation, 0) as migration_generation,
              pv.perceived_at,
              pv.sort_order
            from project_videos pv
            join videos v on v.id=pv.video_id
            where pv.project_id=?
            order by pv.sort_order, v.id""",
            (project_id,),
        ).fetchall()


def set_project_status(db: Path, project_id: int, status: str) -> None:
    with connect(db) as con:
        con.execute("update projects set status=?, updated_at=current_timestamp where id=?", (status, project_id))


def project_revision(db: Path, project_id: int) -> int:
    """Return the persisted monotonic revision for a project."""
    init_db(db)
    with connect(db) as con:
        row = con.execute("select project_revision from projects where id=?", (int(project_id),)).fetchone()
    if not row:
        raise ValueError(f"project not found: {project_id}")
    return max(1, int(row["project_revision"] or 1))
