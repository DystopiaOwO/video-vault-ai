"""Per-job process groups and FFmpeg progress parsing."""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping, Sequence


@dataclass(frozen=True)
class ProgressSnapshot:
    out_time_ms: int = 0
    progress: str = "continue"
    speed: str = ""
    frame: int | None = None
    fps: float | None = None
    percent: float = 0.0


class FFmpegProgressParser:
    def __init__(self, duration_ms: int):
        self.duration_ms, self._values, self._percent = max(0, int(duration_ms)), {}, 0.0

    def feed(self, line: str) -> ProgressSnapshot | None:
        if "=" not in line: return None
        key, value = line.strip().split("=", 1)
        self._values[key] = value
        if key not in {"out_time_ms", "progress", "speed", "frame", "fps"}: return None
        try: out_time = int(float(self._values.get("out_time_ms", 0)))
        except ValueError: out_time = 0
        percent = self._percent if not self.duration_ms else out_time * 100 / self.duration_ms
        self._percent = max(self._percent, min(100.0, percent))
        if self._values.get("progress") == "end": self._percent = 100.0
        try: frame = int(float(self._values["frame"]))
        except (KeyError, ValueError): frame = None
        try: fps = float(self._values["fps"])
        except (KeyError, ValueError): fps = None
        return ProgressSnapshot(out_time, self._values.get("progress", "continue"),
                                self._values.get("speed", ""), frame, fps, self._percent)

    def parse(self, lines: Sequence[str]) -> list[ProgressSnapshot]:
        return [snapshot for line in lines if (snapshot := self.feed(line)) is not None]


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str] | None = None
    stderr_file: IO[str] | None = None


class FFmpegProcessManager:
    def __init__(self): self._processes: dict[str, _ManagedProcess] = {}

    def start(self, job_id: str, command: Sequence[str], *, cwd: str | Path | None = None,
              env: Mapping[str, str] | None = None, stdout_path: str | Path | None = None,
              stderr_path: str | Path | None = None) -> subprocess.Popen[str]:
        if job_id in self._processes and self.is_running(job_id):
            raise RuntimeError(f"process already running for job {job_id}")
        stdout = _open_log(stdout_path); stderr = _open_log(stderr_path)
        kwargs: dict[str, object] = {"cwd": str(cwd) if cwd else None,
            "env": dict(env) if env else None, "stdout": stdout or subprocess.PIPE,
            "stderr": stderr or subprocess.PIPE, "text": True, "bufsize": 1}
        if os.name == "nt": kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else: kwargs["start_new_session"] = True
        try: process = subprocess.Popen(list(command), **kwargs)
        except Exception:
            _close(stdout, stderr); raise
        self._processes[job_id] = _ManagedProcess(process, stdout, stderr)
        return process

    def get(self, job_id: str) -> subprocess.Popen[str] | None:
        managed = self._processes.get(job_id); return managed.process if managed else None

    def is_running(self, job_id: str) -> bool:
        process = self.get(job_id); return process is not None and process.poll() is None

    def wait(self, job_id: str, timeout: float | None = None) -> int:
        managed = self._require(job_id)
        try: return managed.process.wait(timeout=timeout)
        finally:
            if managed.process.poll() is not None: self._close_job(job_id)

    def run(self, job_id: str, command: Sequence[str], *, timeout: float | None = None, **kwargs: object) -> int:
        self.start(job_id, command, **kwargs)
        try: return self.wait(job_id, timeout)
        except subprocess.TimeoutExpired:
            self.terminate(job_id); raise

    def terminate(self, job_id: str, *, timeout: float = 5.0) -> bool:
        managed = self._require(job_id); process = managed.process
        if process.poll() is not None: self._close_job(job_id); return False
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else: os.killpg(process.pid, signal.SIGTERM)
        try: process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
            else: os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=timeout)
        finally: self._close_job(job_id)
        return True

    def close(self) -> None:
        for job_id in list(self._processes):
            if self.is_running(job_id): self.terminate(job_id)
            else: self._close_job(job_id)

    def _require(self, job_id: str) -> _ManagedProcess:
        if job_id not in self._processes: raise KeyError(f"unknown render job: {job_id}")
        return self._processes[job_id]

    def _close_job(self, job_id: str) -> None:
        managed = self._processes.pop(job_id, None)
        if managed: _close(managed.stdout_file, managed.stderr_file)


def _open_log(path: str | Path | None) -> IO[str] | None:
    if path is None: return None
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8", newline="\n")


def _close(*streams: IO[str] | None) -> None:
    for stream in streams:
        if stream: stream.close()


__all__ = ["FFmpegProcessManager", "FFmpegProgressParser", "ProgressSnapshot"]
