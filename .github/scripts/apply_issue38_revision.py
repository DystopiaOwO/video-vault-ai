from __future__ import annotations

from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise SystemExit(f"missing patch target: {label}")
    return text.replace(old, new, 1)


def patch_database() -> None:
    path = Path("src/video_vault/database.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "  status text default 'draft',\n  created_at text default current_timestamp,\n",
        "  status text default 'draft',\n  project_revision integer default 0,\n  created_at text default current_timestamp,\n",
        "projects schema revision",
    )
    text = replace_once(
        text,
        '''                "target_duration_seconds": "real default 0",
            },
''',
        '''                "target_duration_seconds": "real default 0",
                "project_revision": "integer default 0",
            },
''',
        "projects migration revision",
    )
    path.write_text(text, encoding="utf-8")


def patch_ui() -> None:
    path = Path("src/video_vault/ui.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from .audio_state import audio_state_for_api, update_audio_state\n",
        "from .audio_state import audio_state_for_api, load_audio_state, update_audio_state\n",
        "audio load import",
    )
    text = replace_once(
        text,
        "from .color_consistency import ColorReferenceError, analyze_project_color, color_state_for_api, preview_file_path, reference_file_path, render_project_color_previews, set_color_reference, update_color_state\n",
        "from .color_consistency import ColorReferenceError, analyze_project_color, color_state_for_api, load_project_color_state, preview_file_path, reference_file_path, render_project_color_previews, set_color_reference, update_color_state\n",
        "color load import",
    )
    text = replace_once(
        text,
        "from .project_perception import run_project_perception\n",
        "from .project_perception import run_project_perception\nfrom .project_mutation import ProjectConflict, project_mutation\n",
        "mutation import",
    )
    text = replace_once(
        text,
        '''            data = self._json_body()
            self._api_post(parsed.path, data)
''',
        '''            data = self._json_body()
            try:
                self._api_post(parsed.path, data)
            except ProjectConflict as exc:
                self._json(exc.as_dict(), status=409)
''',
        "post conflict handling",
    )
    text = replace_once(
        text,
        '''        def _json(self, data: object) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
''',
        '''        def _json(self, data: object, *, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
''',
        "json status",
    )
    old_start = '''            elif path == "/api/project/clip-summary":
                project_id = int(data.get("project_id", 0))
                user_summary = data.get("user_summary", data.get("summary", ""))
                ok = update_clip_summary(cfg, db, project_id, int(data.get("video_id", 0)), str(user_summary))
                self._json({"ok": ok, "plan_rebuilt": ok})
'''
    new_start = '''            elif path == "/api/project/clip-summary":
                project_id = int(data.get("project_id", 0))
                user_summary = str(data.get("user_summary", data.get("summary", ""))).strip()
                current_row = next((dict(row) for row in project_videos(db, project_id) if int(row["id"]) == int(data.get("video_id", 0))), None)
                changed = bool(current_row is not None and str(current_row.get("user_summary") or "") != user_summary)
                mutation = _api_project_mutation(db, project_id, data, "clip-summary")
                with mutation:
                    ok = update_clip_summary(cfg, db, project_id, int(data.get("video_id", 0)), user_summary) if changed else current_row is not None
                    mutation.mark_changed(ok and changed)
                self._json({"ok": ok, "plan_rebuilt": ok and changed, "project_revision": mutation.committed_revision})
'''
    text = replace_once(text, old_start, new_start, "clip summary mutation")
    text = replace_once(
        text,
        '''            elif path == "/api/project/build-plan":
                self._json({"ok": True, "plan": build_project_plan(cfg, db, int(data.get("project_id", 0)))})
''',
        '''            elif path == "/api/project/build-plan":
                project_id = int(data.get("project_id", 0))
                mutation = _api_project_mutation(db, project_id, data, "build-plan")
                with mutation:
                    plan = build_project_plan(cfg, db, project_id)
                    mutation.mark_changed()
                self._json({"ok": True, "plan": plan, "project_revision": mutation.committed_revision})
''',
        "build plan mutation",
    )
    text = replace_once(
        text,
        '''            elif path == "/api/project/revise":
                project_id = int(data.get("project_id", 0))
                save_revision_notes(cfg, project_id, data.get("notes", ""))
                self._json({"ok": True, "plan": build_project_plan(cfg, db, project_id)})
''',
        '''            elif path == "/api/project/revise":
                project_id = int(data.get("project_id", 0))
                mutation = _api_project_mutation(db, project_id, data, "revise")
                with mutation:
                    save_revision_notes(cfg, project_id, data.get("notes", ""))
                    plan = build_project_plan(cfg, db, project_id)
                    mutation.mark_changed()
                self._json({"ok": True, "plan": plan, "project_revision": mutation.committed_revision})
''',
        "revise mutation",
    )
    text = replace_once(
        text,
        '''                    project_id = int(data.get("project_id", 0))
                    self._json({"ok": True, "path": str(save_segment_review(cfg, db, project_id, data.get("segments", [])))})
''',
        '''                    project_id = int(data.get("project_id", 0))
                    mutation = _api_project_mutation(db, project_id, data, "segment-review")
                    with mutation:
                        output_path = save_segment_review(cfg, db, project_id, data.get("segments", []))
                        mutation.mark_changed()
                    self._json({"ok": True, "path": str(output_path), "project_revision": mutation.committed_revision})
''',
        "segment review mutation",
    )
    text = replace_once(
        text,
        '''                    output_path = update_segment_timing(
                        cfg,
                        db,
                        project_id,
                        str(data.get("segment_id") or ""),
                        float(data.get("start_seconds")),
                        float(data.get("end_seconds")),
                        float(data.get("speed")),
                    )
                    self._json({"ok": True, "path": str(output_path)})
''',
        '''                    mutation = _api_project_mutation(db, project_id, data, "segment-timing")
                    with mutation:
                        output_path = update_segment_timing(
                            cfg,
                            db,
                            project_id,
                            str(data.get("segment_id") or ""),
                            float(data.get("start_seconds")),
                            float(data.get("end_seconds")),
                            float(data.get("speed")),
                        )
                        mutation.mark_changed()
                    self._json({"ok": True, "path": str(output_path), "project_revision": mutation.committed_revision})
''',
        "segment timing mutation",
    )
    text = replace_once(
        text,
        '''                    project_id = int(data.get("project_id", 0))
                    result = update_storyboard(cfg, db, project_id, data.get("state", data), return_result=True)
                    self._json({"ok": True, "storyboard": result["state"], "render_changed": result["render_changed"], "approval_invalidated": result["approval_invalidated"]})
''',
        '''                    project_id = int(data.get("project_id", 0))
                    before = storyboard_for_api(cfg, db, project_id)
                    mutation = _api_project_mutation(db, project_id, data, "storyboard")
                    with mutation:
                        result = update_storyboard(cfg, db, project_id, data.get("state", data), return_result=True)
                        mutation.mark_changed(result["state"] != before)
                    self._json({"ok": True, "storyboard": result["state"], "render_changed": result["render_changed"], "approval_invalidated": result["approval_invalidated"], "project_revision": mutation.committed_revision})
''',
        "storyboard mutation",
    )
    text = replace_once(
        text,
        '''                    state = update_audio_state(cfg, db, project_id, patch if isinstance(patch, dict) else {})
                    self._json({"ok": True, "state": audio_state_for_api(cfg, project_id, db)})
''',
        '''                    before = load_audio_state(cfg, project_id)
                    mutation = _api_project_mutation(db, project_id, data, "audio-settings")
                    with mutation:
                        state = update_audio_state(cfg, db, project_id, patch if isinstance(patch, dict) else {})
                        mutation.mark_changed(state != before)
                    self._json({"ok": True, "state": audio_state_for_api(cfg, project_id, db), "project_revision": mutation.committed_revision})
''',
        "audio mutation",
    )
    text = replace_once(
        text,
        '''            elif path == "/api/project/bgm":
                add_project_bgm(db, int(data.get("project_id", 0)), int(data.get("bgm_id", 0)))
                mark_project_needs_review(cfg, db, int(data.get("project_id", 0)))
                self._json({"ok": True})
''',
        '''            elif path == "/api/project/bgm":
                project_id = int(data.get("project_id", 0))
                bgm_id = int(data.get("bgm_id", 0))
                before = {int(row["id"]) for row in project_bgm_tracks(db, project_id)}
                mutation = _api_project_mutation(db, project_id, data, "project-bgm")
                with mutation:
                    add_project_bgm(db, project_id, bgm_id)
                    changed = bgm_id not in before
                    if changed:
                        mark_project_needs_review(cfg, db, project_id)
                    mutation.mark_changed(changed)
                self._json({"ok": True, "project_revision": mutation.committed_revision})
''',
        "bgm mutation",
    )
    text = replace_once(
        text,
        '''                    patch = data.get("state") if isinstance(data.get("state"), dict) else data.get("patch", {})
                    state = update_color_state(cfg, db, project_id, patch if isinstance(patch, dict) else {})
                    self._json({"ok": True, "state": color_state_for_api(cfg, int(data.get("project_id", 0)), state)})
''',
        '''                    patch = data.get("state") if isinstance(data.get("state"), dict) else data.get("patch", {})
                    before = load_project_color_state(cfg, project_id)
                    mutation = _api_project_mutation(db, project_id, data, "color-settings")
                    with mutation:
                        state = update_color_state(cfg, db, project_id, patch if isinstance(patch, dict) else {})
                        mutation.mark_changed(state != before)
                    self._json({"ok": True, "state": color_state_for_api(cfg, project_id, state), "project_revision": mutation.committed_revision})
''',
        "color settings mutation",
    )
    text = replace_once(
        text,
        '''                    state = set_color_reference(cfg, db, int(data.get("project_id", 0)), str(data.get("reference_id", "")))
                    self._json({"ok": True, "state": color_state_for_api(cfg, int(data.get("project_id", 0)), state)})
''',
        '''                    project_id = int(data.get("project_id", 0))
                    before = load_project_color_state(cfg, project_id)
                    mutation = _api_project_mutation(db, project_id, data, "color-reference")
                    with mutation:
                        state = set_color_reference(cfg, db, project_id, str(data.get("reference_id", "")))
                        mutation.mark_changed(state != before)
                    self._json({"ok": True, "state": color_state_for_api(cfg, project_id, state), "project_revision": mutation.committed_revision})
''',
        "color reference mutation",
    )
    text = replace_once(
        text,
        '''            elif path == "/api/project/approve":
                set_review_status(cfg, db, int(data.get("project_id", 0)), "approved", data.get("notes", ""))
                self._json({"ok": True})
            elif path == "/api/project/reject":
                set_review_status(cfg, db, int(data.get("project_id", 0)), "rejected", data.get("notes", ""))
                self._json({"ok": True})
''',
        '''            elif path == "/api/project/approve":
                project_id = int(data.get("project_id", 0))
                mutation = _api_project_mutation(db, project_id, data, "approve")
                with mutation:
                    set_review_status(cfg, db, project_id, "approved", data.get("notes", ""))
                    mutation.mark_changed()
                self._json({"ok": True, "project_revision": mutation.committed_revision})
            elif path == "/api/project/reject":
                project_id = int(data.get("project_id", 0))
                mutation = _api_project_mutation(db, project_id, data, "reject")
                with mutation:
                    set_review_status(cfg, db, project_id, "rejected", data.get("notes", ""))
                    mutation.mark_changed()
                self._json({"ok": True, "project_revision": mutation.committed_revision})
''',
        "review status mutation",
    )
    marker = "\n\ndef _web_dist() -> Path:\n"
    helper = '''


def _api_project_mutation(db: Path, project_id: int, data: dict, reason: str):
    value = data.get("base_revision")
    if value is None or value == "":
        return project_mutation(db, project_id, None, reason=reason)
    try:
        expected = int(value)
    except (TypeError, ValueError) as exc:
        from .project_mutation import project_revision

        raise ProjectConflict(
            project_id,
            None,
            project_revision(db, project_id),
            code="invalid_project_revision",
            message="專案版本格式無效，請重新整理後再試。",
        ) from exc
    return project_mutation(db, project_id, expected, reason=reason)
'''
    if helper.strip() not in text:
        text = text.replace(marker, helper + marker, 1)
    path.write_text(text, encoding="utf-8")


