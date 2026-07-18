"""Post-render media quality checks for Render Pipeline v2."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .media_probe import MediaProbeError, probe_media
from .render_types import QcReport, RenderProfile


def quality_check(
    output: str | Path,
    cfg: Mapping[str, Any],
    *,
    expected_duration_ms: int | None = None,
    profile: RenderProfile | None = None,
) -> QcReport:
    """Check that an output is decodable and matches the requested contract."""

    path = Path(output)
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {"file_size": path.stat().st_size if path.exists() else 0}
    if not path.exists() or path.stat().st_size == 0:
        return QcReport(False, status="failed_qc", output=str(path), errors=["output is missing or empty"], metrics=metrics)
    try:
        result = probe_media(path, cfg, force=True)
    except (MediaProbeError, OSError) as exc:
        return QcReport(False, status="failed_qc", output=str(path), errors=[str(exc)], metrics=metrics)

    metrics.update({
        "duration_ms": result.duration_ms,
        "width": result.width,
        "height": result.height,
        "fps_num": result.fps_num,
        "fps_den": result.fps_den,
        "pixel_format": result.pixel_format,
        "has_audio": result.has_audio,
        "audio_sample_rate": result.audio_sample_rate,
        "audio_channels": result.audio_channels,
    })
    if result.width <= 0 or result.height <= 0:
        errors.append("output has no video track")
    if profile is not None:
        if (result.width, result.height) != (profile.width, profile.height):
            errors.append(f"resolution mismatch: got {result.width}x{result.height}, expected {profile.width}x{profile.height}")
        if result.pixel_format and result.pixel_format != profile.pixel_format:
            warnings.append(f"pixel format differs: got {result.pixel_format}, expected {profile.pixel_format}")
        if result.has_audio and result.audio_sample_rate != profile.audio_sample_rate:
            errors.append(f"audio sample rate mismatch: got {result.audio_sample_rate}, expected {profile.audio_sample_rate}")
        if result.has_audio and result.audio_channels != profile.audio_channels:
            errors.append(f"audio channel mismatch: got {result.audio_channels}, expected {profile.audio_channels}")
    if expected_duration_ms is not None:
        delta = abs(result.duration_ms - int(expected_duration_ms))
        metrics["duration_delta_ms"] = delta
        tolerance = max(50, round(1000 / max(1, (profile.fps_num / profile.fps_den if profile else 30))))
        if delta > tolerance:
            errors.append(f"duration mismatch: got {result.duration_ms}ms, expected {expected_duration_ms}ms")
    return QcReport(not errors, status="passed" if not errors else "failed_qc", output=str(path),
                    duration_ms=result.duration_ms, metrics=metrics, warnings=warnings, errors=errors)


def write_qc_report(report: QcReport, directory: str | Path) -> tuple[Path, Path]:
    """Write the machine-readable and human-readable QC reports."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path, md_path = root / "qc_report.json", root / "qc_report.md"
    data = {
        "passed": report.passed, "status": report.status, "output": report.output,
        "duration_ms": report.duration_ms, "metrics": report.metrics,
        "warnings": report.warnings, "errors": report.errors,
    }
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Render QC", "", f"- Status: `{report.status}`", f"- Passed: `{report.passed}`"]
    if report.output:
        lines.append(f"- Output: `{report.output}`")
    if report.warnings:
        lines += ["", "## Warnings", *[f"- {item}" for item in report.warnings]]
    if report.errors:
        lines += ["", "## Errors", *[f"- {item}" for item in report.errors]]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


qc_output = quality_check

__all__ = ["quality_check", "qc_output", "write_qc_report"]
