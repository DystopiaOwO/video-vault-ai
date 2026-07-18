import sys
from video_vault.process_manager import FFmpegProcessManager, FFmpegProgressParser


def test_progress_is_real_and_monotonic():
    parser = FFmpegProgressParser(10000)
    assert parser.feed("out_time_ms=5000").percent == 50
    assert parser.feed("out_time_ms=2000").percent == 50
    assert parser.feed("progress=end").percent == 100


def test_only_requested_process_is_terminated():
    manager = FFmpegProcessManager(); command = [sys.executable, "-c", "import time; time.sleep(30)"]
    first = manager.start("a", command); second = manager.start("b", command)
    try:
        manager.terminate("a")
        assert first.poll() is not None and second.poll() is None
    finally:
        if second.poll() is None: manager.terminate("b")
