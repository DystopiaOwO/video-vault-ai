from pathlib import Path

from video_vault.ffmpeg_tools import extract_frames, frame_timestamp


def test_extract_frames_seeks_per_timestamp(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)

    monkeypatch.setattr("video_vault.ffmpeg_tools.subprocess.run", fake_run)
    monkeypatch.setattr("video_vault.ffmpeg_tools.metadata", lambda path, cfg: {"duration_seconds": 11})
    extract_frames(Path("clip.mp4"), tmp_path, {"ffmpeg_path": "ffmpeg", "frame_interval_seconds": 5, "frame_height": 720})
    assert [cmd[cmd.index("-ss") + 1] for cmd in calls] == ["0", "5", "10"]
    assert str(calls[0][-1]).endswith("frame_00000.jpg")


def test_frame_timestamp_from_sequence_name():
    assert frame_timestamp(Path("frame_00003.jpg"), {"frame_interval_seconds": 5}) == 15
