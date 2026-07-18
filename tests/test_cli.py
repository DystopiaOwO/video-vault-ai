from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault import cli
from video_vault.project_renderer import ProjectRenderError


def _patch_cli(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_config", lambda path: {"library_root": str(tmp_path)})
    monkeypatch.setattr(cli, "db_path", lambda cfg: tmp_path / "video_vault.sqlite3")
    monkeypatch.setattr(cli, "init_db", lambda path: None)


def test_render_project_cli_success_returns_zero_and_prints_output(monkeypatch, tmp_path: Path, capsys):
    _patch_cli(monkeypatch, tmp_path)
    output = tmp_path / "project.mp4"
    monkeypatch.setattr(cli, "render_project", lambda *args, **kwargs: SimpleNamespace(output_path=output))

    cli.main(["render-project", "--project-id", "1"])

    captured = capsys.readouterr()
    assert captured.out.strip() == str(output)
    assert captured.err == ""


@pytest.mark.parametrize("error", [PermissionError("尚未核准"), ProjectRenderError("render failed")])
def test_render_project_cli_failure_returns_nonzero_and_writes_stderr(monkeypatch, tmp_path: Path, capsys, error):
    _patch_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "render_project", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["render-project", "--project-id", "1"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert str(error) in captured.err
    assert captured.out == ""
