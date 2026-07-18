from __future__ import annotations

import json
from pathlib import Path

from video_vault.render_types import (
    BgmSettings,
    ColorSettings,
    RenderKind,
    RenderManifest,
    RenderSegment,
    RenderSettings,
    RenderStage,
    to_dict,
)


ROOT = Path(__file__).resolve().parents[1]


def test_render_contracts_import_and_serialize() -> None:
    manifest = RenderManifest(
        project_id="project_1",
        plan_id="plan_v001",
        render_kind=RenderKind.FINAL,
        settings=RenderSettings(
            kind=RenderKind.FINAL,
            bgm=BgmSettings(enabled=True, track_id="bgm-1"),
            color=ColorSettings(mode="dji_lut"),
        ),
        segments=[
            RenderSegment(
                segment_id="seg_001",
                source_file="D:/VideoLibrary/source.mp4",
                source_in_ms=1200,
                source_out_ms=3370,
            )
        ],
    )

    data = to_dict(manifest)

    assert data["render_kind"] == "final"
    assert data["settings"]["kind"] == "final"
    assert data["segments"][0]["source_in_ms"] == 1200
    assert data["bgm"]["enabled"] is False
    assert RenderStage.QUALITY_CHECK.value == "quality_check"


def test_render_manifest_schema_is_loadable_and_declares_contract() -> None:
    schema_path = ROOT / "schemas" / "render_manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert "render_kind" in schema["required"]
    assert schema["$defs"]["renderKind"]["enum"] == [
        "rough_preview",
        "accurate_preview",
        "final",
    ]
    assert schema["$defs"]["renderSegment"]["properties"]["source_in_ms"]["type"] == "integer"
