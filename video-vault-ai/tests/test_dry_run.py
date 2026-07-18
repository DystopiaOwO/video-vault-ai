from video_vault.cli import dry_run


def test_dry_run_changes_nothing(tmp_path, capsys):
    (tmp_path / "00_inbox").mkdir()
    dry_run({"library_root": str(tmp_path), "inbox_dir": "00_inbox", "ffmpeg_path": "missing-ffmpeg", "ffprobe_path": "missing-ffprobe"})
    assert "no files changed" in capsys.readouterr().out
