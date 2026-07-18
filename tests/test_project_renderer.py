from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.project_renderer import ProjectRenderError, render_project


def _manifest(tmp_path: Path, *, transition="cut", overlay=False, bgm=None):
    return {
        "manifest_hash": "a" * 64,
        "profile": {"profile_id": "final_1080p"},
        "settings": {"transition": {"type": transition, "duration_seconds": 0 if transition == "cut" else 1}, "overlay": {"enabled": overlay}},
        "segments": [{"segment_id": "b", "order": 2, "timeline_duration_seconds": 1}, {"segment_id": "a", "order": 1, "timeline_duration_seconds": 1}],
        "bgm": bgm or [],
    }


def test_project_renderer_stops_at_approval_gate(monkeypatch, tmp_path: Path):
    import video_vault.project_renderer as renderer

    monkeypatch.setattr(renderer, "can_project_render", lambda *args: (False, "尚未核准"))
    with pytest.raises(PermissionError, match="尚未核准"):
        render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1)


@pytest.mark.parametrize(
    ("transition", "overlay", "message"),
    [("dissolve", False, "unsupported transition"), ("cut", True, "overlay is not supported")],
)
def test_phase4a_rejects_transition_and_overlay(monkeypatch, tmp_path: Path, transition, overlay, message):
    import video_vault.project_renderer as renderer

    folder = tmp_path / "08_projects" / "project_1"
    folder.mkdir(parents=True)
    manifest = _manifest(tmp_path, transition=transition, overlay=overlay)
    (folder / "render_manifest.json").write_text(__import__("json").dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(__import__("json").dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(renderer, "can_project_render", lambda *args: (True, "approved"))
    monkeypatch.setattr(renderer, "manifest_hash", lambda value: "a" * 64)
    monkeypatch.setattr(renderer, "validate_render_manifest", lambda value: {"errors": []})
    with pytest.raises(ProjectRenderError, match=message):
        render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1)


def test_multiple_bgm_is_rejected(monkeypatch, tmp_path: Path):
    import video_vault.project_renderer as renderer
    import json

    folder = tmp_path / "08_projects" / "project_1"
    folder.mkdir(parents=True)
    manifest = _manifest(tmp_path, bgm=[{"track_id": 1}, {"track_id": 2}])
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(renderer, "can_project_render", lambda *args: (True, "approved"))
    monkeypatch.setattr(renderer, "manifest_hash", lambda value: "a" * 64)
    monkeypatch.setattr(renderer, "validate_render_manifest", lambda value: {"errors": []})
    with pytest.raises(ProjectRenderError, match="multiple BGM scheduling"):
        render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1)


def test_report_publish_failure_rolls_back_final_files(monkeypatch, tmp_path: Path):
    import json
    import video_vault.project_renderer as renderer
    from video_vault.final_qc import FinalQCResult
    from video_vault.segment_renderer import SegmentRenderResult

    folder = tmp_path / "08_projects" / "project_1"
    folder.mkdir(parents=True)
    manifest = _manifest(tmp_path)
    manifest.update({"project_id": 1, "schema_version": "2.0", "plan_id": "p1", "project_name": "test", "expected_duration_seconds": 2})
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(renderer, "can_project_render", lambda *args: (True, "approved"))
    monkeypatch.setattr(renderer, "manifest_hash", lambda value: "a" * 64)
    monkeypatch.setattr(renderer, "validate_render_manifest", lambda value: {"errors": [], "warnings": []})
    cache_file = tmp_path / "segment.mp4"
    cache_file.write_bytes(b"segment")
    fake_segment = SegmentRenderResult("a", cache_file, "cache-a", True, "cpu", "libx264", 1)
    monkeypatch.setattr(renderer, "render_segment", lambda *args, **kwargs: fake_segment)
    monkeypatch.setattr(renderer, "build_concat_file", lambda paths, out: out)
    monkeypatch.setattr(renderer, "build_timeline_command", lambda *args, **kwargs: ["ffmpeg", str(args[2])])

    def fake_run(command, runner=None):
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[-1]).write_bytes(b"final")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(renderer, "run_command", fake_run)
    monkeypatch.setattr(renderer, "validate_final_output", lambda *args, **kwargs: FinalQCResult(True, 2, "sha"))
    original_replace = Path.replace

    def fail_report_replace(self, target):
        if self.name.endswith(".render.json.tmp"):
            raise OSError("report publish failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_report_replace)
    with pytest.raises(OSError, match="report publish failed"):
        render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1)
    renders = folder / "renders"
    assert not list(renders.glob("*.mp4"))
    assert not list(renders.glob("*.render.json"))
    assert not list(renders.glob("*.partial.mp4"))
    assert list((renders / "logs").glob("*.log"))
