from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_smoke,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def test_tiny_media_pipeline_preserves_source_and_decodes_frame(tmp_path: Path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "smoke.mp4"
    source_command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=8000",
        "-t",
        "0.6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-shortest",
        str(source),
    ]
    created = subprocess.run(source_command, capture_output=True, text=True, check=False)
    assert created.returncode == 0, created.stderr

    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    transcode = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            "scale=160:90",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-t",
            "0.5",
            "-c:a",
            "aac",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert transcode.returncode == 0, transcode.stderr
    assert output.is_file() and output.stat().st_size > 0

    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    metadata = json.loads(probe.stdout)
    video = next(stream for stream in metadata["streams"] if stream.get("codec_type") == "video")
    assert (int(video["width"]), int(video["height"])) == (160, 90)
    assert float(metadata["format"]["duration"]) == pytest.approx(0.5, abs=0.2)

    decoded = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(output), "-frames:v", "1", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.tmp"))
