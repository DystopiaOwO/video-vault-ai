from video_vault.scanner import scan_inbox


def test_scan_video_extensions(tmp_path):
    inbox = tmp_path / "00_inbox"
    inbox.mkdir()
    (inbox / "a.mp4").write_text("")
    (inbox / "b.txt").write_text("")
    assert [p.name for p in scan_inbox({"library_root": str(tmp_path), "inbox_dir": "00_inbox"})] == ["a.mp4"]
