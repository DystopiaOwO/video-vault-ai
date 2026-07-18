from pathlib import Path
import os
import subprocess
import sys
import threading
import time

import pytest

from video_vault.ffmpeg_process_runner import ManagedFFmpegRunner, parse_ffmpeg_progress
from video_vault.render_job_models import RenderCancelled


def test_parse_progress_prefers_out_time_us_and_clamps():
    assert parse_ffmpeg_progress({"out_time_us": "500000", "out_time": "00:00:09.000"}, 1) == pytest.approx(0.5)
    assert parse_ffmpeg_progress({"out_time_us": "2000000"}, 1) == 1
    assert parse_ffmpeg_progress({"out_time_us": "bad", "out_time": "00:00:00.250"}, 1) == pytest.approx(0.25)


def test_runner_consumes_progress_and_large_stderr_without_deadlock():
    code = "import sys,time; [ (print(f'out_time_us={i*250000}', flush=True), print('e'*20000, file=sys.stderr, flush=True), time.sleep(.02)) for i in range(5) ]; print('progress=end', flush=True)"
    progress = []
    runner = ManagedFFmpegRunner(on_progress=lambda fraction, values: progress.append(fraction))
    result = runner.run([sys.executable, "-c", code], expected_duration_seconds=1)
    assert result.returncode == 0
    assert progress
    assert progress == sorted(progress)
    assert len(result.stderr.encode("utf-8")) <= 2 * 1024 * 1024
    assert runner.current_pid() is None


def test_runner_cancel_terminates_only_its_process_tree(tmp_path: Path):
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(r'{child_pid_file}').write_text(str(p.pid)); "
        "print('out_time_us=0',flush=True); time.sleep(30)"
    )
    runner = ManagedFFmpegRunner()
    result = {}

    def run():
        try:
            runner.run([sys.executable, "-c", code], expected_duration_seconds=30)
        except Exception as exc:  # noqa: BLE001 - assertion below checks the concrete cancellation type.
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while runner.current_pid() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner.current_pid() is not None
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    runner.request_cancel()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), RenderCancelled)
    assert not _pid_exists(child_pid)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
