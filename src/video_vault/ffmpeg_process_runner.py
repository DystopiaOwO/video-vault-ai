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
        self._process: subprocess.Popen[str] | None = None
        self._cancel_sent = False

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
        process = subprocess.Popen(managed_command, **popen_kwargs)
        with self._lock:
            self._process = process
            self._cancel_sent = False
        if self.on_process:
            self.on_process(process.pid)
        if self.on_log:
            self.on_log(f"FFmpeg PID: {process.pid}\ncommand_args_count: {len(managed_command)}")

        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        stdout_thread = threading.Thread(target=_read_stream, args=(process.stdout, "stdout", events), daemon=True)
        stderr_thread = threading.Thread(target=_read_stream, args=(process.stderr, "stderr", events), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        stdout_tail = _TailBuffer(256 * 1024)
        stderr_tail = _TailBuffer(self.stderr_limit)
        progress_values: dict[str, str] = {}
        closed_streams = 0
        cancelled = False
        try:
            while process.poll() is None or closed_streams < 2 or not events.empty():
                if self.cancel_event.is_set() and not cancelled:
                    cancelled = True
                    self._terminate_process(process)
                try:
                    stream_name, line = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    closed_streams += 1
                    continue
                if stream_name == "stderr":
                    stderr_tail.add(line)
                    if self.on_log:
                        self.on_log(line.rstrip())
                    continue
                stdout_tail.add(line)
                if "=" not in line:
                    continue
                key, value = line.rstrip("\r\n").split("=", 1)
                progress_values[key] = value
                fraction = parse_ffmpeg_progress(progress_values, expected_duration_seconds)
                if fraction is not None and self.on_progress:
                    self.on_progress(fraction, dict(progress_values))
            while process.poll() is None:
                time.sleep(0.01)
            returncode = int(process.returncode or 0)
            if cancelled or self.cancel_event.is_set():
                raise RenderCancelled("render cancellation requested")
            return ManagedRunResult(returncode, stdout_tail.value(), stderr_tail.value())
        finally:
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            with self._lock:
                self._process = None
            if self.on_process:
                self.on_process(None)

    def request_cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            process = self._process
            already_sent = self._cancel_sent
            self._cancel_sent = True
        if process is not None and process.poll() is None and not already_sent:
            self._terminate_process(process)

    def current_pid(self) -> int | None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return None
            return int(self._process.pid)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        pid = int(process.pid)
        if self.on_log:
            self.on_log(f"cancel requested; terminating PID tree rooted at {pid}")
        terminate_process_tree(pid, process=process, on_log=self.on_log)


def terminate_process_tree(pid: int, *, process: subprocess.Popen[Any] | None = None, on_log: Callable[[str], None] | None = None, grace_seconds: float = 0.75) -> None:
    """Terminate only the process group rooted at pid."""
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, text=True, check=False)
        if on_log:
            on_log(f"termination: taskkill /PID {pid} /T /F returncode={result.returncode}")
        if result.returncode != 0 and process is not None:
            process.terminate()
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        if on_log:
            on_log(f"termination: SIGTERM process group {pid}")
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.05, grace_seconds)
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return
        time.sleep(0.02)
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        if on_log:
            on_log(f"termination: SIGKILL process group {pid}")
    except ProcessLookupError:
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
    finally:
        events.put((name, None))


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


__all__ = ["ManagedFFmpegRunner", "ManagedRunResult", "parse_ffmpeg_progress", "terminate_process_tree"]
