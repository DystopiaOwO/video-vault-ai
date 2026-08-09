"""Isolated Windows real-media acceptance for VID-7 Formal Delivery QA.

The script only reads the supplied source media.  Short normalized fixtures,
the SQLite database, projects, formal renders, and QA bundles are all created
under ``--root``.  It intentionally stops before human delivery confirmation;
that final boundary is exercised through the WebUI against the same root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from video_vault.database import add_analysis, connect, init_db, upsert_video
from video_vault.delivery_qa import delivery_qa_for_api
from video_vault.media_probe import probe_media
from video_vault.project import build_project_plan, create_project, project_dir, set_review_status
from video_vault.render_job_manager import RenderJobManager
from video_vault.storyboard import generate_storyboard


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"sha256": _sha256(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _run(command: list[str], *, timeout: int = 180) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "command failed")[-2000:])


def _derive_fixture(ffmpeg: str, ffprobe: str, source: Path, target: Path) -> float:
    source_probe = probe_media(ffprobe, source)
    if not source_probe.has_video or not source_probe.has_audio:
        raise RuntimeError("real acceptance source requires both video and audio streams")
    start = 0.5 if source_probe.duration_seconds >= 3 else 0.0
    duration = min(2.25, max(0.75, source_probe.duration_seconds - start - 0.1))
    target.parent.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target),
    ])
    derived = probe_media(ffprobe, target)
    if not derived.has_video or not derived.has_audio or derived.duration_seconds <= 0:
        raise RuntimeError("derived real-media fixture is not a decodable A/V clip")
    return float(derived.duration_seconds)


def _wait(manager: RenderJobManager, job_id: str, timeout: float = 240) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.1)
    raise TimeoutError(f"render job did not finish: {job_id}")


def _create_project(
    cfg: dict[str, Any],
    db: Path,
    root: Path,
    *,
    slug: str,
    category: str,
    content_type: str,
    sources: list[Path],
) -> dict[str, Any]:
    fixture_root = root / "real-fixtures" / slug
    video_ids: list[int] = []
    for index, source in enumerate(sources, start=1):
        derived = fixture_root / f"real-clip-{index:02d}.mp4"
        duration = _derive_fixture(str(cfg["ffmpeg_path"]), str(cfg["ffprobe_path"]), source, derived)
        video_id = upsert_video(db, {
            "original_path": str(derived),
            "current_path": str(derived),
            "filename": derived.name,
            "category": category,
            "duration_seconds": duration,
            "status": "perceived",
        })
        add_analysis(
            db,
            video_id,
            "real-media-fixture",
            "vid7-acceptance-v1",
            {"segments": [{
                "start_seconds": 0,
                "end_seconds": round(duration, 3),
                "segment_type": "key_action",
                "title": f"real segment {index}",
                "reason": "isolated VID-7 acceptance",
                "tags": [category, "real-media"],
                "score": 1.0,
                "suggested_use": "main",
            }]},
            fixture_root / f"analysis-{index:02d}.json",
        )
        video_ids.append(video_id)

    project_id = create_project(db, f"VID-7 {slug} acceptance", video_ids, category=category, content_type=content_type)
    build_project_plan(cfg, db, project_id)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    generate_storyboard(cfg, db, project_id)
    settings = {
        "profile_id": "final_1080p",
        "encoder": "cpu",
        "color": {"mode": "none", "lut_path": ""},
        "audio": {"original_gain_db": 0, "lower_original_gain_db": -12, "bgm_gain_db": -18},
        "transition": {"type": "cut", "duration_seconds": 0},
        "overlay": {"enabled": False},
    }
    (project_dir(cfg, project_id) / "render_settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    set_review_status(cfg, db, project_id, "approved")

    manager = RenderJobManager(cfg, db)
    try:
        created = manager.enqueue(project_id)
        if not created.get("created"):
            raise RuntimeError(str(created))
        job = _wait(manager, str(created["job"]["job_id"]))
    finally:
        manager.shutdown()
    if job.get("status") != "succeeded":
        raise RuntimeError(f"formal render failed: {job.get('error') or job.get('message')}")
    qa = delivery_qa_for_api(cfg, project_id)
    if not qa.get("exists") or qa.get("lifecycle_status") not in {"qa_needs_review", "qa_blocked"}:
        raise RuntimeError(f"automatic Delivery QA was not created: {qa}")
    if int((qa.get("summary") or {}).get("blocked") or 0) or int((qa.get("summary") or {}).get("skipped") or 0):
        raise RuntimeError(f"real acceptance Delivery QA is blocked: {qa.get('checks')}")
    return {
        "profile": content_type,
        "project_id": project_id,
        "render_job_uuid": job.get("job_id"),
        "qa_run_uuid": qa.get("qa_run_uuid"),
        "lifecycle_status": qa.get("lifecycle_status"),
        "summary": qa.get("summary"),
        "output_sha256": (qa.get("output_fingerprint") or {}).get("sha256"),
        "output_size_bytes": (qa.get("output_fingerprint") or {}).get("size_bytes"),
        "evidence_artifact_count": len(qa.get("evidence_index") or []),
        "output_path": job.get("output_path"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--travel", type=Path, action="append", required=True)
    parser.add_argument("--coffee", type=Path, action="append", required=True)
    args = parser.parse_args()
    if len(args.travel) < 2 or len(args.coffee) < 2:
        parser.error("provide at least two --travel and two --coffee sources")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required")
    sources = [path.expanduser().resolve() for path in [*args.travel, *args.coffee]]
    if any(not path.is_file() for path in sources):
        raise FileNotFoundError("one or more real acceptance sources are unavailable")
    root = args.root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("--root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    if any(root == source or root in source.parents for source in sources):
        raise RuntimeError("isolated root cannot contain source media")

    before = [_source_state(path) for path in sources]
    cfg = {
        "library_root": str(root / "library"),
        "ffmpeg_path": ffmpeg,
        "ffprobe_path": ffprobe,
        "delivery_qa": {"contract_version": "delivery-qa-v1", "timeout_seconds": 600, "threshold_overrides": {}, "profiles": {}},
    }
    library = Path(cfg["library_root"])
    library.mkdir(parents=True)
    db = library / "05_index" / "video_vault.sqlite3"
    init_db(db)

    projects = [
        _create_project(cfg, db, root, slug="travel", category="travel", content_type="travel_diary", sources=sources[:2]),
        _create_project(cfg, db, root, slug="coffee-matcha", category="coffee", content_type="coffee_matcha_diary", sources=sources[2:4]),
    ]
    after = [_source_state(path) for path in sources]
    source_unchanged = before == after
    if not source_unchanged:
        raise RuntimeError("source media changed during isolated acceptance")
    result = {
        "acceptance": "VID-7 Windows real multi-clip formal render and Delivery QA",
        "isolated": True,
        "source_count": len(sources),
        "source_media_unchanged": source_unchanged,
        "production_user_data_modified": False,
        "projects": projects,
    }
    result_path = root / "vid7-acceptance.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**result, "acceptance_result_path": str(result_path), "config": cfg, "database": str(db)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
