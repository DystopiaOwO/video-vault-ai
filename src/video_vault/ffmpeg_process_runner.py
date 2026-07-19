"""Deadlock-safe FFmpeg process management with real progress and targeted cancel."""

from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Mapping

from .render_job_models import RenderCancelled


@dataclass(frozen=True)
class ManagedRunResult:
    returncode: int
    stdout: str
    stderr: str


class ManagedRunnerCallbackError(RuntimeError):
    """A runner callback failed after a process had been started."""

    def __init__(self, callback_name: str, original: BaseException):
        self.callback_name = callback_name
        self.original = original
        super().__init__(f"FFmpeg runner callback {callback_name} failed: {original}")


def parse_ffmpeg_progress(values: Mapping[str, str], expected_duration_seconds: float | None) -> float | None:
    if expected_duration_seconds is None or expected_duration_seconds <= 0:
        return None
    seconds: float | None = None
    raw_us = str(values.get("out_time_us") or "").strip()
    if raw_us and raw_us.lstrip("-").isdigit():
        seconds = int(raw_us) / 1_000_000
    if seconds is None:
        raw_time = str(values.get("out_time") or "").strip()
        parts = raw_time.split(":")
        if len(parts) == 3:
            try:
                hours, minutes, second = parts
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(second)
            except ValueError:
                seconds = None
    if seconds is None:
        return None
    return max(0.0, min(1.0, seconds / float(expected_duration_seconds)))


