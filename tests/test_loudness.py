from pathlib import Path

from video_vault.loudness import AAC_TRUE_PEAK_HEADROOM_DB, LoudnessMeasurement, build_second_pass_command


def test_second_pass_loudness_reserves_aac_true_peak_headroom():
    measurement = LoudnessMeasurement(
        measured_I=-44.38,
        measured_LRA=2.7,
        measured_TP=-23.44,
        measured_thresh=-54.41,
        offset=0.24,
        target_lufs=-14.0,
        true_peak_db=-4.0,
    )

    command = build_second_pass_command(
        "ffmpeg",
        Path("assembled.mp4"),
        Path("normalized.mp4"),
        {"audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        measurement,
        duration_seconds=62.734,
    )
    filter_graph = command[command.index("-af") + 1]

    assert "linear=false" in filter_graph
    assert "linear=true" not in filter_graph
    assert "alimiter=limit=0.50118723:level=false" in filter_graph
    assert command[command.index("-t") + 1] == "62.734000"
    assert AAC_TRUE_PEAK_HEADROOM_DB == 2.0
