"""Manifest-driven Render Pipeline v2 engine.

The engine owns stage orchestration and temporary output handling.  Segment
rendering is injected so Agent B's renderer can plug in without introducing a
second manifest or cache contract.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .media_probe import probe_media
from .process_manager import FFmpegProcessManager
from .render_jobs import RenderJobStore
from .render_qc import quality_check, write_qc_report
from .render_types import QcReport, RenderJob, RenderJobStatus, RenderManifest, RenderStage
from .timeline_assembler import assembly_items, concat_file_lines, validate_timeline


class RenderCancelled(RuntimeError):
    """Raised when the current render job is cancelled."""


SegmentRenderer = Callable[[RenderManifest, Any, Path], Path]
AudioMixer = Callable[[RenderManifest, Path, Path], Path]


class RenderEngine:
    def __init__(self, cfg: Mapping[str, Any], job_store: RenderJobStore,
                 process_manager: FFmpegProcessManager | None = None,
                 segment_renderer: SegmentRenderer | None = None,
                 audio_mixer: AudioMixer | None = None):
        self.cfg = dict(cfg)
        self.job_store = job_store
        self.process_manager = process_manager or FFmpegProcessManager()
        self.segment_renderer = segment_renderer
        self.audio_mixer = audio_mixer

    def render(self, manifest: RenderManifest, job: RenderJob | str) -> QcReport:
        current = self.job_store.get_job(job) if isinstance(job, str) else job
        if current is None:
            raise ValueError("render job was not found")
        job_id = current.job_id
        if current.status is RenderJobStatus.CANCELLED:
            raise RenderCancelled()
        root = Path(self.cfg.get("render_root") or Path(self.cfg.get("library_root", ".")) / "render")
        cache_root, output_root, qc_root = root / "cache" / "segments", root / "outputs", root / "qc" / job_id
        cache_root.mkdir(parents=True, exist_ok=True); output_root.mkdir(parents=True, exist_ok=True)
        self._update(job_id, status=RenderJobStatus.RUNNING, stage=RenderStage.COMPILE_MANIFEST, percent=0.0,
                     total_segments=len(manifest.segments), encoder=manifest.settings.encoder)
        try:
            self._check_cancel(job_id)
            self._stage(job_id, RenderStage.PREFLIGHT, 5.0)
            errors = validate_timeline(manifest)
            if errors: raise ValueError("; ".join(errors))
            if not manifest.segments: raise ValueError("manifest contains no renderable segments")
            self._stage(job_id, RenderStage.PROBE_SOURCES, 10.0)
            for item in assembly_items(manifest):
                self._check_cancel(job_id)
                probe_media(item.source_file, self.cfg)
            self._stage(job_id, RenderStage.RENDER_SEGMENTS, 15.0)
            rendered: list[Path] = []
            for index, item in enumerate(assembly_items(manifest), 1):
                self._check_cancel(job_id)
                self._update(job_id, current_segment=item.segment_id,
                             percent=15.0 + 45.0 * index / len(manifest.segments))
                cache_path = cache_root / f"{item.segment_id}.mp4"
                if not cache_path.exists():
                    if self.segment_renderer is None:
                        raise RuntimeError("no segment renderer configured")
                    produced = Path(self.segment_renderer(manifest, item, cache_path))
                    if produced != cache_path:
                        os.replace(produced, cache_path)
                rendered.append(cache_path)
            self._check_cancel(job_id)
            self._stage(job_id, RenderStage.ASSEMBLE_TIMELINE, 65.0)
            partial = output_root / f"{manifest.plan_id or manifest.project_id}_{manifest.manifest_hash}.mp4.partial"
            self._concat(job_id, manifest, rendered, partial)
            self._stage(job_id, RenderStage.MIX_AUDIO, 78.0)
            self._check_cancel(job_id)
            mixed = self._mix_audio(job_id, manifest, partial)
            self._stage(job_id, RenderStage.RENDER_OVERLAYS, 82.0)
            self._stage(job_id, RenderStage.ENCODE_OUTPUT, 90.0)
            self._check_cancel(job_id)
            self._stage(job_id, RenderStage.QUALITY_CHECK, 95.0)
            profile = None
            try:
                from .render_profiles import get_render_profile
                profile = get_render_profile(manifest.profile)
            except ValueError:
                pass
            report = quality_check(mixed, self.cfg, expected_duration_ms=manifest.timeline_duration_ms, profile=profile)
            write_qc_report(report, qc_root)
            if not report.passed:
                self._update(job_id, status=RenderJobStatus.FAILED_QC, percent=100.0, error="; ".join(report.errors), output=str(partial), finished_at=_now())
                return report
            final = output_root / partial.name.removesuffix(".partial")
            if mixed != partial:
                os.replace(mixed, partial)
            os.replace(partial, final)
            report = QcReport(True, status="passed", output=str(final), duration_ms=report.duration_ms,
                              metrics=report.metrics, warnings=report.warnings, errors=report.errors)
            write_qc_report(report, qc_root)
            self._update(job_id, status=RenderJobStatus.COMPLETED, percent=100.0, output=str(final), finished_at=_now(), current_segment=None)
            return report
        except RenderCancelled:
            self._update(job_id, status=RenderJobStatus.CANCELLED, error="render cancelled", finished_at=_now())
            raise
        except Exception as exc:
            self._update(job_id, status=RenderJobStatus.FAILED, error=str(exc), finished_at=_now())
            raise

    def _concat(self, job_id: str, manifest: RenderManifest, rendered: list[Path], partial: Path) -> None:
        fd, list_name = tempfile.mkstemp(prefix="render_concat_", suffix=".txt")
        os.close(fd)
        list_file = Path(list_name)
        try:
            list_file.write_text(concat_file_lines(manifest, [str(path) for path in rendered]), encoding="utf-8")
            ffmpeg = str(self.cfg.get("ffmpeg_path", "ffmpeg"))
            from .render_profiles import get_render_profile
            profile = get_render_profile(manifest.profile)
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(list_file), "-c:v", manifest.settings.encoder or profile.video_encoder,
                       "-r", f"{profile.fps_num}/{profile.fps_den}", "-pix_fmt", profile.pixel_format,
                       "-c:a", "aac", "-ar", str(profile.audio_sample_rate), "-ac", str(profile.audio_channels),
                       "-movflags", "+faststart", str(partial)]
            code = self.process_manager.run(job_id, command)
            if code != 0: raise RuntimeError(f"ffmpeg assembly failed with exit code {code}")
        finally:
            list_file.unlink(missing_ok=True)

    def _mix_audio(self, job_id: str, manifest: RenderManifest, assembled: Path) -> Path:
        """Run the injected audio mixer, keeping its result temporary."""
        if self.audio_mixer is None:
            return assembled
        mixed = assembled.with_name(assembled.name.removesuffix(".partial") + ".mixed.mp4.partial")
        produced = Path(self.audio_mixer(manifest, assembled, mixed))
        if produced != mixed:
            os.replace(produced, mixed)
        self._check_cancel(job_id)
        return mixed

    def _stage(self, job_id: str, stage: RenderStage, percent: float) -> None:
        self._check_cancel(job_id); self._update(job_id, stage=stage, percent=percent)

    def _check_cancel(self, job_id: str) -> None:
        job = self.job_store.get_job(job_id)
        if job and job.status is RenderJobStatus.CANCELLED: raise RenderCancelled()

    def _update(self, job_id: str, **changes: Any) -> None:
        self.job_store.update_job(job_id, **changes)


def render_manifest(engine: RenderEngine, manifest: RenderManifest, job: RenderJob | str) -> QcReport:
    return engine.render(manifest, job)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = ["RenderCancelled", "RenderEngine", "render_manifest"]