def patch_api() -> None:
    path = Path("web/src/api.ts")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''  video_count?: number;
};
''',
        '''  video_count?: number;
  project_revision: number;
};
''',
        "project revision type",
    )
    text = replace_once(
        text,
        '''async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  projects: () => json<Project[]>("/api/projects"),
  project: (id: number) => json<ProjectDetail>(`/api/project?id=${id}`),
''',
        '''export class ApiError extends Error {
  constructor(public status: number, message: string, public data: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
  }
}

const projectRevisions = new Map<number, number>();

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    const data = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    throw new ApiError(res.status, String(data.error || `${res.status} ${res.statusText}`), data);
  }
  return payload as T;
}

function rememberProjectRevision(projectId: number, payload: unknown) {
  if (!payload || typeof payload !== "object") return;
  const revision = Number((payload as { project_revision?: unknown }).project_revision);
  if (Number.isInteger(revision) && revision >= 0) projectRevisions.set(projectId, revision);
}

async function projectDetail(projectId: number): Promise<ProjectDetail> {
  const detail = await json<ProjectDetail>(`/api/project?id=${projectId}`);
  projectRevisions.set(projectId, Number(detail.project.project_revision || 0));
  return detail;
}

async function projectPost<T>(url: string, projectId: number, payload: Record<string, unknown>): Promise<T> {
  const result = await json<T>(url, post({ ...payload, project_id: projectId, base_revision: projectRevisions.get(projectId) ?? 0 }));
  rememberProjectRevision(projectId, result);
  return result;
}

