from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from video_vault.ffmpeg_process_runner import ManagedFFmpegRunner, ManagedRunnerCallbackError, parse_ffmpeg_progress
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


def test_on_process_callback_failure_cleans_up_process():
    runner = ManagedFFmpegRunner(on_process=lambda pid: (_ for _ in ()).throw(RuntimeError("job write failed")))
    with pytest.raises(ManagedRunnerCallbackError, match="on_process"):
        runner.run([sys.executable, "-c", "import time; time.sleep(30)"])
    assert runner.current_pid() is None


def test_on_progress_callback_failure_terminates_process_tree(tmp_path: Path):
    child_pid_file = tmp_path / "callback-child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(r'{child_pid_file}').write_text(str(p.pid)); "
        "print('out_time_us=500000',flush=True); time.sleep(30)"
    )
    runner = ManagedFFmpegRunner(on_progress=lambda fraction, values: (_ for _ in ()).throw(RuntimeError("progress write failed")))
    result = {}

    def run():
        try:
            runner.run([sys.executable, "-c", code], expected_duration_seconds=1)
        except Exception as exc:  # noqa: BLE001 - assertion below checks callback error.
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), ManagedRunnerCallbackError)
    assert not _pid_exists(int(child_pid_file.read_text(encoding="utf-8")))
    assert runner.current_pid() is None


def test_on_log_callback_failure_is_reported_without_blocking_cleanup():
    runner = ManagedFFmpegRunner(on_log=lambda line: (_ for _ in ()).throw(RuntimeError("log disk full")))
    with pytest.raises(ManagedRunnerCallbackError, match="on_log"):
        runner.run([sys.executable, "-c", "import time; print('hello', flush=True); time.sleep(.1)"])
    assert runner.current_pid() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_posix_stubborn_child_requires_sigkill_fallback(tmp_path: Path):
    child_pid_file = tmp_path / "stubborn-child.pid"
    code = (
        "import pathlib,signal,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
        f"pathlib.Path(r'{child_pid_file}').write_text(str(p.pid)); "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(30)"
    )
    runner = ManagedFFmpegRunner()
    result = {}

    def run():
        try:
            runner.run([sys.executable, "-c", code], expected_duration_seconds=30)
        except Exception as exc:  # noqa: BLE001 - cancellation type is asserted below.
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    runner.request_cancel()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), RenderCancelled)
    assert not _pid_exists(child_pid)
    assert runner.current_pid() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_independent_process_group_survives_runner_cancel(tmp_path: Path):
    helper_pid_file = tmp_path / "independent-helper.pid"
    helper_session_file = tmp_path / "independent-helper.session"
    signal_file = tmp_path / "independent-helper.signal"
    helper_code = (
        "import os,pathlib,signal,time; "
        f"signal.signal(signal.SIGTERM, lambda *_: pathlib.Path(r'{signal_file}').write_text('SIGTERM')); "
        f"signal.signal(signal.SIGHUP, lambda *_: pathlib.Path(r'{signal_file}').write_text('SIGHUP')); "
        f"pathlib.Path(r'{helper_pid_file}').write_text(str(os.getpid())); pathlib.Path(r'{helper_session_file}').write_text(str(os.getsid(0))); "
        "time.sleep(30)"
    )
    code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',\"{helper_code}\"], start_new_session=True); "
        "time.sleep(30)"
    )
    runner = ManagedFFmpegRunner()
    result = {}

    def run():
        try:
            runner.run([sys.executable, "-c", code], expected_duration_seconds=30)
        except Exception as exc:  # noqa: BLE001 - cancellation type is asserted below.
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while not helper_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    helper_pid = int(helper_pid_file.read_text(encoding="utf-8"))
    assert int(helper_session_file.read_text(encoding="utf-8")) == helper_pid
    runner.request_cancel()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), RenderCancelled)
    assert _wait_for_pid_running(helper_pid, timeout=1.0)
    assert not signal_file.exists(), "independent process group received runner termination signal"
    try:
        os.kill(helper_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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


def _wait_for_pid_running(pid: int, timeout: float) -> bool:
    """Wait briefly for a live, non-zombie process after cancellation."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_exists(pid):
            if os.name != "nt":
                try:
                    state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
                    if state == "Z":
                        return False
                except (FileNotFoundError, OSError, IndexError):
                    continue
            return True
        time.sleep(0.02)
    return False
