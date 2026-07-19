from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

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


def _write_approved_project(tmp_path: Path, manifest: dict) -> Path:
    folder = tmp_path / "08_projects" / "project_1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "render_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"approved_manifest_hash": "a" * 64}), encoding="utf-8")
    return folder


def _patch_approved(monkeypatch):
    import video_vault.project_renderer as renderer

    monkeypatch.setattr(renderer, "can_project_render", lambda *args: (True, "approved"))
    monkeypatch.setattr(renderer, "manifest_hash", lambda value: "a" * 64)
    monkeypatch.setattr(renderer, "validate_render_manifest", lambda value: {"errors": [], "warnings": []})
    return renderer


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
    with pytest.raises(ProjectRenderError, match="report publish failed"):
        render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1)
    renders = folder / "renders"
    assert not list(renders.glob("*.mp4"))
    assert not list(renders.glob("*.render.json"))
    assert not list(renders.glob("*.partial.mp4"))
    assert list((renders / "logs").glob("*.log"))


def test_output_equal_to_source_is_rejected_without_modifying_source(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    manifest = _manifest(tmp_path)
    manifest["segments"][0].update({"source_file": str(source)})
    _write_approved_project(tmp_path, manifest)
    renderer = _patch_approved(monkeypatch)
    with pytest.raises(ProjectRenderError, match="output path conflicts with source media"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=source)
    assert source.read_bytes() == b"original"


def test_partial_equal_to_another_source_is_rejected(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.partial.mp4"
    source.write_bytes(b"original")
    manifest = _manifest(tmp_path)
    manifest["segments"][0].update({"source_file": str(source)})
    _write_approved_project(tmp_path, manifest)
    renderer = _patch_approved(monkeypatch)
    with pytest.raises(ProjectRenderError, match="partial path conflicts with source media"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=tmp_path / "clip.mp4")
    assert source.read_bytes() == b"original"


def test_output_equal_to_bgm_is_rejected_without_modifying_bgm(monkeypatch, tmp_path: Path):
    bgm = tmp_path / "music.mp4"
    bgm.write_bytes(b"music")
    manifest = _manifest(tmp_path, bgm=[{"track_id": 1, "source_path": str(bgm)}])
    _write_approved_project(tmp_path, manifest)
    renderer = _patch_approved(monkeypatch)
    monkeypatch.setattr(renderer, "validate_bgm_track", lambda *args, **kwargs: None)
    monkeypatch.setattr(renderer, "bgm_fingerprint", lambda track: {"source_path": str(bgm)})
    with pytest.raises(ProjectRenderError, match="output path conflicts with BGM"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=bgm)
    assert bgm.read_bytes() == b"music"


def test_output_inside_segment_cache_is_rejected(monkeypatch, tmp_path: Path):
    manifest = _manifest(tmp_path)
    folder = _write_approved_project(tmp_path, manifest)
    renderer = _patch_approved(monkeypatch)
    unsafe = folder / "cache" / "unsafe.mp4"
    with pytest.raises(ProjectRenderError, match="protected segment cache"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=unsafe)


def test_existing_custom_output_is_never_overwritten_or_deleted(monkeypatch, tmp_path: Path):
    output = tmp_path / "family-video.mp4"
    output.write_bytes(b"family")
    _write_approved_project(tmp_path, _manifest(tmp_path))
    renderer = _patch_approved(monkeypatch)
    with pytest.raises(ProjectRenderError, match="custom output already exists"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=output)
    assert output.read_bytes() == b"family"
    assert not output.with_name("family-video.partial.mp4").exists()


@pytest.mark.parametrize("suffix", [".mov", ".mkv", ""])
def test_project_output_requires_mp4(monkeypatch, tmp_path: Path, suffix: str):
    _write_approved_project(tmp_path, _manifest(tmp_path))
    renderer = _patch_approved(monkeypatch)
    with pytest.raises(ProjectRenderError, match="must use the .mp4 extension"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=tmp_path / f"output{suffix}")


def test_custom_output_parent_is_created_after_validation(monkeypatch, tmp_path: Path):
    import video_vault.project_renderer as renderer
    from video_vault.final_qc import FinalQCResult
    from video_vault.segment_renderer import SegmentRenderResult

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manifest = _manifest(tmp_path)
    manifest["segments"][0].update({"source_file": str(source), "source_duration_seconds": 1, "source_in_seconds": 0, "source_out_seconds": 1, "timeline_duration_seconds": 1, "video_id": 1, "clip_id": "a"})
    _write_approved_project(tmp_path, manifest)
    _patch_approved(monkeypatch)
    fake_segment = SegmentRenderResult("a", source, "cache-a", False, "cpu", "libx264", 1)
    monkeypatch.setattr(renderer, "render_segment", lambda *args, **kwargs: fake_segment)
    monkeypatch.setattr(renderer, "build_concat_file", lambda paths, out: out)
    monkeypatch.setattr(renderer, "build_timeline_command", lambda *args, **kwargs: ["fake", str(args[2])])

    def fake_run(command, runner=None):
        Path(command[-1]).write_bytes(b"rendered")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(renderer, "run_command", fake_run)
    monkeypatch.setattr(renderer, "validate_final_output", lambda *args, **kwargs: FinalQCResult(True, 1, "sha"))
    output = tmp_path / "new" / "nested" / "project.mp4"
    result = renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=output)
    assert result.output_path == output.resolve()
    assert output.exists()
    assert output.with_name("project.mp4.render.json").exists()


def test_segment_failure_keeps_log_but_no_formal_output(monkeypatch, tmp_path: Path):
    manifest = _manifest(tmp_path)
    _write_approved_project(tmp_path, manifest)
    renderer = _patch_approved(monkeypatch)
    monkeypatch.setattr(renderer, "render_segment", lambda *args, **kwargs: (_ for _ in ()).throw(ProjectRenderError("segment failed")))
    output = tmp_path / "new-output.mp4"
    with pytest.raises(ProjectRenderError, match="segment failed"):
        renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=output)
    assert not output.exists()
    assert not output.with_name("new-output.mp4.render.json").exists()
    assert not output.with_name("new-output.partial.mp4").exists()
    assert list((tmp_path / "08_projects" / "project_1" / "renders" / "logs").glob("*.log"))


