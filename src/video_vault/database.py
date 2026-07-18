from __future__ import annotations

from pathlib import Path
import json
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
  score_usefulness real
);
create table if not exists segments (
  id integer primary key,
  video_id integer,
  start_seconds real,
  end_seconds real,
  segment_type text,
  title text,
  reason text,
  tags text,
  score real,
  suggested_use text
);
create table if not exists analysis_runs (
  id integer primary key,
  video_id integer,
  provider text,
  model text,
  created_at text default current_timestamp,
  status text,
  raw_output_path text
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
  created_at text default current_timestamp,
  updated_at text default current_timestamp
);
create table if not exists project_videos (
  project_id integer not null,
  video_id integer not null,
  sort_order integer default 0,
  primary key(project_id, video_id)
);
create table if not exists project_bgm (
  project_id integer not null,
  bgm_id integer not null,
  primary key(project_id, bgm_id)
);
"""


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def init_db(db: Path) -> None:
    with connect(db) as con:
        con.executescript(SCHEMA)
        existing = {row["name"] for row in con.execute("pragma table_info(projects)").fetchall()}
        for name, spec in {
            "category": "text default 'unknown'",
            "content_type": "text default 'diary_montage'",
            "platform": "text default 'YouTube'",
            "target_duration_seconds": "real default 0",
        }.items():
            if name not in existing:
                con.execute(f"alter table projects add column {name} {spec}")


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
            """update frames set vision_summary=?, tags=?, score_visual_quality=?, score_usefulness=?
            where id=?""",
            (
                result["summary"],
                ",".join(result["tags"]),
                result["visual_quality_score"],
                result["usefulness_score"],
                frame_id,
            ),
        )


def add_analysis(db: Path, video_id: int, provider: str, model: str, result: dict, raw_path: Path) -> None:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect(db) as con:
        con.execute("delete from segments where video_id=?", (video_id,))
        for seg in result["segments"]:
            con.execute(
                """insert into segments(video_id,start_seconds,end_seconds,segment_type,title,reason,tags,score,suggested_use)
                values(?,?,?,?,?,?,?,?,?)""",
                (
                    video_id,
                    seg["start_seconds"],
                    seg["end_seconds"],
                    seg["segment_type"],
                    seg["title"],
                    seg["reason"],
                    ",".join(seg["tags"]),
                    seg["score"],
                    seg["suggested_use"],
                ),
            )
        con.execute(
            "insert into analysis_runs(video_id,provider,model,status,raw_output_path) values(?,?,?,?,?)",
            (video_id, provider, model, "done", str(raw_path)),
        )
        con.execute("update videos set status='analyzed' where id=?", (video_id,))


def replace_segments(db: Path, video_id: int, segments: list[dict]) -> None:
    with connect(db) as con:
        con.execute("delete from segments where video_id=?", (video_id,))
        for seg in segments:
            con.execute(
                """insert into segments(video_id,start_seconds,end_seconds,segment_type,title,reason,tags,score,suggested_use)
                values(?,?,?,?,?,?,?,?,?)""",
                (
                    video_id,
                    seg["start_seconds"],
                    seg["end_seconds"],
                    seg["segment_type"],
                    seg["title"],
                    seg["reason"],
                    ",".join(seg["tags"]),
                    seg["score"],
                    seg["suggested_use"],
                ),
            )
        con.execute("update videos set status='analyzed' where id=?", (video_id,))


def videos(db: Path) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from videos order by id").fetchall()


def segments(db: Path, video_id: int) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from segments where video_id=? order by start_seconds", (video_id,)).fetchall()


def frames(db: Path, video_id: int) -> list[sqlite3.Row]:
    with connect(db) as con:
        return con.execute("select * from frames where video_id=? order by timestamp_seconds", (video_id,)).fetchall()


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
    with connect(db) as con:
        con.execute("delete from project_videos where project_id=?", (project_id,))
        for order, video_id in enumerate(video_ids, 1):
            con.execute(
                "insert into project_videos(project_id, video_id, sort_order) values(?, ?, ?)",
                (project_id, video_id, order),
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
    with connect(db) as con:
        return con.execute(
            """select v.*
            from project_videos pv
            join videos v on v.id=pv.video_id
            where pv.project_id=?
            order by pv.sort_order, v.id""",
            (project_id,),
        ).fetchall()


def set_project_status(db: Path, project_id: int, status: str) -> None:
    with connect(db) as con:
        con.execute("update projects set status=?, updated_at=current_timestamp where id=?", (status, project_id))
