from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import cgi
import json
import subprocess
import threading
import time

from .analyzer.vision_pipeline import analyze_video_frames
from .bgm import import_bgm, list_bgm
from .color import render_color_preview
from .database import add_frame, add_project_bgm, frames as db_frames, init_db, project_videos, set_project_videos, set_video_status, upsert_video, videos
from .ffmpeg_tools import extract_frames, frame_timestamp, metadata
from .hyperframes import export_hyperframes_project, render_fast_draft
from .naming import rename_after_perception
from .opencut import OPENCUT_URL, export_opencut_handoff, opencut_status, start_opencut
from .paths import db_path
from .planner import draft_plan, perceive_output, review_text, revise_plan, set_plan_status, video_dir, write_plan_files
from .project import build_project_plan, create_project, list_projects, project_detail, project_dir, set_review_status, sync_project_files
from .job_api import cancel_render_job, get_render_job, list_render_jobs, list_render_outputs, start_render_job
from .render_api import RenderApiError, compile_project, preflight_project, render_settings, update_render_settings
from .renderer import render_approved
from .scanner import scan_inbox


JOBS: dict[tuple[int, str], dict] = {}
JOBS_LOCK = threading.Lock()


def run_ui(cfg: dict, host: str = "127.0.0.1", port: int = 8765) -> None:
    db = db_path(cfg)
    init_db(db)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                project_id = int(query.get("project_id", ["0"])[0] or 0)
                self._html(render_page(cfg, db, project_id, query.get("message", [""])[0]))
            elif parsed.path == "/bgm":
                self._html(render_bgm_page(db, query.get("message", [""])[0]))
            elif parsed.path == "/api/projects":
                self._json(list_projects(db))
            elif parsed.path == "/api/project":
                self._json(project_detail(cfg, db, int(query.get("id", ["0"])[0])))
            elif parsed.path == "/api/videos":
                self._json(video_list(cfg, db))
            elif parsed.path == "/api/bgm":
                self._json(list_bgm(db))
            elif parsed.path == "/api/project/render/settings":
                self._json({"ok": True, "settings": render_settings(cfg, int(query.get("project_id", ["0"])[0]))})
            elif parsed.path == "/api/project/render/jobs":
                self._json({"ok": True, "jobs": list_render_jobs(cfg, int(query.get("project_id", ["0"])[0]))})
            elif parsed.path == "/api/project/render/job":
                job = get_render_job(cfg, int(query.get("project_id", ["0"])[0]), query.get("job_id", [""])[0])
                self._json({"ok": job is not None, "job": job})
            elif parsed.path == "/api/project/render/outputs":
                self._json({"ok": True, "outputs": list_render_outputs(cfg, int(query.get("project_id", ["0"])[0]))})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/ui/"):
                self._ui_post(parsed.path)
                return
            if parsed.path == "/api/upload":
                self._json(upload(self, cfg))
                return
            if parsed.path == "/api/project/upload":
                self._json(upload_project(self, cfg, db))
                return
            if parsed.path == "/api/upload-bgm":
                self._json(upload_bgm(self, cfg, db))
                return
            data = self._json_body()
            self._api_post(parsed.path, data)

        def _ui_post(self, path: str) -> None:
            if path in {"/ui/upload-project", "/ui/upload-bgm"}:
                if path == "/ui/upload-project":
                    result = upload_project(self, cfg, db)
                    project_id = int(result.get("project_id") or 0)
                    self._redirect(project_id, f"已匯入 {len(result.get('files', []))} 支素材")
                else:
                    upload_bgm(self, cfg, db)
                    self._redirect(0, "BGM 已登錄")
                return

            data = self._form_body()
            project_id = int(data.get("project_id", "0") or 0)
            try:
                if path == "/ui/create":
                    project_id = create_project(db, data.get("name", ""), [], category=data.get("category", "unknown"), content_type=data.get("content_type", "diary_montage"), platform="YouTube")
                    sync_project_files(cfg, db, project_id)
                    self._redirect(project_id, "專案已建立")
                elif path == "/ui/analyze-project":
                    started = start_analyze_job(cfg, db, project_id, data.get("force") == "1")
                    self._redirect(project_id, "內容感知已開始" if started else "內容感知已在執行中")
                elif path == "/ui/analyze-video":
                    analyze_project_video(cfg, db, project_id, int(data.get("video_id", 0)))
                    self._redirect(project_id, "單支素材感知完成")
                elif path == "/ui/build-plan":
                    build_project_plan(cfg, db, project_id)
                    self._redirect(project_id, "故事整理已更新")
                elif path == "/ui/approve":
                    set_review_status(cfg, db, project_id, "approved", data.get("notes", ""))
                    self._redirect(project_id, "專案已核准")
                elif path == "/ui/reject":
                    set_review_status(cfg, db, project_id, "rejected", data.get("notes", ""))
                    self._redirect(project_id, "專案已退回")
                elif path == "/ui/color-preview":
                    started = start_color_job(cfg, db, project_id, data.get("mode") or cfg.get("color", {}).get("default_mode", "safe_restore"))
                    self._redirect(project_id, "調色預覽已開始" if started else "調色預覽已在執行中")
                elif path == "/ui/opencut-export":
                    out = export_opencut_handoff(cfg, db, project_id, data.get("render_clips") == "1", int(data.get("max_segments", 20)))
                    self._redirect(project_id, f"OpenCut 匯出完成：{out}")
                elif path == "/ui/opencut-handoff":
                    started = start_opencut_job(cfg, db, project_id, data.get("render_clips") == "1", int(data.get("max_segments", 20)))
                    self._redirect(project_id, "正在準備 OpenCut" if started else "OpenCut 準備工作已在執行中")
                elif path == "/ui/opencut-folder":
                    out = Path(data.get("folder", ""))
                    if out.exists():
                        _open_folder(out)
                    self._redirect(project_id, f"已打開資料夾：{out}" if out.exists() else "找不到 OpenCut 素材包，請先準備素材")
                elif path == "/ui/opencut-start":
                    status = start_opencut()
                    self._redirect(project_id, "OpenCut 已啟動" if status.get("running") else f"OpenCut 啟動失敗：{status.get('error', '')}")
                elif path == "/ui/hyperframes-export":
                    started = start_hyperframes_job(cfg, db, project_id, data.get("render") == "1", int(data.get("max_segments", 20)))
                    self._redirect(project_id, "正在產生 HyperFrames 初剪" if started else "HyperFrames 工作已在執行中")
                elif path == "/ui/hyperframes-folder":
                    out = project_dir(cfg, project_id) / "output" / "hyperframes"
                    if out.exists():
                        _open_folder(out)
                    self._redirect(project_id, f"已打開資料夾：{out}" if out.exists() else "尚未產生 HyperFrames 專案")
                elif path == "/ui/stop-jobs":
                    stop_project_jobs(project_id)
                    self._redirect(project_id, "已停止目前背景工作")
                elif path == "/ui/project-bgm":
                    add_project_bgm(db, project_id, int(data.get("bgm_id", 0)))
                    self._redirect(project_id, "BGM 已加入本專案")
                else:
                    self.send_error(404)
            except Exception as exc:
                self._redirect(project_id, f"操作失敗：{exc}")

        def _api_post(self, path: str, data: dict) -> None:
            try:
                self._render_api_post(path, data)
            except RenderApiError as exc:
                self._json(exc.as_response())
            except (ValueError, KeyError, OSError) as exc:
                self._json({"ok": False, "error": {"code": "request_failed", "message": str(exc), "details": {}}})

        def _render_api_post(self, path: str, data: dict) -> None:
            project_id = int(data.get("project_id", 0) or 0)
            if path == "/api/project/render/settings":
                self._json({"ok": True, "settings": update_render_settings(cfg, db, project_id, data.get("settings", data))})
            elif path == "/api/project/render/compile":
                self._json(compile_project(cfg, db, project_id, data.get("settings")))
            elif path == "/api/project/render/validate":
                self._json(preflight_project(cfg, db, project_id, final=False, overrides=data.get("settings")))
            elif path == "/api/project/render/preview":
                self._json(start_render_job(cfg, db, project_id, "accurate_preview", data.get("settings")))
            elif path == "/api/project/render/final":
                self._json(start_render_job(cfg, db, project_id, "final", data.get("settings")))
            elif path == "/api/project/render/cancel":
                self._json(cancel_render_job(cfg, project_id, str(data.get("job_id", ""))))
            elif path == "/api/process-inbox":
                self._json(process_inbox(cfg, db))
            elif path == "/api/project/analyze":
                self._json(analyze_project(cfg, db, int(data.get("project_id", 0)), bool(data.get("force"))))
            elif path == "/api/project/analyze-video":
                self._json(analyze_project_video(cfg, db, int(data.get("project_id", 0)), int(data.get("video_id", 0))))
            elif path == "/api/projects":
                project_id = create_project(db, data.get("name", ""), [int(v) for v in data.get("video_ids", [])], category=data.get("category", "unknown"), content_type=data.get("content_type", "diary_montage"), platform=data.get("platform", "YouTube"), target_duration_seconds=float(data.get("target_duration_seconds") or 0))
                sync_project_files(cfg, db, project_id)
                self._json({"ok": True, "id": project_id})
            elif path == "/api/project/build-plan":
                self._json({"ok": True, "plan": build_project_plan(cfg, db, int(data.get("project_id", 0)))})
            elif path == "/api/project/bgm":
                add_project_bgm(db, int(data.get("project_id", 0)), int(data.get("bgm_id", 0)))
                self._json({"ok": True})
            elif path == "/api/project/color-preview":
                self._json(color_preview_project(cfg, db, int(data.get("project_id", 0)), data.get("mode") or cfg.get("color", {}).get("default_mode", "safe_restore")))
            elif path == "/api/project/opencut-export":
                out = export_opencut_handoff(cfg, db, int(data.get("project_id", 0)), bool(data.get("render_clips")), int(data.get("max_segments", 20)))
                self._json({"ok": True, "folder": str(out)})
            elif path == "/api/project/approve":
                set_review_status(cfg, db, int(data.get("project_id", 0)), "approved", data.get("notes", ""))
                self._json({"ok": True})
            elif path == "/api/project/reject":
                set_review_status(cfg, db, int(data.get("project_id", 0)), "rejected", data.get("notes", ""))
                self._json({"ok": True})
            elif path == "/api/project/render-dry-run":
                review = _read_json(Path(cfg["library_root"]) / "08_projects" / f"project_{data.get('project_id', 0)}" / "review_status.json")
                self._json({"ok": bool(review.get("approved_by_user")), "message": "已核准，可以進入輸出檢查" if review.get("approved_by_user") else "尚未核准，不能輸出"})
            else:
                self._video_post(path, data, db)

        def _video_post(self, path: str, data: dict, db: Path) -> None:
            video_id = int(data.get("video_id", 0))
            if path == "/api/approve":
                set_plan_status(cfg, video_id, "approved")
                set_video_status(db, video_id, "approved")
                self._json({"ok": True})
            elif path == "/api/reject":
                set_plan_status(cfg, video_id, "rejected", data.get("notes", ""))
                set_video_status(db, video_id, "rejected")
                self._json({"ok": True})
            elif path == "/api/revise":
                (video_dir(cfg, video_id) / "revision_prompt.txt").write_text(data.get("notes", ""), encoding="utf-8")
                revise_plan(cfg, video_id)
                set_video_status(db, video_id, "needs_review")
                self._json({"ok": True})
            elif path == "/api/render-dry-run":
                out = render_approved(cfg, video_id, dry_run=True)
                self._json({"ok": bool(out), "output": str(out) if out else ""})
            else:
                self.send_error(404)

        def _form_body(self) -> dict:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[-1] for k, v in parse_qs(body).items()}

        def _json_body(self) -> dict:
            length = int(self.headers.get("content-length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _redirect(self, project_id: int = 0, message: str = "") -> None:
            query = {}
            if project_id:
                query["project_id"] = str(project_id)
            if message:
                query["message"] = message
            self.send_response(303)
            self.send_header("location", "/" + ("?" + urlencode(query) if query else ""))
            self.end_headers()

        def _json(self, data: object) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    print(f"video-vault-ai UI: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def render_page(cfg: dict, db: Path, project_id: int = 0, message: str = "") -> str:
    projects = list_projects(db)
    if not project_id and projects:
        project_id = int(projects[0]["id"])
    detail = project_detail(cfg, db, project_id) if project_id else {}
    bgm = list_bgm(db)
    jobs = project_jobs(project_id) if project_id else []
    refresh = '<meta http-equiv="refresh" content="3">' if any(j.get("status") in {"running", "queued"} for j in jobs) else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  {refresh}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>video-vault-ai</title>
  <style>{_css()}</style>
</head>
<body>
  <header>video-vault-ai 專案工作台</header>
  <main>
    <aside>{_nav()}{_create_project_form()}{_project_list(projects, project_id)}</aside>
    <section>
      {f'<div class="card"><b>{h(message)}</b></div>' if message else ''}
      {_jobs_panel(jobs)}
      {_project_panel(detail)}
      {_hyperframes_panel(detail)}
      <div class="grid">{_clips_panel(detail)}{_project_bgm_panel(project_id, detail, bgm)}</div>
      {_script_panel(detail)}
    </section>
  </main>
</body>
</html>"""


def render_bgm_page(db: Path, message: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BGM 資料庫</title>
  <style>{_css()}</style>
</head>
<body>
  <header>video-vault-ai BGM 資料庫</header>
  <main>
    <aside>{_nav()}</aside>
    <section>
      {f'<div class="card"><b>{h(message)}</b></div>' if message else ''}
      {_bgm_library_panel(list_bgm(db))}
    </section>
  </main>
</body>
</html>"""


def _nav() -> str:
    return '<div class="card"><h3>導覽</h3><a class="item" href="/">專案工作台</a><a class="item" href="/bgm">BGM 資料庫</a></div>'


def _css() -> str:
    return """
    body{margin:0;font-family:Segoe UI,'Microsoft JhengHei',sans-serif;background:#f5f7fa;color:#17212b}
    header{height:56px;display:flex;align-items:center;padding:0 20px;background:#17212b;color:white;font-weight:700}
    main{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 56px)} aside{background:white;border-right:1px solid #d8dee6;overflow:auto}
    section{padding:18px} .card{background:white;border:1px solid #d8dee6;border-radius:8px;padding:14px;margin-bottom:12px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px} .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center} .stack{display:grid;gap:8px}
    .item{display:block;padding:12px 14px;border-bottom:1px solid #edf0f3;color:inherit;text-decoration:none} .item:hover,.active{background:#edf6ff}
    button,input,select,textarea{font:inherit} button,.buttonlink{border:1px solid #b8c0cc;background:white;border-radius:6px;padding:8px 10px;cursor:pointer;color:inherit;text-decoration:none;display:inline-block}
    .primary{background:#1769aa;color:white;border-color:#1769aa} .good{background:#16794c;color:white;border-color:#16794c} .danger{background:#b42318;color:white;border-color:#b42318}
    input,select,textarea{border:1px solid #b8c0cc;border-radius:6px;padding:8px;box-sizing:border-box} textarea{width:100%;min-height:72px}
    .muted{font-size:13px;color:#64748b} .pill{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700} .ok{background:#dcfce7;color:#166534} .wait{background:#e2e8f0;color:#475569}
    .bar{display:block;width:100%;height:8px;background:#e2e8f0;border-radius:999px;overflow:hidden;margin-top:6px}.bar span{display:block;height:100%;background:#1769aa}
    pre{white-space:pre-wrap;background:#fbfcfd;border:1px solid #d8dee6;border-radius:8px;padding:12px;line-height:1.45;max-height:420px;overflow:auto}
    @media(max-width:900px){main{grid-template-columns:1fr} aside{max-height:320px} .grid{grid-template-columns:1fr}}
  """


def start_analyze_job(cfg: dict, db: Path, project_id: int, force: bool) -> bool:
    key = (project_id, "analyze")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        JOBS[key] = {"kind": "內容感知", "status": "queued", "message": "等待開始", "done": 0, "total": 0, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_analyze_job, args=(cfg, db, project_id, force), daemon=True)
    thread.start()
    return True


def start_color_job(cfg: dict, db: Path, project_id: int, mode: str) -> bool:
    key = (project_id, "color")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        JOBS[key] = {"kind": "調色預覽", "status": "queued", "message": "等待開始", "done": 0, "total": 0, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_color_job, args=(cfg, db, project_id, mode), daemon=True)
    thread.start()
    return True


def start_opencut_job(cfg: dict, db: Path, project_id: int, render_clips: bool, max_segments: int) -> bool:
    key = (project_id, "opencut")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        JOBS[key] = {"kind": "OpenCut 交接", "status": "queued", "message": "等待開始", "done": 0, "total": 3, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_opencut_job, args=(cfg, db, project_id, render_clips, max_segments), daemon=True)
    thread.start()
    return True


def start_hyperframes_job(cfg: dict, db: Path, project_id: int, render: bool, max_segments: int) -> bool:
    key = (project_id, "hyperframes")
    with JOBS_LOCK:
        current = JOBS.get(key)
        if current and current.get("status") in {"queued", "running"}:
            return False
        JOBS[key] = {"kind": "HyperFrames 初剪", "status": "queued", "message": "等待開始", "done": 0, "total": 3 if render else 2, "percent": 0, "updated_at": time.time()}
    thread = threading.Thread(target=_hyperframes_job, args=(cfg, db, project_id, render, max_segments), daemon=True)
    thread.start()
    return True


def project_jobs(project_id: int) -> list[dict]:
    with JOBS_LOCK:
        return [dict(job, project_id=pid) for (pid, _), job in JOBS.items() if pid == project_id]


def stop_project_jobs(project_id: int) -> None:
    with JOBS_LOCK:
        for (pid, _), job in JOBS.items():
            if pid == project_id and job.get("status") in {"queued", "running"}:
                job.update(status="stopped", message="已由使用者停止", updated_at=time.time())
    _kill_video_vault_ffmpeg()


def _kill_video_vault_ffmpeg() -> None:
    pattern = "D:\\VideoLibrary"
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"name='ffmpeg.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{pattern}*'}} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _set_job(project_id: int, name: str, **changes: object) -> None:
    with JOBS_LOCK:
        job = JOBS.setdefault((project_id, name), {})
        job.update(changes)
        if "percent" not in changes:
            done = int(job.get("done") or 0)
            total = int(job.get("total") or 0)
            if total:
                job["percent"] = min(100, int(done * 100 / total))
        job["updated_at"] = time.time()


def _analyze_job(cfg: dict, db: Path, project_id: int, force: bool) -> None:
    try:
        rows = [dict(row) for row in project_videos(db, project_id)]
        todo = [row for row in rows if force or row.get("status") != "perceived"]
        _set_job(project_id, "analyze", status="running", message="正在準備內容感知", done=0, total=len(todo), percent=0)
        processed = []
        for index, video in enumerate(todo, 1):
            if _job_stopped(project_id, "analyze"):
                return
            _set_job(project_id, "analyze", message=f"正在分析 {video.get('filename', '')}", done=index - 1)
            analyze_ui_video(cfg, db, video)
            video = rename_after_perception(cfg, db, video)
            perceive_output(cfg, db, video)
            write_plan_files(cfg, draft_plan(cfg, db, video))
            set_video_status(db, int(video["id"]), "perceived")
            processed.append({"id": video["id"], "filename": video["filename"]})
            _set_job(project_id, "analyze", message=f"已完成 {video.get('filename', '')}", done=index)
        build_project_plan(cfg, db, project_id)
        _set_job(project_id, "analyze", status="done", message=f"內容感知完成：{len(processed)} 支", processed=processed, percent=100)
    except Exception as exc:
        _set_job(project_id, "analyze", status="failed", message=f"內容感知失敗：{exc}")


def _color_job(cfg: dict, db: Path, project_id: int, mode: str) -> None:
    try:
        rows = [dict(row) for row in project_videos(db, project_id)]
        out_dir = project_dir(cfg, project_id) / "output" / "color_previews"
        files = []
        _set_job(project_id, "color", status="running", message=f"正在準備調色預覽：{mode}", done=0, total=len(rows), percent=0)
        for index, video in enumerate(rows, 1):
            if _job_stopped(project_id, "color"):
                return
            _set_job(project_id, "color", message=f"正在調色 {video.get('filename', '')}", done=index - 1)
            out = out_dir / f"video_{video['id']}_{mode}.mp4"
            files.append(str(render_color_preview(Path(video["current_path"]), out, cfg, mode)))
            _set_job(project_id, "color", message=f"已完成 {video.get('filename', '')}", done=index)
        _set_job(project_id, "color", status="done", message=f"調色預覽完成：{len(files)} 支", files=files, percent=100)
    except Exception as exc:
        _set_job(project_id, "color", status="failed", message=f"調色預覽失敗：{exc}")


def _opencut_job(cfg: dict, db: Path, project_id: int, render_clips: bool, max_segments: int) -> None:
    try:
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", status="running", message="正在檢查並啟動 OpenCut", done=0, percent=5)
        status = start_opencut()
        if not status.get("running"):
            _set_job(project_id, "opencut", status="failed", message=f"OpenCut 啟動失敗：{status.get('error', '')}", done=0)
            return
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", message="正在產生 OpenCut 素材包", done=1)
        out = export_opencut_handoff(cfg, db, project_id, render_clips, max_segments)
        if _job_stopped(project_id, "opencut"):
            return
        _set_job(project_id, "opencut", message="正在打開素材包資料夾", done=2)
        _open_folder(out)
        _set_job(project_id, "opencut", status="done", message=f"OpenCut 已啟動，素材包已完成：{out}", done=3, folder=str(out), percent=100)
    except Exception as exc:
        _set_job(project_id, "opencut", status="failed", message=f"OpenCut 交接失敗：{exc}")


def _hyperframes_job(cfg: dict, db: Path, project_id: int, render: bool, max_segments: int) -> None:
    try:
        _set_job(project_id, "hyperframes", status="running", message="正在產生調色片段與 HyperFrames timeline", done=0, percent=5)
        out = export_hyperframes_project(cfg, db, project_id, True, max_segments)
        if _job_stopped(project_id, "hyperframes"):
            return
        _set_job(project_id, "hyperframes", message="正在打開初剪資料夾", done=1, percent=60 if render else 80)
        _open_folder(out)
        if render:
            _set_job(project_id, "hyperframes", message="正在快速輸出 story_draft_fast.mp4", done=2, percent=75)
            result = render_fast_draft(out, cfg)
            if not result["ok"]:
                _set_job(project_id, "hyperframes", status="failed", message=f"快速初剪輸出失敗：{result['stderr'][-500:]}", done=2, folder=str(out))
                return
        _set_job(project_id, "hyperframes", status="done", message=f"HyperFrames 初剪已完成：{out}", done=3 if render else 2, folder=str(out), percent=100)
    except Exception as exc:
        _set_job(project_id, "hyperframes", status="failed", message=f"HyperFrames 初剪失敗：{exc}")


def _job_stopped(project_id: int, name: str) -> bool:
    with JOBS_LOCK:
        return JOBS.get((project_id, name), {}).get("status") == "stopped"


def _jobs_panel(jobs: list[dict]) -> str:
    if not jobs:
        return ""
    lines = []
    for job in sorted(jobs, key=lambda j: j.get("updated_at", 0), reverse=True):
        status = job.get("status", "")
        done = int(job.get("done") or 0)
        total = int(job.get("total") or 0)
        percent = int(job.get("percent") or 0)
        progress = f"{done}/{total}｜{percent}%" if total else f"{percent}%"
        cls = "ok" if status == "done" else "wait"
        if status == "failed":
            cls = "danger"
        lines.append(f'<p><b>{h(job.get("kind", "工作"))}</b> <span class="pill {cls}">{h(status)}</span><br>{h(job.get("message", ""))}<br><span class="muted">{h(progress)}</span><span class="bar"><span style="width:{percent}%"></span></span></p>')
    stop = _button('/ui/stop-jobs', int(jobs[0].get("project_id") or 0), '停止目前工作', 'danger') if any(j.get("status") in {"queued", "running"} for j in jobs) else ""
    return '<div class="card"><h3>工作狀態</h3>' + "".join(lines) + stop + "</div>"


def _create_project_form() -> str:
    return """<div class="card"><h3>新增專案</h3>
<form class="stack" method="post" action="/ui/create">
<input name="name" placeholder="專案名稱">
<div class="row"><select name="category"><option value="coffee">咖啡</option><option value="matcha">抹茶</option><option value="travel">旅行</option><option value="mixed">混合</option><option value="unknown">未分類</option></select>
<select name="content_type"><option value="diary_montage">日常紀錄</option><option value="process_montage">過程剪輯</option><option value="travel_diary">旅行日記</option><option value="highlight">精華</option></select></div>
<button class="primary">建立專案</button>
</form></div>"""


def _project_list(projects: list[dict], current: int) -> str:
    items = []
    for p in projects:
        cls = "item active" if int(p["id"]) == current else "item"
        items.append(f'<a class="{cls}" href="/?project_id={p["id"]}"><b>{h(p["name"])}</b><div class="muted">#{p["id"]} | {h(p["status"])} | {p.get("video_count", 0)} clips</div></a>')
    return '<div class="card"><h3>專案</h3>' + ("".join(items) or '<p class="muted">目前沒有專案</p>') + "</div>"


def _project_panel(detail: dict) -> str:
    if not detail:
        return '<div class="card"><h2>尚未選擇專案</h2></div>'
    p = detail["project"]
    pid = int(p["id"])
    return f"""<div class="card">
<h2>{h(p['name'])}</h2>
<p class="muted">{h(detail['folder'])}</p>
<p class="muted">狀態：{h(p['status'])}</p>
<div class="row">
{_button('/ui/build-plan', pid, '更新故事整理')}
{_button('/ui/approve', pid, '核准專案', 'good')}
{_button('/ui/reject', pid, '退回修改', 'danger')}
</div>
<form method="post" action="/ui/reject"><input type="hidden" name="project_id" value="{pid}"><textarea name="notes" placeholder="審核備註或退回原因"></textarea></form>
</div>"""


def _clips_panel(detail: dict) -> str:
    if not detail:
        return '<div class="card"><h3>專案素材</h3><p class="muted">先建立或選擇專案。</p></div>'
    pid = int(detail["project"]["id"])
    clips = "".join(_clip_item(pid, c) for c in detail.get("clips", [])) or '<p class="muted">此專案尚無素材。</p>'
    return f"""<div class="card"><h3>專案素材</h3>
<form method="post" action="/ui/upload-project" enctype="multipart/form-data" class="stack"><input type="hidden" name="project_id" value="{pid}"><input name="file" type="file" multiple accept=".mp4,.mov,.m4v,video/*"><button>匯入到此專案</button></form>
<div class="row">{_button('/ui/analyze-project', pid, '跑待感知素材', 'primary')}{_button('/ui/analyze-project', pid, '全部重跑感知', '', {'force': '1'})}</div>
<form method="post" action="/ui/color-preview" class="row"><input type="hidden" name="project_id" value="{pid}"><select name="mode"><option value="dji_lut">DJI LUT</option><option value="safe_restore">保守修正</option><option value="warm_food">咖啡/食物暖色</option><option value="none">不調色</option></select><button>產生調色預覽</button></form>
{clips}</div>"""


def _opencut_panel(detail: dict) -> str:
    if not detail:
        return ""
    pid = int(detail["project"]["id"])
    status = opencut_status()
    state = "已啟動" if status["running"] else "未啟動"
    folder = Path(detail["folder"]) / "output" / "opencut_handoff"
    open_link = f'<a class="buttonlink primary" href="{OPENCUT_URL}" target="_blank">開啟 OpenCut</a>' if status["running"] else ""
    folder_button = _button('/ui/opencut-folder', pid, '打開素材包資料夾', '', {'folder': str(folder)}) if folder.exists() else ""
    return f"""<div class="card"><h3>OpenCut 剪輯</h3>
<p class="muted">狀態：{state}</p>
<div class="item"><b>建議流程</b><p class="muted">按「一鍵交給 OpenCut」後，OpenCut 會開在專案列表，Windows 也會打開素材包資料夾；把資料夾內影片拖進 OpenCut 即可。</p>
{_button('/ui/opencut-handoff', pid, '一鍵交給 OpenCut', 'primary', {'render_clips': '1'})}</div>
<div class="row">
{_button('/ui/opencut-export', pid, '只產生素材包')}
{_button('/ui/opencut-export', pid, '只產生調色片段', '', {'render_clips': '1'})}
{folder_button}
{_button('/ui/opencut-start', pid, '啟動 OpenCut')}
{open_link}
</div>
<p class="muted">匯出資料夾：{h(folder)}</p>
</div>"""


def _hyperframes_panel(detail: dict) -> str:
    if not detail:
        return ""
    pid = int(detail["project"]["id"])
    folder = Path(detail["folder"]) / "output" / "hyperframes"
    folder_button = _button('/ui/hyperframes-folder', pid, '打開初剪資料夾') if folder.exists() else ""
    preview = f'<span class="pill ok">已輸出 story_draft_fast.mp4</span>' if (folder / "story_draft_fast.mp4").exists() else ""
    return f"""<div class="card"><h3>HyperFrames 初剪</h3>
<div class="item"><b>主流程</b><p class="muted">照行程腳本自動串片、加地點字卡、套 BGM。先產生可預覽的 HTML timeline，需要成片再 render MP4。</p>
<div class="row">
{_button('/ui/hyperframes-export', pid, '產生初剪專案', 'primary')}
{_button('/ui/hyperframes-export', pid, '快速輸出 MP4', 'good', {'render': '1'})}
{folder_button}
{preview}
</div></div>
<p class="muted">輸出資料夾：{h(folder)}</p>
</div>"""


def _open_folder(path: Path) -> None:
    subprocess.Popen(["explorer", str(path)])


def _clip_item(project_id: int, clip: dict) -> str:
    done = clip.get("status") == "perceived"
    segment_count = int(clip.get("segment_count") or 0)
    label = f"已感知｜{segment_count} 段" if done and segment_count else ("已感知｜無推薦片段" if done else "待感知")
    pill = f'<span class="pill {"ok" if done else "wait"}">{label}</span>'
    return f"""<div class="item"><div class="row"><b>{h(clip['clip_id'])}</b>{pill}{_button('/ui/analyze-video', project_id, '跑感知', '', {'video_id': clip['video_id']})}</div>
<div>{h(clip['filename'])}</div><div class="muted">{round(float(clip.get('duration_seconds') or 0))}s | {h(clip.get('detected_category', 'unknown'))} | {h(clip.get('time_of_day', ''))}</div></div>"""


def _project_bgm_panel(project_id: int, detail: dict, bgm: list[dict]) -> str:
    used = detail.get("bgm", []) if detail else []
    options = "".join(f'<option value="{track["id"]}">#{track["id"]} {h(track["title"])} - {h(track.get("artist", ""))}</option>' for track in bgm)
    lines = "\n\n".join(f"#{track['id']} {track['title']} - {track.get('artist', '')}\n{track.get('attribution_text', '')}" for track in used) or "未指定 BGM"
    return f"""<div class="card"><h3>本專案 BGM</h3>
<form method="post" action="/ui/project-bgm" class="row"><input type="hidden" name="project_id" value="{project_id}"><select name="bgm_id">{options}</select><button>加入本專案</button></form>
<pre>{h(lines)}</pre></div>"""


def _script_panel(detail: dict) -> str:
    text = detail.get("script") if detail else ""
    return f'<div class="card"><h3>故事整理</h3><pre>{h(text or "尚未產生故事整理。")}</pre></div>'


def _bgm_library_panel(bgm: list[dict]) -> str:
    rows = "\n\n".join(f"#{t['id']} {t['title']} - {t.get('artist', '')}\n{t.get('license_name', '')} {t.get('source_url', '')}" for t in bgm) or "尚無 BGM"
    return f"""<div class="card"><h3>BGM 資料庫總覽</h3>
<form method="post" action="/ui/upload-bgm" enctype="multipart/form-data" class="stack"><div class="row"><input name="file" type="file" accept="audio/*"><input name="title" placeholder="曲名"><input name="artist" placeholder="作者"><input name="source_url" placeholder="來源 URL"><input name="license_name" placeholder="授權"><button>登錄 BGM</button></div><textarea name="attribution_text" placeholder="署名文字"></textarea></form>
<pre>{h(rows)}</pre></div>"""


def _button(action: str, project_id: int, label: str, cls: str = "", fields: dict | None = None) -> str:
    hidden = f'<input type="hidden" name="project_id" value="{project_id}">'
    for key, value in (fields or {}).items():
        hidden += f'<input type="hidden" name="{h(str(key))}" value="{h(str(value))}">'
    return f'<form method="post" action="{action}">{hidden}<button class="{cls}">{h(label)}</button></form>'


def h(value: object) -> str:
    return escape(str(value or ""), quote=True)


def video_list(cfg: dict, db: Path) -> list[dict]:
    result = []
    for video in videos(db):
        folder = video_dir(cfg, int(video["id"]))
        review = _read_json(folder / "review_status.json")
        result.append({"id": video["id"], "filename": video["filename"], "category": video["category"], "duration_seconds": video["duration_seconds"], "status": review.get("status") or video["status"], "approved": review.get("approved_by_user", False)})
    return result


def upload(handler: BaseHTTPRequestHandler, cfg: dict) -> dict:
    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": handler.headers["content-type"]})
    items = form["file"] if isinstance(form["file"], list) else [form["file"]]
    saved = []
    for item in items:
        name = Path(item.filename).name
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".m4v"}:
            continue
        out = Path(cfg["library_root"]) / cfg["inbox_dir"] / name
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as f:
            while chunk := item.file.read(1024 * 1024):
                f.write(chunk)
        saved.append(str(out))
    return {"ok": bool(saved), "files": saved}


def upload_project(handler: BaseHTTPRequestHandler, cfg: dict, db: Path) -> dict:
    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": handler.headers["content-type"]})
    project_id = int(form.getvalue("project_id") or 0)
    if not project_id:
        return {"ok": False, "error": "project_id required", "project_id": 0}
    source_dir = project_dir(cfg, project_id) / "source"
    items = form["file"] if isinstance(form["file"], list) else [form["file"]]
    existing = [int(v["id"]) for v in project_videos(db, project_id)]
    saved = []
    for item in items:
        name = Path(item.filename).name
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".m4v"}:
            continue
        out = source_dir / name
        with out.open("wb") as f:
            while chunk := item.file.read(1024 * 1024):
                f.write(chunk)
        video_id = upsert_video(db, {"original_path": str(out), "current_path": str(out), "filename": out.name, "category": "unknown", **metadata(out, cfg), "status": "uploaded"})
        existing.append(video_id)
        saved.append(str(out))
    set_project_videos(db, project_id, list(dict.fromkeys(existing)))
    sync_project_files(cfg, db, project_id)
    return {"ok": bool(saved), "files": saved, "project_id": project_id}


def upload_bgm(handler: BaseHTTPRequestHandler, cfg: dict, db: Path) -> dict:
    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": handler.headers["content-type"]})
    item = form["file"]
    tmp = Path(cfg["library_root"]) / "04_audio" / "_incoming_bgm" / Path(item.filename).name
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as f:
        while chunk := item.file.read(1024 * 1024):
            f.write(chunk)
    info = {k: str(form.getvalue(k) or "") for k in ("title", "artist", "source_url", "license_name", "license_url", "attribution_text", "mood")}
    info["attribution_required"] = str(form.getvalue("attribution_required") or "") == "on"
    return {"ok": True, "id": import_bgm(cfg, db, tmp, info)}


def process_inbox(cfg: dict, db: Path) -> dict:
    processed = []
    for path in scan_inbox(cfg):
        video_id = upsert_video(db, {"original_path": str(path), "current_path": str(path), "filename": path.name, "category": "unknown", **metadata(path, cfg), "status": "ingested"})
        video = dict(next(v for v in videos(db) if int(v["id"]) == video_id))
        analyze_ui_video(cfg, db, video)
        video = rename_after_perception(cfg, db, video)
        perceive_output(cfg, db, video)
        write_plan_files(cfg, draft_plan(cfg, db, video))
        set_video_status(db, video_id, "perceived")
        processed.append({"id": video_id, "filename": video["filename"]})
    return {"ok": True, "processed": processed}


def analyze_project(cfg: dict, db: Path, project_id: int, force: bool = False) -> dict:
    processed = []
    for row in project_videos(db, project_id):
        video = dict(row)
        if not force and video.get("status") == "perceived":
            continue
        analyze_ui_video(cfg, db, video)
        video = rename_after_perception(cfg, db, video)
        perceive_output(cfg, db, video)
        write_plan_files(cfg, draft_plan(cfg, db, video))
        set_video_status(db, int(video["id"]), "perceived")
        processed.append({"id": video["id"], "filename": video["filename"]})
    build_project_plan(cfg, db, project_id)
    return {"ok": True, "processed": processed}


def analyze_project_video(cfg: dict, db: Path, project_id: int, video_id: int) -> dict:
    video = next((dict(v) for v in project_videos(db, project_id) if int(v["id"]) == video_id), None)
    if not video:
        return {"ok": False, "error": "video not found in project"}
    analyze_ui_video(cfg, db, video)
    video = rename_after_perception(cfg, db, video)
    perceive_output(cfg, db, video)
    write_plan_files(cfg, draft_plan(cfg, db, video))
    set_video_status(db, video_id, "perceived")
    build_project_plan(cfg, db, project_id)
    return {"ok": True, "processed": [{"id": video_id, "filename": video["filename"]}]}


def color_preview_project(cfg: dict, db: Path, project_id: int, mode: str) -> dict:
    out_dir = project_dir(cfg, project_id) / "output" / "color_previews"
    files = []
    for video in project_videos(db, project_id):
        out = out_dir / f"video_{video['id']}_{mode}.mp4"
        files.append(str(render_color_preview(Path(video["current_path"]), out, cfg, mode)))
    return {"ok": True, "mode": mode, "files": files}


def analyze_ui_video(cfg: dict, db: Path, video: dict) -> None:
    if not db_frames(db, int(video["id"])):
        out_dir = Path(cfg["library_root"]) / "03_frames" / Path(video["filename"]).stem
        for frame in extract_frames(Path(video["current_path"]), out_dir, cfg):
            add_frame(db, int(video["id"]), frame, frame_timestamp(frame, cfg))
    analyze_video_frames(db, video, cfg)


def video_detail(cfg: dict, video_id: int) -> dict:
    folder = video_dir(cfg, video_id)
    return {"plan": _read_json(folder / "edit_plan.json"), "review": _read_json(folder / "review_status.json"), "script": review_text(cfg, video_id) if (folder / "edit_script.md").exists() else "", "perception": _read_json(folder / "perception.json")}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
