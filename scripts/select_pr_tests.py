"""Select the PR test layers from the changed file list."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
import subprocess
from typing import Iterable


CORE_EXPRESSION = "pr_core and not media_smoke and not media_e2e"
NON_MEDIA_EXPRESSION = "not media_smoke and not media_e2e"

CORE_TEST_BY_AREA = {
    "perception": [
        "tests/test_perception_runs.py",
        "tests/test_perception_run_concurrency.py",
        "tests/test_perception_run_early_failures.py",
        "tests/test_media_ownership.py",
    ],
    "database": [
        "tests/test_database.py",
        "tests/test_project.py",
        "tests/test_media_ownership.py",
        "tests/test_user_summary_provenance.py",
    ],
    "render": [
        "tests/test_render_contracts.py",
        "tests/test_render_manifest.py",
        "tests/test_render_profiles.py",
        "tests/test_segment_cache.py",
        "tests/test_segment_renderer.py",
        "tests/test_project_renderer.py",
        "tests/test_timeline_assembler.py",
        "tests/test_final_qc.py",
    ],
    "audio": [
        "tests/test_audio_pipeline.py",
        "tests/test_audio_state.py",
        "tests/test_audio_mixing.py",
        "tests/test_bgm_pipeline.py",
        "tests/test_bgm.py",
    ],
    "color": [
        "tests/test_color.py",
        "tests/test_color_pipeline.py",
        "tests/test_color_consistency.py",
    ],
    "ui": [
        "tests/test_ui_static.py",
        "tests/test_ui_upload_parser.py",
        "tests/test_render_job_api.py",
    ],
}

MEDIA_SMOKE_PATHS = (
    "src/video_vault/project_renderer.py",
    "src/video_vault/segment_renderer.py",
    "src/video_vault/timeline_assembler.py",
    "src/video_vault/ffmpeg_tools.py",
    "src/video_vault/bgm_pipeline.py",
    "src/video_vault/color_consistency.py",
    "src/video_vault/render_manifest.py",
)

FALLBACK_PATHS = (
    ".github/workflows/",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements",
    "package.json",
    "package-lock.json",
    "scripts/select_pr_tests.py",
    "tests/conftest.py",
)


@dataclass
class Selection:
    changed_files: list[str] = field(default_factory=list)
    targeted_tests: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    fallback_non_media: bool = False
    media_smoke: bool = False

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["core_expression"] = CORE_EXPRESSION
        result["non_media_expression"] = NON_MEDIA_EXPRESSION
        return result


def select_changed_files(paths: Iterable[str]) -> Selection:
    changed = sorted({path.replace("\\", "/") for path in paths if path})
    result = Selection(changed_files=changed)
    targeted: set[str] = set()

    if not changed:
        result.fallback_non_media = True
        result.reasons.append("changed file list is empty or unavailable")
        return result

    for path in changed:
        if path.startswith(FALLBACK_PATHS):
            result.fallback_non_media = True
            result.reasons.append(f"broad fallback required by {path}")
        if path.startswith("web/"):
            result.reasons.append("frontend change is covered by the Frontend job")
        if path.startswith("tests/") and path.endswith(".py"):
            if path == "tests/test_media_smoke.py":
                result.media_smoke = True
                result.reasons.append("changed media smoke test enables Media Smoke")
            elif "e2e" not in Path(path).stem and path != "tests/conftest.py":
                targeted.add(path)
                result.reasons.append(f"changed test selected: {path}")
            else:
                result.reasons.append(f"media test remains in the full/media suite: {path}")
        if path.startswith("src/video_vault/"):
            lowered = path.lower()
            matched_area = False
            if any(token in lowered for token in ("perception", "analysis", "frame", "segment", "migration")):
                targeted.update(CORE_TEST_BY_AREA["perception"])
                result.reasons.append(f"perception/segment mapping selected for {path}")
                matched_area = True
            if "database.py" in lowered or path.endswith("project.py") or "migration" in lowered:
                targeted.update(CORE_TEST_BY_AREA["database"])
                result.reasons.append(f"database/project mapping selected for {path}")
                matched_area = True
            if any(token in lowered for token in ("project_renderer", "segment_renderer", "timeline_assembler", "ffmpeg_tools", "render_manifest")):
                targeted.update(CORE_TEST_BY_AREA["render"])
                result.reasons.append(f"render mapping selected for {path}")
                matched_area = True
                if path in MEDIA_SMOKE_PATHS:
                    result.media_smoke = True
            if "audio" in lowered or "bgm" in lowered:
                targeted.update(CORE_TEST_BY_AREA["audio"])
                result.reasons.append(f"audio mapping selected for {path}")
                matched_area = True
            if "color" in lowered:
                targeted.update(CORE_TEST_BY_AREA["color"])
                result.reasons.append(f"color mapping selected for {path}")
                matched_area = True
            if path.endswith("ui.py"):
                targeted.update(CORE_TEST_BY_AREA["ui"])
                result.reasons.append(f"UI/API mapping selected for {path}")
                matched_area = True
            if not matched_area:
                result.fallback_non_media = True
                result.reasons.append(f"unmapped source module requires fallback: {path}")

    result.targeted_tests = sorted(targeted)
    if result.fallback_non_media:
        result.reasons.append("Ubuntu will run all non-media tests as the safe fallback")
    elif result.targeted_tests:
        result.reasons.append("Ubuntu will run PR Core plus the selected targeted tests")
    else:
        result.reasons.append("no Python targeted tests required")
    if result.media_smoke:
        result.reasons.append("Ubuntu Media Smoke is enabled for the render path")
    return result


def changed_files(base: str, head: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return completed.stdout.splitlines()


def write_github_output(path: str, selection: Selection) -> None:
    values = {
        "fallback_non_media": str(selection.fallback_non_media).lower(),
        "media_smoke": str(selection.media_smoke).lower(),
        "targeted_tests": " ".join(selection.targeted_tests),
        "changed_count": str(len(selection.changed_files)),
    }
    with open(path, "a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args()
    try:
        selection = select_changed_files(changed_files(args.base, args.head))
    except (OSError, RuntimeError) as exc:
        selection = select_changed_files([])
        selection.reasons.append(f"selector error: {exc}")
    print(json.dumps(selection.to_dict(), ensure_ascii=False, indent=2))
    if args.github_output or os.environ.get("GITHUB_OUTPUT"):
        write_github_output(args.github_output or os.environ["GITHUB_OUTPUT"], selection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
