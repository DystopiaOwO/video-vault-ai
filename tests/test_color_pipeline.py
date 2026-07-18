from pathlib import Path

import pytest

from video_vault.color_pipeline import ColorPipelineError, build_color_filter, escape_filter_path


def test_none_color_does_not_add_filter():
    assert build_color_filter({"mode": "none"}) == ""


def test_dji_lut_requires_existing_file_and_escapes_path(tmp_path: Path):
    lut = tmp_path / "DJI LUT.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    result = build_color_filter({"mode": "dji_lut", "lut_path": str(lut)})
    assert result.startswith("lut3d=file='")
    assert "DJI LUT.cube" in result
    with pytest.raises(ColorPipelineError, match="does not exist"):
        build_color_filter({"mode": "dji_lut", "lut_path": str(tmp_path / "missing.cube")})


def test_windows_filter_path_escapes_drive_colon_backslash_and_quote():
    escaped = escape_filter_path(r"C:\work dir\it's.cube")
    assert escaped == r"C\:/work dir/it\'s.cube"


def test_lut_is_not_applied_twice(tmp_path: Path):
    lut = tmp_path / "identity.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    with pytest.raises(ColorPipelineError, match="more than once"):
        build_color_filter({"mode": "dji_lut", "lut_path": str(lut)}, lut_already_applied=True)
