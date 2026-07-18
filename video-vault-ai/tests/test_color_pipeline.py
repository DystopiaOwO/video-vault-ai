import pytest

from video_vault.color_pipeline import ColorPipelineError, build_color_filter
from video_vault.render_types import ColorSettings, RenderProfile


def test_none_color_has_no_colour_operation():
    assert build_color_filter(ColorSettings(mode="none"), RenderProfile()) == ""


def test_missing_lut_fails_before_render(tmp_path):
    with pytest.raises(ColorPipelineError, match="LUT file does not exist"):
        build_color_filter(ColorSettings(mode="dji_lut", lut_path=str(tmp_path / "missing.cube")), RenderProfile())


def test_lut_is_only_one_filter(tmp_path):
    lut = tmp_path / "camera.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="ascii")
    result = build_color_filter(ColorSettings(mode="dji_lut", lut_path=str(lut)), RenderProfile())
    assert result.count("lut3d=") == 1
