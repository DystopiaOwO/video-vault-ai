import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "select_pr_tests",
    Path(__file__).parents[1] / "scripts" / "select_pr_tests.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
select_changed_files = _MODULE.select_changed_files


def test_frontend_change_does_not_trigger_python_fallback():
    selection = select_changed_files(["web/src/main.tsx"])
    assert selection.fallback_non_media is False
    assert selection.targeted_tests == []
    assert selection.media_smoke is False


def test_database_change_selects_database_project_tests():
    selection = select_changed_files(["src/video_vault/database.py"])
    assert "tests/test_database.py" in selection.targeted_tests
    assert "tests/test_project.py" in selection.targeted_tests
    assert selection.fallback_non_media is False


def test_perception_change_selects_perception_tests():
    selection = select_changed_files(["src/video_vault/perception_runs.py"])
    assert "tests/test_perception_runs.py" in selection.targeted_tests
    assert "tests/test_media_ownership.py" in selection.targeted_tests


def test_renderer_change_enables_micro_media_smoke():
    selection = select_changed_files(["src/video_vault/segment_renderer.py"])
    assert selection.media_smoke is True
    assert "tests/test_segment_renderer.py" in selection.targeted_tests
    assert selection.fallback_non_media is False


@pytest.mark.parametrize(
    "path",
    [
        "src/video_vault/analyzer/multi_frame.py",
        "src/video_vault/analyzer/vision_pipeline.py",
        "src/video_vault/project_perception.py",
        "src/video_vault/visual_compositor.py",
        "src/video_vault/ui.py",
        "tests/test_media_smoke.py",
    ],
)
def test_phase2_media_paths_enable_media_smoke(path):
    selection = select_changed_files([path])
    assert selection.media_smoke is True


def test_unrelated_documentation_does_not_enable_media_smoke():
    selection = select_changed_files(["docs/phase-2-notes.md"])
    assert selection.media_smoke is False


def test_unknown_source_change_uses_non_media_fallback():
    selection = select_changed_files(["src/video_vault/unmapped_feature.py"])
    assert selection.fallback_non_media is True
    assert selection.media_smoke is False


def test_ci_workflow_change_uses_safe_non_media_fallback():
    selection = select_changed_files([".github/workflows/ci.yml"])
    assert selection.fallback_non_media is True
    assert any("fallback" in reason for reason in selection.reasons)


def test_conftest_change_uses_safe_non_media_fallback():
    selection = select_changed_files(["tests/conftest.py"])
    assert selection.fallback_non_media is True
    assert selection.targeted_tests == []


def test_media_smoke_test_change_enables_media_smoke():
    selection = select_changed_files(["tests/test_media_smoke.py"])
    assert selection.media_smoke is True
    assert selection.targeted_tests == []


def _collect(path: str, marker_expression: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", path, "-m", marker_expression],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_pure_audio_manifest_test_is_collected_without_media_marker():
    output = _collect(
        "tests/test_audio_mixing.py::test_selected_bgm_id_and_bgm_only_without_track_block_manifest",
        "not media_e2e",
    )
    assert "test_selected_bgm_id_and_bgm_only_without_track_block_manifest" in output


def test_pure_color_candidate_test_is_collected_without_media_marker():
    output = _collect(
        "tests/test_color_consistency.py::test_frame_in_disabled_project_segment_is_not_a_reference_candidate",
        "not media_e2e",
    )
    assert "test_frame_in_disabled_project_segment_is_not_a_reference_candidate" in output


def test_real_ffmpeg_e2e_test_keeps_media_marker():
    output = _collect(
        "tests/test_color_consistency.py::test_real_ffmpeg_color_preview_writes_before_after",
        "media_e2e",
    )
    assert "test_real_ffmpeg_color_preview_writes_before_after" in output
