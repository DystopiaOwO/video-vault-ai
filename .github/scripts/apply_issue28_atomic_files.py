from __future__ import annotations

from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise SystemExit(f"missing patch target: {label}")
    return text.replace(old, new, 1)


def replace_block(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"missing block target: {label}")
    return updated


def patch_perception_runs() -> None:
    path = Path("src/video_vault/perception_runs.py")
    text = path.read_text(encoding="utf-8")
    text = replace_block(
        text,
        r"def create_perception_run\(\n    db: Path,\n    cfg: dict,\n    project_id: int,\n    video: dict,\n\) -> dict:.*?\n\ndef analysis_run",
        '''def create_perception_run(
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
                    input_snapshot_json, staging_path, previous_success_run_uuid
                ) values(?, ?, ?, 'running', '', ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
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
                    str(staging),
                    previous_uuid,
                ),
            )
        except sqlite3.IntegrityError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"video {video_id} already has an active perception run") from exc
        con.execute(
            """update project_videos
            set current_analysis_run_uuid=?, analysis_generation=?, analysis_status='analyzing'
            where video_id=?""",
            (run_uuid, generation, video_id),
        )
        con.execute(
            """update projects
            set status='needs_review', updated_at=current_timestamp
            where id in (select project_id from project_videos where video_id=?)""",
            (video_id,),
        )
    try:
        input_snapshot = build_input_snapshot(video, cfg)
    except Exception as exc:
        mark_perception_run_terminal(db, run_uuid, "failed", str(exc))
        raise
    with connect(db) as con:
        con.execute(
            "update analysis_runs set input_snapshot_json=? where run_uuid=?",
            (json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True), run_uuid),
        )
    return analysis_run(db, run_uuid)


def analysis_run''',
        "create perception run",
    )
    path.write_text(text, encoding="utf-8")


def patch_planner() -> None:
    path = Path("src/video_vault/planner.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "STATUSES = {\"new\", \"ingested\", \"perceived\", \"plan_drafted\", \"needs_review\", \"approved\", \"rejected\", \"rendered\", \"needs_revision\"}\n\n\n",
        '''STATUSES = {"new", "ingested", "perceived", "plan_drafted", "needs_review", "approved", "rejected", "rendered", "needs_revision"}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


''',
        "planner atomic helper",
    )
    text = text.replace(
        '    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")\n',
        '    _atomic_write_text(out, json.dumps(data, ensure_ascii=False, indent=2))\n',
    )
    text = replace_once(
        text,
        '''    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path.write_text(edit_script(plan), encoding="utf-8")
    status_path.write_text(
        json.dumps({"video_id": plan["video_id"], "status": plan["status"], "created_at": now, "updated_at": now, "approved_by_user": False, "notes": ""}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
''',
        '''    _atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2))
    _atomic_write_text(script_path, edit_script(plan))
    _atomic_write_text(
        status_path,
        json.dumps({"video_id": plan["video_id"], "status": plan["status"], "created_at": now, "updated_at": now, "approved_by_user": False, "notes": ""}, ensure_ascii=False, indent=2),
    )
''',
        "write plan files",
    )
    text = replace_once(
        text,
        '''    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    status_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
''',
        '''    _atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2))
    _atomic_write_text(status_path, json.dumps(review, ensure_ascii=False, indent=2))
''',
        "set plan status",
    )
    text = replace_once(
        text,
        '    (folder / "edit_script.md").write_text(edit_script(plan) + f"\\n\\n## 修改備註\\n{notes}\\n", encoding="utf-8")\n',
        '    _atomic_write_text(folder / "edit_script.md", edit_script(plan) + f"\\n\\n## 修改備註\\n{notes}\\n")\n',
        "revise plan script",
    )
    path.write_text(text, encoding="utf-8")


def patch_project() -> None:
    path = Path("src/video_vault/project.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''    plan_path.write_text(payload, encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    version_path.write_text(payload, encoding="utf-8")
    version_script_path.write_text(script, encoding="utf-8")
    latest_path.write_text(json.dumps({"plan_id": plan_id, "path": str(version_path), "script_path": str(version_script_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
''',
        '''    _atomic_write_text(plan_path, payload)
    _atomic_write_text(script_path, script)
    _atomic_write_text(version_path, payload)
    _atomic_write_text(version_script_path, script)
    _atomic_write_text(latest_path, json.dumps({"plan_id": plan_id, "path": str(version_path), "script_path": str(version_script_path)}, ensure_ascii=False, indent=2))
''',
        "project plan writes",
    )
    text = replace_once(
        text,
        '''def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
''',
        '''def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
''',
        "project atomic helper",
    )
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_perception_runs()
    patch_planner()
    patch_project()