def test_cancelled_project_render_cleans_final_temps_and_keeps_segment_cache(monkeypatch, tmp_path: Path):
    import video_vault.project_renderer as renderer
    from video_vault.render_job_models import RenderCancelled
    from video_vault.segment_renderer import SegmentRenderResult

    source = tmp_path / "source.mp4"
    source.write_bytes(b"original source")
    manifest = _manifest(tmp_path)
    manifest["segments"][0].update({"source_file": str(source), "source_duration_seconds": 1, "source_in_seconds": 0, "source_out_seconds": 1, "video_id": 1, "clip_id": "a"})
    folder = _write_approved_project(tmp_path, manifest)
    _patch_approved(monkeypatch)
    cache_file = folder / "cache" / "segments" / "cache-a.mp4"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"completed segment cache")
    fake_segment = SegmentRenderResult("a", cache_file, "cache-a", True, "cpu", "libx264", 1)
    monkeypatch.setattr(renderer, "render_segment", lambda *args, **kwargs: fake_segment)

    def fake_concat(paths, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("ffconcat", encoding="utf-8")
        return output

    monkeypatch.setattr(renderer, "build_concat_file", fake_concat)
    monkeypatch.setattr(renderer, "build_timeline_command", lambda *args, **kwargs: ["fake-ffmpeg", str(args[2])])
    started = threading.Event()

    class SlowRunner:
        def __init__(self):
            self.cancel_event = threading.Event()

        def run(self, command, **kwargs):
            partial = Path(command[-1])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"final partial")
            started.set()
            while not self.cancel_event.is_set():
                time.sleep(0.01)
            raise RenderCancelled("cancelled by test")

    runner = SlowRunner()
    output = tmp_path / "cancelled.mp4"
    errors = []

    def render():
        try:
            renderer.render_project({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, output_path=output, runner=runner)
        except Exception as exc:  # noqa: BLE001 - cancellation is asserted below.
            errors.append(exc)

    thread = threading.Thread(target=render)
    thread.start()
    assert started.wait(timeout=5)
    runner.cancel_event.set()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert isinstance(errors[0], RenderCancelled)
    report = output.with_name(output.name + ".render.json")
    assert not output.exists()
    assert not report.exists()
    assert not output.with_name("cancelled.partial.mp4").exists()
    assert not list((folder / "work").rglob("*.tmp"))
    assert not list((folder / "work").rglob("*.ffconcat"))
    assert cache_file.read_bytes() == b"completed segment cache"
    assert source.read_bytes() == b"original source"
    assert list((folder / "renders" / "logs").glob("*.log"))


def test_mp4_publish_failure_does_not_delete_existing_target(monkeypatch, tmp_path: Path):
    import video_vault.project_renderer as renderer

    partial = tmp_path / "partial.mp4"
    report_temp = tmp_path / ".report.tmp"
    output = tmp_path / "existing.mp4"
    report = tmp_path / "existing.mp4.render.json"
    partial.write_bytes(b"partial")
    report_temp.write_bytes(b"report")
    output.write_bytes(b"old")
    original_replace = Path.replace

    def fail_partial_replace(self, target):
        if self == partial:
            raise OSError("mp4 publish failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_partial_replace)
    with pytest.raises(ProjectRenderError, match="mp4 publish failed"):
        renderer.publish_final_render_atomically(partial, report_temp, output, report)
    assert output.read_bytes() == b"old"
    assert not partial.exists()
    assert not report_temp.exists()