class ManagedFFmpegRunner:
    """Run one FFmpeg command while consuming progress and stderr concurrently."""

    def __init__(
        self,
        *,
        cancel_event: threading.Event | None = None,
        on_process: Callable[[int | None], None] | None = None,
        on_progress: Callable[[float, dict[str, str]], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        stderr_limit: int = 2 * 1024 * 1024,
    ):
        self.cancel_event = cancel_event or threading.Event()
        self.on_process = on_process
        self.on_progress = on_progress
        self.on_log = on_log
        self.stderr_limit = max(1024, int(stderr_limit))
        self._lock = threading.RLock()
        self._callback_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._process_group_id: int | None = None
        self._cancel_sent = False
        self._log_callback_error: ManagedRunnerCallbackError | None = None

    def __call__(self, command: list[str], **kwargs: Any) -> ManagedRunResult:
        return self.run(command, **kwargs)

    def run(
        self,
        command: list[str],
        *,
        expected_duration_seconds: float | None = None,
        capture_output: bool = True,
        text: bool = True,
        check: bool = False,
    ) -> ManagedRunResult:
        del capture_output, text, check
        if self.cancel_event.is_set():
            raise RenderCancelled("render cancellation requested")

        managed_command = _add_progress_options(command)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "creationflags": creationflags,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        process: subprocess.Popen[str] | None = None
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        primary_error: BaseException | None = None
        result: ManagedRunResult | None = None
        self._log_callback_error = None

        try:
            process = subprocess.Popen(managed_command, **popen_kwargs)
            with self._lock:
                self._process = process
                # start_new_session=True makes the child the session and
                # process-group leader. Using pid avoids the fork/setsid race
                # in an immediate os.getpgid(pid) call.
                self._process_group_id = int(process.pid) if os.name != "nt" else None
                self._cancel_sent = False

            self._invoke_fatal("on_process", self.on_process, process.pid)
            self._safe_log(f"FFmpeg PID: {process.pid}\ncommand_args_count: {len(managed_command)}")

            events: queue.Queue[tuple[str, str | None]] = queue.Queue()
            stdout_thread = threading.Thread(target=_read_stream, args=(process.stdout, "stdout", events), name=f"ffmpeg-stdout-{process.pid}", daemon=True)
            stderr_thread = threading.Thread(target=_read_stream, args=(process.stderr, "stderr", events), name=f"ffmpeg-stderr-{process.pid}", daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            stdout_tail = _TailBuffer(256 * 1024)
            stderr_tail = _TailBuffer(self.stderr_limit)
            progress_values: dict[str, str] = {}
            closed_streams = 0
            cancelled = False
            cancel_deadline: float | None = None

            while process.poll() is None or closed_streams < 2 or not events.empty():
                if self.cancel_event.is_set() and not cancelled:
                    cancelled = True
                    cancel_deadline = time.monotonic() + 2.0
                    self._terminate_process(process)
                    self._close_process_pipes(process)
                # A child in an independent process group may inherit these
                # pipes. Once the managed parent has exited, waiting for EOF
                # from that unrelated child can otherwise block forever.
                if cancelled and process.poll() is not None:
                    self._close_process_pipes(process)
                    break
                if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                    self._close_process_pipes(process)
                    break
                try:
                    stream_name, line = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    closed_streams += 1
                    continue
                if stream_name == "stderr":
                    stderr_tail.add(line)
                    self._safe_log(line.rstrip())
                    continue
                stdout_tail.add(line)
                if "=" not in line:
                    continue
                key, value = line.rstrip("\r\n").split("=", 1)
                progress_values[key] = value
                fraction = parse_ffmpeg_progress(progress_values, expected_duration_seconds)
                if fraction is not None:
                    self._invoke_fatal("on_progress", self.on_progress, fraction, dict(progress_values))

            try:
                returncode = int(process.wait(timeout=2.0))
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                try:
                    returncode = int(process.wait(timeout=2.0))
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("FFmpeg process did not exit before cancellation deadline") from exc
            if cancelled or self.cancel_event.is_set():
                raise RenderCancelled("render cancellation requested")
            result = ManagedRunResult(returncode, stdout_tail.value(), stderr_tail.value())
        except BaseException as exc:
            primary_error = exc
        finally:
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            if process is not None:
                _close_stream(getattr(process, "stdout", None))
                _close_stream(getattr(process, "stderr", None))
            if stdout_thread is not None:
                stdout_thread.join(timeout=2)
            if stderr_thread is not None:
                stderr_thread.join(timeout=2)
            with self._lock:
                self._process = None
                self._process_group_id = None
            cleanup_error = self._invoke_nonfatal("on_process", self.on_process, None)
            if cleanup_error is not None and primary_error is None:
                primary_error = cleanup_error

        if primary_error is not None:
            raise primary_error
        if self._log_callback_error is not None:
            raise self._log_callback_error
        if result is None:
            raise RuntimeError("FFmpeg runner finished without a result")
        return result

    def request_cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            process = self._process
            already_sent = self._cancel_sent
            process_group_id = self._process_group_id
            self._cancel_sent = True
        if process is not None and not already_sent:
            try:
                self._terminate_process(process, process_group_id=process_group_id)
            finally:
                # A separately-created process group may inherit the managed
                # pipes. Closing this runner's descriptors prevents cancel
                # from waiting for an unrelated child to close them.
                self._close_process_pipes(process)

    def current_pid(self) -> int | None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return None
            return int(self._process.pid)

    def _terminate_process(self, process: subprocess.Popen[str], *, process_group_id: int | None = None) -> None:
        pid = int(process.pid)
        self._safe_log(f"cancel requested; terminating PID tree rooted at {pid}")
        terminate_process_tree(pid, process=process, process_group_id=process_group_id or self._process_group_id, on_log=self._safe_log)

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[Any]) -> None:
        _close_stream(getattr(process, "stdout", None))
        _close_stream(getattr(process, "stderr", None))

    def _safe_log(self, message: str) -> None:
        error = self._invoke_nonfatal("on_log", self.on_log, message)
        if error is not None:
            with self._callback_lock:
                if self._log_callback_error is None:
                    self._log_callback_error = error

    @staticmethod
    def _invoke_fatal(name: str, callback: Callable[..., Any] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except BaseException as exc:
            error = ManagedRunnerCallbackError(name, exc)
            raise error from exc

    @staticmethod
    def _invoke_nonfatal(name: str, callback: Callable[..., Any] | None, *args: Any) -> ManagedRunnerCallbackError | None:
        if callback is None:
            return None
        try:
            callback(*args)
        except BaseException as exc:
            error = ManagedRunnerCallbackError(name, exc)
            error.__cause__ = exc
            return error
        return None


def terminate_process_tree(
    pid: int,
    *,
    process: subprocess.Popen[Any] | None = None,
    process_group_id: int | None = None,
    on_log: Callable[[str], None] | None = None,
    grace_seconds: float = 0.75,
) -> None:
    """Terminate only the process group rooted at pid."""
    if os.name == "nt":
        try:
            result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, text=True, check=False)
            _safe_external_log(on_log, f"termination: taskkill /PID {pid} /T /F returncode={result.returncode}")
            if result.returncode == 0:
                return
            _safe_external_log(on_log, f"taskkill stderr: {result.stderr or ''}")
        except OSError as exc:
            _safe_external_log(on_log, f"taskkill failed: {exc}")
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=max(0.05, grace_seconds))
            except (OSError, subprocess.TimeoutExpired) as exc:
                _safe_external_log(on_log, f"parent terminate fallback failed: {exc}")
                try:
                    process.kill()
                except OSError as kill_exc:
                    _safe_external_log(on_log, f"parent kill fallback failed: {kill_exc}")
        return

    pgid = process_group_id
    if pgid is None:
        try:
            pgid = os.getpgid(int(pid))
        except ProcessLookupError:
            return
        except OSError as exc:
            _safe_external_log(on_log, f"getpgid failed for {pid}: {exc}")
            return
    try:
        os.killpg(pgid, signal.SIGTERM)
        _safe_external_log(on_log, f"termination: SIGTERM process group {pgid}")
    except ProcessLookupError:
        return
    except PermissionError as exc:
        _safe_external_log(on_log, f"SIGTERM permission error for process group {pgid}: {exc}")

    deadline = time.monotonic() + max(0.05, grace_seconds)
    while time.monotonic() < deadline:
        if not _process_group_exists(pgid):
            return
        time.sleep(0.02)
    if not _process_group_exists(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
        _safe_external_log(on_log, f"termination: SIGKILL process group {pgid}")
    except ProcessLookupError:
        return
    except PermissionError as exc:
        _safe_external_log(on_log, f"SIGKILL permission error for process group {pgid}: {exc}")
    deadline = time.monotonic() + max(0.05, grace_seconds)
    while time.monotonic() < deadline and _process_group_exists(pgid):
        time.sleep(0.02)


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _initial_process_group_id(pid: int) -> int | None:
    if os.name == "nt":
        return None
    try:
        return int(os.getpgid(int(pid)))
    except (OSError, ProcessLookupError):
        return int(pid)


def _safe_external_log(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except BaseException:
        pass


def _add_progress_options(command: list[str]) -> list[str]:
    result = list(command)
    if "-progress" not in result:
        executable = os.path.basename(str(result[0])).casefold() if result else ""
        options = ["-progress", "pipe:1", "-nostats"]
        if "python" in executable or executable in {"py", "py.exe"}:
            result.extend(options)
        else:
            insert_at = result.index("-nostdin") + 1 if "-nostdin" in result else 1
            result[insert_at:insert_at] = options
    return result


def _read_stream(stream: Any, name: str, events: queue.Queue[tuple[str, str | None]]) -> None:
    if stream is None:
        events.put((name, None))
        return
    try:
        for line in iter(stream.readline, ""):
            events.put((name, line))
    except (OSError, ValueError):
        pass
    finally:
        events.put((name, None))


def _close_stream(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


class _TailBuffer:
    def __init__(self, limit: int):
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0

    def add(self, value: str) -> None:
        self.parts.append(value)
        self.size += len(value.encode("utf-8", errors="replace"))
        while self.size > self.limit and self.parts:
            removed = self.parts.pop(0)
            self.size -= len(removed.encode("utf-8", errors="replace"))

    def value(self) -> str:
        return "".join(self.parts)


__all__ = [
    "ManagedFFmpegRunner",
    "ManagedRunResult",
    "ManagedRunnerCallbackError",
    "parse_ffmpeg_progress",
    "terminate_process_tree",
]
