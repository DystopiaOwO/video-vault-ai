"""Shared Render Pipeline v2 data contracts.

This module intentionally contains no rendering logic.  Render stages should
exchange these stable, serializable records instead of defining local copies
of the same concepts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


class _ValueEnum(str, Enum):
    """String-valued enum that serializes naturally in JSON."""

    def __str__(self) -> str:
        return self.value


class RenderKind(_ValueEnum):
    ROUGH_PREVIEW = "rough_preview"
    ACCURATE_PREVIEW = "accurate_preview"
    FINAL = "final"


class RenderJobStatus(_ValueEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_QC = "failed_qc"
    CANCELLED = "cancelled"


class RenderStage(_ValueEnum):
    COMPILE_MANIFEST = "compile_manifest"
    PREFLIGHT = "preflight"
    PROBE_SOURCES = "probe_sources"
    RENDER_SEGMENTS = "render_segments"
    ASSEMBLE_TIMELINE = "assemble_timeline"
    MIX_AUDIO = "mix_audio"
    RENDER_OVERLAYS = "render_overlays"
    ENCODE_OUTPUT = "encode_output"
    QUALITY_CHECK = "quality_check"


@dataclass(frozen=True)
class BgmSettings:
    """Background music selection and licensing metadata for a manifest."""

    enabled: bool = False
    source_file: str | None = None
    track_id: str | None = None
    volume_db: float = -24.0
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    loop: bool = True
    attribution: str = ""
    license_name: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class ColorSettings:
    """Color decision captured at review time."""

    mode: str = "none"
    lut_path: str | None = None
    decision: str = ""
    reference_clip_id: str | None = None
    brightness: float = 0.0
    saturation: float = 1.0
    gamma: float = 1.0


@dataclass(frozen=True)
class RenderProfile:
    """Resolved output characteristics; profile lookup belongs elsewhere."""

    name: str = "preview_1080p30"
    width: int = 1920
    height: int = 1080
    fps_num: int = 30
    fps_den: int = 1
    pixel_format: str = "yuv420p"
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    video_encoder: str = "h264_nvenc"
    fallback_video_encoder: str = "libx264"


@dataclass(frozen=True)
class RenderSettings:
    """All render-affecting settings that must be bound to approval."""

    kind: RenderKind = RenderKind.ROUGH_PREVIEW
    profile: str = "preview_1080p30"
    encoder: str = "h264_nvenc"
    transition: str = "cut"
    overlay_enabled: bool = False
    audio_role: str = "keep_original"
    audio_crossfade_ms: int = 80
    bgm: BgmSettings = field(default_factory=BgmSettings)
    color: ColorSettings = field(default_factory=ColorSettings)


@dataclass(frozen=True)
class MediaProbeResult:
    """Normalized ffprobe result for one source file."""

    source_file: str
    duration_ms: int
    width: int
    height: int
    rotation: int = 0
    fps_num: int = 0
    fps_den: int = 1
    is_vfr: bool = False
    time_base: str = ""
    pixel_format: str = ""
    color_metadata: dict[str, Any] = field(default_factory=dict)
    has_audio: bool = False
    audio_track_count: int = 0
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None


@dataclass(frozen=True)
class RenderSegment:
    """One reviewed source range in timeline order."""

    segment_id: str
    source_file: str
    source_in_ms: int
    source_out_ms: int
    manual_order: int = 0
    include: bool = True
    speed: float = 1.0
    audio_role: str = "keep_original"
    scene_role: str = ""
    title: str = ""
    timeline_start_ms: int = 0
    timeline_duration_ms: int = 0
    source_duration_ms: int | None = None
    overlay: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderManifest:
    """Immutable-at-render-time description of one output."""

    schema_version: str = "1.0"
    manifest_hash: str = ""
    plan_id: str = ""
    project_id: str = ""
    render_kind: RenderKind = RenderKind.ROUGH_PREVIEW
    profile: str = "preview_1080p30"
    settings: RenderSettings = field(default_factory=RenderSettings)
    segments: list[RenderSegment] = field(default_factory=list)
    timeline_duration_ms: int = 0
    bgm: BgmSettings = field(default_factory=BgmSettings)
    color: ColorSettings = field(default_factory=ColorSettings)
    overlays: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""


@dataclass
class RenderJob:
    """Persistent state for one asynchronous render job."""

    job_id: str
    project_id: str
    kind: RenderKind
    status: RenderJobStatus = RenderJobStatus.QUEUED
    stage: RenderStage = RenderStage.COMPILE_MANIFEST
    percent: float = 0.0
    current_segment: str | None = None
    total_segments: int = 0
    pid: int | None = None
    encoder: str = ""
    output: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class QcReport:
    """Post-render quality-check result."""

    passed: bool
    status: str = "passed"
    output: str | None = None
    duration_ms: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ManifestCompiler(Protocol):
    def compile(self, project_id: str, settings: RenderSettings) -> RenderManifest:
        """Compile reviewed project inputs into an immutable manifest."""


class RenderExecutor(Protocol):
    def render(self, manifest: RenderManifest, job: RenderJob) -> QcReport:
        """Render one manifest and return its post-render QC result."""


def to_dict(value: Any) -> Any:
    """Convert contract values to JSON-compatible builtins."""

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_dict(item) for item in value]
    return value


__all__ = [
    "BgmSettings",
    "ColorSettings",
    "ManifestCompiler",
    "MediaProbeResult",
    "QcReport",
    "RenderExecutor",
    "RenderJob",
    "RenderJobStatus",
    "RenderKind",
    "RenderManifest",
    "RenderProfile",
    "RenderSegment",
    "RenderSettings",
    "RenderStage",
    "to_dict",
]
