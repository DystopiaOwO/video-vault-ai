from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise SystemExit(f"missing patch target: {label}")
    return text.replace(old, new, 1)


path = Path("src/video_vault/perception_runs.py")
text = path.read_text(encoding="utf-8")
text = replace_once(text, "import shutil\n", "import shutil\nimport sqlite3\n", "sqlite import")
text = replace_once(
    text,
    '''        con.execute(
            "create index if not exists idx_analysis_runs_project on analysis_runs(project_id, id desc)"
        )
        con.execute("update analysis_runs set status='succeeded' where status='done'")
''',
    '''        con.execute(
            "create index if not exists idx_analysis_runs_project on analysis_runs(project_id, id desc)"
        )
        con.execute(
            """create unique index if not exists idx_analysis_runs_active_video
            on analysis_runs(video_id)
            where status in ('queued','running','publishing')"""
        )
        con.execute("update analysis_runs set status='succeeded' where status='done'")
''',
    "active run index",
)
text = replace_once(
    text,
    '''        con.execute(
            """insert into analysis_runs(
                video_id, provider, model, status, raw_output_path, run_uuid, project_id,
                project_media_uuid, generation, requested_at, started_at,
                input_snapshot_json, staging_path, previous_success_run_uuid
            ) values(?, ?, ?, 'running', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video_id,
                str(cfg.get("ai", {}).get("provider", "mock")),
                str(cfg.get("ai", {}).get("model", "")),
                run_uuid,
                int(project_id),
                project_media_uuid,
                generation,
                created,
                created,
                json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
                str(staging),
                previous_uuid,
            ),
        )
''',
    '''        try:
            con.execute(
                """insert into analysis_runs(
                    video_id, provider, model, status, raw_output_path, run_uuid, project_id,
                    project_media_uuid, generation, requested_at, started_at,
                    input_snapshot_json, staging_path, previous_success_run_uuid
                ) values(?, ?, ?, 'running', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    str(cfg.get("ai", {}).get("provider", "mock")),
                    str(cfg.get("ai", {}).get("model", "")),
                    run_uuid,
                    int(project_id),
                    project_media_uuid,
                    generation,
                    created,
                    created,
                    json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
                    str(staging),
                    previous_uuid,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"video {video_id} already has an active perception run") from exc
''',
    "active run conflict",
)
text = replace_once(
    text,
    '''        migration = con.execute(
            "select coalesce(max(id), 0) as max_id from segment_identity_migrations"
        ).fetchone()
        video = con.execute("select status from videos where id=?", (int(video_id),)).fetchone()
        return {
''',
    '''        migration_ids = [
            int(row["id"])
            for row in con.execute(
                "select id from segment_identity_migrations where video_id=? order by id",
                (int(video_id),),
            ).fetchall()
        ]
        video = con.execute("select status from videos where id=?", (int(video_id),)).fetchone()
        return {
''',
    "migration snapshot",
)
text = replace_once(
    text,
    '''            "project_videos": [dict(row) for row in con.execute("select * from project_videos where video_id=? order by project_id", (int(video_id),)).fetchall()],
            "migration_max_id": int(migration["max_id"]),
''',
    '''            "project_videos": [dict(row) for row in con.execute("select * from project_videos where video_id=? order by project_id", (int(video_id),)).fetchall()],
            "migration_ids": migration_ids,
''',
    "migration ids payload",
)
text = replace_once(
    text,
    '''        con.execute(
            "delete from segment_identity_migrations where id>?",
            (int(snapshot.get("migration_max_id") or 0),),
        )
''',
    '''        migration_ids = [int(value) for value in snapshot.get("migration_ids", [])]
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
''',
    "scoped migration restore",
)
path.write_text(text, encoding="utf-8")
