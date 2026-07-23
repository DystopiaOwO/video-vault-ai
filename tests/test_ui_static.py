from pathlib import Path

from video_vault import ui
from video_vault.ui import JOBS, JOBS_LOCK, _kill_video_vault_processes, _set_job, _static_file, _web_dist, cancel_legacy_job, project_jobs, stop_project_jobs


REPO_ROOT = Path(__file__).parents[1]
WEB_ROOT = REPO_ROOT / "web"
WEB_SRC = WEB_ROOT / "src"
APP_SOURCE = WEB_SRC / "App.tsx"


def test_application_entry_mounts_and_reexports_the_workspace_app():
    entry = (WEB_SRC / "main.tsx").read_text(encoding="utf-8")
    app = APP_SOURCE.read_text(encoding="utf-8")

    assert 'export { App } from "./App";' in entry
    assert "createRoot(rootElement)" in entry
    assert "export function App()" in app
    assert "createRoot(rootElement)" not in app


def test_application_shell_is_linked_and_scoped_to_root_layout():
    index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    shell = (WEB_SRC / "app-shell.css").read_text(encoding="utf-8")
    navigation = (WEB_SRC / "project-navigation.css").read_text(encoding="utf-8")
    stripped_lines = {line.strip() for line in shell.splitlines()}

    assert '<link rel="stylesheet" href="/src/app-shell.css" />' in index
    assert '<link rel="stylesheet" href="/src/project-navigation.css" />' in index
    assert "<title>Video Vault AI</title>" in index
    assert "#root > main" in shell
    assert "#root > main > aside" in shell
    assert "#root > main > section" in shell
    assert "#root .review-workspace { padding: 0; }" in shell
    assert "#root > main > section .workspace-nav" in navigation
    assert "#root > main > aside .project-search" in navigation
    assert "aside {" not in stripped_lines
    assert "section {" not in stripped_lines


def test_project_workspace_exposes_search_loading_and_anchor_navigation():
    source = APP_SOURCE.read_text(encoding="utf-8")

    assert 'aria-label="搜尋專案"' in source
    assert "WorkspaceLoading" in source
    assert "WorkspaceEmpty" in source
    assert 'aria-label="專案工作區導覽"' in source
    assert "workspace-storyboard" in source
    assert "已有同名專案" in source


def test_audio_ui_exposes_polling_safe_drafts_and_preview_controls():
    app = APP_SOURCE.read_text(encoding="utf-8")
    workspace = (WEB_SRC / "workspaces" / "audio" / "AudioMixingWorkspace.tsx").read_text(encoding="utf-8")

    assert "<AudioMixingWorkspace" in app
    assert "有未儲存變更" in workspace
    assert "放棄變更" in workspace
    assert "beforeunload" in workspace
    assert "搜尋音訊片段" in workspace
    assert "忽略快取重跑" in workspace
    assert "AudioMixingPanel" not in app


def test_storyboard_ui_exposes_review_first_controls():
    app = APP_SOURCE.read_text(encoding="utf-8")
    workspace = (WEB_SRC / "workspaces" / "storyboard" / "StoryboardReviewWorkspace.tsx").read_text(encoding="utf-8")
    controller = (WEB_SRC / "workspaces" / "storyboard" / "StoryboardWorkspaceController.tsx").read_text(encoding="utf-8")

    assert "StoryboardWorkspaceController" in app
    assert "<StoryboardWorkspaceController" in app
    assert "StoryboardPanel" not in app

    assert "分鏡審核" in workspace
    assert "建立分鏡" in workspace
    assert "預覽前後銜接" in workspace
    assert "onMoveSegment" in workspace
    assert "片段 25%" in workspace

    assert "api.storyboardPreview" in controller
    assert "api.storyboardThumbnail" in controller


def test_static_file_serving_stays_inside_web_dist():
    dist = _web_dist()
    assert _static_file("/index.html") == dist / "index.html" if (dist / "index.html").exists() else True
    assert _static_file("/../config.yaml") is None


def test_project_jobs_tracks_percent_and_stop(monkeypatch):
    monkeypatch.setattr(ui, "_kill_video_vault_processes", lambda: None)
    with JOBS_LOCK:
        JOBS.clear()
    _set_job(7, "render", kind="輸出", status="running", done=1, total=4)

    job = project_jobs(7)[0]
    assert job["percent"] == 25

    stop_project_jobs(7)
    assert project_jobs(7)[0]["status"] == "stopped"


def test_cancel_legacy_job_is_scoped_to_one_key(monkeypatch):
    with JOBS_LOCK:
        JOBS.clear()
    _set_job(7, "analyze", kind="內容感知", status="running", done=1, total=2)
    _set_job(7, "color", kind="調色預覽", status="running", done=1, total=2)

    class GuardManager:
        def list(self, project_id):
            return [{"job_id": "formal-1", "status": "running", "project_id": 7}]

        def cancel_project(self, project_id):
            raise AssertionError("legacy cancellation must not cancel persistent Render Jobs")

    result = cancel_legacy_job(7, "analyze")

    assert result["ok"] is True
    assert result["job"]["legacy_job_key"] == "analyze"
    jobs = project_jobs(7, GuardManager())
    legacy_jobs = {job["legacy_job_key"]: job for job in jobs if "legacy_job_key" in job}
    assert legacy_jobs["analyze"]["status"] == "stopped"
    assert legacy_jobs["color"]["status"] == "running"
    assert next(job for job in jobs if job.get("job_id") == "formal-1")["status"] == "running"


def test_stop_processes_does_not_use_global_process_kill(monkeypatch):
    monkeypatch.setattr(ui.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("global process kill is forbidden")))
    _kill_video_vault_processes()
