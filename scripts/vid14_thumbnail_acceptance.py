"""VID-14 isolated Windows large-source storyboard/status API acceptance."""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import multiprocessing
import os
import socket
import threading
import time
from urllib.request import urlopen

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.source_fingerprint import reset_source_fingerprint_cache, source_fingerprint_metrics, source_stat
from video_vault.storyboard import generate_storyboard
from video_vault.ui import run_ui


def _serve(cfg: dict, port: int, ready, stop, metrics_pipe) -> None:
    """Run UI and return metrics from the actual server process."""

    reset_source_fingerprint_cache()
    server_error: BaseException | None = None

    def run() -> None:
        nonlocal server_error
        try:
            run_ui(cfg, "127.0.0.1", port)
        except BaseException as exc:  # pragma: no cover - exercised by acceptance failures
            server_error = exc

    thread = threading.Thread(target=run, name="vid14-ui", daemon=True)
    thread.start()
    try:
        _wait_for_port(port)
        ready.set()
        stop.wait()
    except BaseException as exc:  # pragma: no cover - exercised by acceptance failures
        server_error = exc
        ready.set()
    finally:
        message = {
            "pid": os.getpid(),
            "metrics": source_fingerprint_metrics(),
        }
        if server_error is not None:
            message["error"] = repr(server_error)
        metrics_pipe.send(message)
        metrics_pipe.close()


def _wait_for_port(port: int) -> None:
    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"UI did not listen on {port}")


def _get_json(url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    with urlopen(url, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, round((time.perf_counter() - started) * 1000, 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--isolated-root", required=True)
    parser.add_argument("--port", type=int, default=18714)
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve()
    isolated_root = Path(args.isolated_root).expanduser().resolve()
    isolated_source = isolated_root / "08_projects" / "project_1" / "source" / source.name
    isolated_source.parent.mkdir(parents=True, exist_ok=True)
    if isolated_source.exists():
        isolated_source.unlink()
    isolated_source.hardlink_to(source)
    before = {"path": str(source), **source_stat(source)}

    db = isolated_root / "05_index" / "video_vault.sqlite3"
    init_db(db)
    video_id = upsert_video(
        db,
        {
            "original_path": str(isolated_source),
            "current_path": str(isolated_source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 326.890667,
            "status": "uploaded",
        },
    )
    segments = [
        {
            "start_seconds": round(index * 15.0, 3),
            "end_seconds": round(index * 15.0 + 8.0, 3),
            "segment_type": "detail",
            "title": f"Coffee segment {index + 1}",
            "reason": "VID-14 large-source acceptance",
            "tags": ["coffee"],
            "score": 0.9,
            "suggested_use": "main",
        }
        for index in range(20)
    ]
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {"segments": segments},
        isolated_root / "raw.json",
    )
    cfg = {
        "library_root": str(isolated_root),
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
    }
    project_id = create_project(db, "VID-14 isolated large source", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)

    reset_source_fingerprint_cache()
    generation_started = time.perf_counter()
    storyboard = generate_storyboard(cfg, db, project_id)
    generation_ms = round((time.perf_counter() - generation_started) * 1000, 3)
    generation_metrics = source_fingerprint_metrics()

    reset_source_fingerprint_cache()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop = context.Event()
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(target=_serve, args=(cfg, args.port, ready, stop, child_pipe), daemon=True)
    process.start()
    server_message: dict = {}
    try:
        if not ready.wait(35):
            raise TimeoutError(f"UI did not listen on {args.port}")
        storyboard_api, storyboard_ms = _get_json(f"http://127.0.0.1:{args.port}/api/project/storyboard?project_id={project_id}")
        status_api, status_ms = _get_json(f"http://127.0.0.1:{args.port}/api/project?id={project_id}")
    finally:
        stop.set()
        if parent_pipe.poll(20):
            server_message = parent_pipe.recv()
        else:
            server_message = {"error": "server did not return metrics before timeout"}
        parent_pipe.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    if server_message.get("error"):
        raise RuntimeError(server_message["error"])
    api_metrics = dict(server_message.get("metrics") or {})
    if int(api_metrics.get("full_hash_calls", -1)) != 0:
        raise AssertionError(f"storyboard/status API hashed source in server process: {api_metrics}")

    after = {"path": str(source), **source_stat(source)}
    report = {
        "acceptance": "VID-14 Windows large-source storyboard/status API",
        "source": before,
        "source_after": after,
        "source_unchanged": before == after,
        "isolated_root": str(isolated_root),
        "project_id": project_id,
        "video_id": video_id,
        "segment_count": len(storyboard.get("segments") or {}),
        "generation_latency_ms": generation_ms,
        "generation_fingerprint_metrics": generation_metrics,
        "storyboard_api_latency_ms": storyboard_ms,
        "status_api_latency_ms": status_ms,
        "storyboard_api_segment_count": len(storyboard_api.get("segments") or {}),
        "status_api_storyboard_segment_count": len((status_api.get("storyboard") or {}).get("segments") or {}),
        "api_fingerprint_metrics": api_metrics,
        "api_metrics_source": "server_child_process_pipe",
        "server_pid": server_message.get("pid"),
        "production_data_modified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
