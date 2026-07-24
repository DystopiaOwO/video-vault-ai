from __future__ import annotations

from pathlib import Path

import pytest


# Keep the PR contract in one place so a mixed test module can contain both
# pure command/state checks and real media tests without marking the whole file
# as media_e2e.
PR_CORE_TESTS = frozenset(
    {
        "test_audio_mixing.py",
        "test_ci_selector.py",
        "test_config.py",
        "test_database.py",
        "test_doctor.py",
        "test_encoding.py",
        "test_ffmpeg_process_runner.py",
        "test_final_qc.py",
        "test_media_ownership.py",
        "test_media_probe.py",
        "test_perception_run_concurrency.py",
        "test_perception_run_early_failures.py",
        "test_perception_runs.py",
        "test_project.py",
        "test_render_contracts.py",
        "test_render_job_api.py",
        "test_render_job_e2e.py",
        "test_render_job_manager.py",
        "test_render_job_store.py",
        "test_render_manifest.py",
        "test_render_profiles.py",
        "test_segment_cache.py",
        "test_stable_identities.py",
        "test_ui_static.py",
        "test_ui_upload_parser.py",
        "test_user_summary_clear_fallback.py",
        "test_user_summary_provenance.py",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        filename = Path(str(item.fspath)).name
        if filename not in PR_CORE_TESTS:
            continue
        if item.get_closest_marker("media_e2e") or item.get_closest_marker("media_smoke"):
            continue
        item.add_marker(pytest.mark.pr_core)
