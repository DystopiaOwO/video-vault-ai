"""Phase 2 render data contracts.

These dataclasses describe the manifest boundary only. They do not execute
FFmpeg, manage jobs, or perform rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RenderProfile:
    profile_id: str
    width: int
    height: int
    fps: int
    video_codec: str
    pixel_format: str
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
        }


@dataclass(frozen=True)
class RenderSegment:
    segment_id: str
    order: int
    clip_id: str
    video_id: int
    source_file: str
    source_in_seconds: float
    source_out_seconds: float
    source_duration_seconds: float
    speed: float
    timeline_duration_seconds: float
    audio_role: str
    scene_role: str = ""
    story_position: str = ""
    user_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "order": self.order,
            "clip_id": self.clip_id,
            "video_id": self.video_id,
            "source_file": self.source_file,
            "source_in_seconds": self.source_in_seconds,
            "source_out_seconds": self.source_out_seconds,
            "source_duration_seconds": self.source_duration_seconds,
            "speed": self.speed,
            "timeline_duration_seconds": self.timeline_duration_seconds,
            "audio_role": self.audio_role,
            "scene_role": self.scene_role,
            "story_position": self.story_position,
            "user_notes": self.user_notes,
        }


@dataclass(frozen=True)
class RenderManifest:
    schema_version: str
    project_id: int
    project_name: str
    plan_id: str
    profile: dict[str, Any]
    settings: dict[str, Any]
    segments: list[dict[str, Any]] = field(default_factory=list)
    bgm: list[dict[str, Any]] = field(default_factory=list)
    expected_duration_seconds: float = 0.0
    manifest_hash: str = ""
    created_at: str = ""
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "plan_id": self.plan_id,
            "profile": self.profile,
            "settings": self.settings,
            "segments": self.segments,
            "bgm": self.bgm,
            "expected_duration_seconds": self.expected_duration_seconds,
            "manifest_hash": self.manifest_hash,
            "created_at": self.created_at,
            "validation": self.validation,
        }


__all__ = ["RenderManifest", "RenderProfile", "RenderSegment"]