export const api = {
  projects: () => json<Project[]>("/api/projects"),
  project: (id: number) => projectDetail(id),
''',
        "api revision helpers",
    )
    replacements = {
        'json<{ ok: boolean; plan_rebuilt?: boolean }>("/api/project/clip-summary", post({ project_id: projectId, video_id: videoId, user_summary: userSummary }))': 'projectPost<{ ok: boolean; plan_rebuilt?: boolean; project_revision: number }>("/api/project/clip-summary", projectId, { video_id: videoId, user_summary: userSummary })',
        'json<{ ok: boolean; state?: ColorState; error?: string }>("/api/project/color-settings", post({ project_id: projectId, state }))': 'projectPost<{ ok: boolean; state?: ColorState; error?: string; project_revision: number }>("/api/project/color-settings", projectId, { state })',
        'json<{ ok: boolean; state?: ColorState; error?: string }>("/api/project/color-reference", post({ project_id: projectId, reference_id: referenceId }))': 'projectPost<{ ok: boolean; state?: ColorState; error?: string; project_revision: number }>("/api/project/color-reference", projectId, { reference_id: referenceId })',
        'json<{ ok: boolean }>("/api/project/build-plan", post({ project_id: projectId }))': 'projectPost<{ ok: boolean; project_revision: number }>("/api/project/build-plan", projectId, {})',
        'json<{ ok: boolean }>("/api/project/bgm", post({ project_id: projectId, bgm_id: bgmId }))': 'projectPost<{ ok: boolean; project_revision: number }>("/api/project/bgm", projectId, { bgm_id: bgmId })',
        'json<{ ok: boolean; state?: AudioState; error?: string }>("/api/project/audio-settings", post({ project_id: projectId, patch }))': 'projectPost<{ ok: boolean; state?: AudioState; error?: string; project_revision: number }>("/api/project/audio-settings", projectId, { patch })',
        'json<{ ok: boolean }>("/api/project/approve", post({ project_id: projectId, notes }))': 'projectPost<{ ok: boolean; project_revision: number }>("/api/project/approve", projectId, { notes })',
        'json<{ ok: boolean }>("/api/project/reject", post({ project_id: projectId, notes }))': 'projectPost<{ ok: boolean; project_revision: number }>("/api/project/reject", projectId, { notes })',
        'json<{ ok: boolean }>("/api/project/revise", post({ project_id: projectId, notes }))': 'projectPost<{ ok: boolean; project_revision: number }>("/api/project/revise", projectId, { notes })',
        'json<{ ok: boolean; path?: string; error?: string }>("/api/project/segments", post({ project_id: projectId, segments }))': 'projectPost<{ ok: boolean; path?: string; error?: string; project_revision: number }>("/api/project/segments", projectId, { segments })',
        'json<{ ok: boolean; path?: string; error?: string }>("/api/project/segment-timing", post({ project_id: projectId, segment_id: segmentId, ...timing }))': 'projectPost<{ ok: boolean; path?: string; error?: string; project_revision: number }>("/api/project/segment-timing", projectId, { segment_id: segmentId, ...timing })',
        'json<{ ok: boolean; storyboard?: StoryboardState; render_changed?: boolean; approval_invalidated?: boolean; error?: string }>("/api/project/storyboard", post({ project_id: projectId, state }))': 'projectPost<{ ok: boolean; storyboard?: StoryboardState; render_changed?: boolean; approval_invalidated?: boolean; error?: string; project_revision: number }>("/api/project/storyboard", projectId, { state })',
    }
    for old, new in replacements.items():
        text = replace_once(text, old, new, old[:50])
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_database()
    patch_ui()
    patch_api()
