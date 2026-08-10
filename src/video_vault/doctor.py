"""Read-only, auditable local environment health checks.

The doctor is deliberately separate from repair workflows.  It never installs
packages, edits configuration, downloads models, or writes production library
data.  ``full`` mode may create and remove a temporary fixture only.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import gc
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys as _system_module
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DEFAULT_CONFIG, load_config
from .paths import db_path


DOCTOR_SCHEMA_VERSION = "doctor-v1"
DOCTOR_STATUSES = frozenset({"pass", "warning", "blocked", "skipped"})
DOCTOR_MODES = frozenset({"default", "quick", "full"})
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|authorization|secret|password|credential|private[_-]?key)", re.IGNORECASE)
_MEDIA_SUFFIX = re.compile(r"\.(?:mp4|mov|mkv|avi|webm|m4v|mp3|wav|m4a|aac|flac|jpg|jpeg|png|webp|gif|ttf|otf|cube)(?:$|[?#])", re.IGNORECASE)
_WINDOWS_LONG_PATH_THRESHOLD = 260
_WINDOWS_LONG_PATH_COMPONENT_LIMIT = 180
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DOCTOR_PROBE_IMAGES = {
    "red": "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAb0lEQVR4nO3PAQkAAAyEwO9feoshgnABdLep8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3IPanc8OLDQitxAAAAAElFTkSuQmCC",
    "green": "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAb0lEQVR4nO3PAQkAAAyEwO9feoshgnABdLu58QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ24Pbjs8OLsrFddAAAAAElFTkSuQmCC",
    "blue": "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAb0lEQVR4nO3PAQkAAAyEwO9feoshgnABdNvJ8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ2oPcf88OIhvJ6vAAAAAElFTkSuQmCC",
    "yellow": "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAdklEQVR4nO3PQQkAMAzAwPo33Yno4xgEIuAyu/N1XtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWjBsQcyl+HSnr9raQAAAABJRU5ErkJggg==",
    "purple": "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAdklEQVR4nO3PQQkAMAzAwEqv9Ino4xgEIuAyO/t1wwUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWHHvPigDxMpG8LwAAAABJRU5ErkJggg==",
}
_STORY_RESERVED_OUTPUT_TOKENS = 2048


def _optional_bool(value: object, *, default: bool = False) -> tuple[bool, str | None]:
    """Parse optional config booleans without treating arbitrary strings as true."""

    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 0:
            return False, None
        if value == 1:
            return True, None
        return default, "invalid_boolean_number"
    normalized = str(value).strip().lower()
    if normalized in _FALSE_VALUES:
        return False, None
    if normalized in _TRUE_VALUES:
        return True, None
    return default, "invalid_boolean_value"


def _safe_float(value: object, *, default: float = 0.0) -> tuple[float, str | None]:
    try:
        parsed = default if value in (None, "") else float(value)
    except (TypeError, ValueError, OverflowError):
        return default, "invalid_number"
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return default, "invalid_number"
    return parsed, None


class _SystemProxy:
    """Keep test overrides local; mutating doctor.sys must not mutate pytest."""

    version_info = _system_module.version_info
    executable = _system_module.executable


sys = _SystemProxy()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _minimum_python(repo_root: Path) -> tuple[int, int]:
    try:
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return (3, 11)
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)', text)
    return (int(match.group(1)), int(match.group(2))) if match else (3, 11)


def _redact_text(value: object) -> str:
    """Keep evidence useful without returning paths, tokens, or key values."""

    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|authorization|secret|password|credential)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*)://[^\s/@]+:[^\s/@]+@", r"\1://<redacted>@", text)
    text = re.sub(r"(?<![\w])(?:\\\\[^\s,;]+|[A-Za-z]:[\\/][^\s,;]+|/(?:[^\s,;/]+/)+[^\s,;]*)", "<redacted-path>", text)
    if _MEDIA_SUFFIX.search(text):
        return "<redacted-media-name>"
    text = re.sub(r"(?<![\w])(?:[A-Za-z]:[\\/]|/)(?:[^\s,;]+)", "<redacted-path>", text)
    return text[:240]


def _redact_value(value: object, *, key: str = "") -> object:
    """Recursively sanitize all report evidence before it crosses a boundary."""

    key_token = str(key or "")
    if (value is None or isinstance(value, (int, float))) and _SENSITIVE_KEY.search(key_token) and any(marker in key_token.lower() for marker in ("context", "estimated", "reserved", "count", "capacity")):
        return value
    if _SENSITIVE_KEY.search(key_token) and not key_token.lower().endswith(("_present", "_configured", "_env", "_name")):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(item, key=key_token) for item in value]
    if isinstance(value, (str, Path)):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


def _check(
    check_id: str,
    category: str,
    status: str,
    summary: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    remediation: str | None = None,
    sensitive: bool = False,
    duration_ms: int = 0,
) -> dict[str, Any]:
    if status not in DOCTOR_STATUSES:
        raise ValueError(f"unsupported doctor status: {status}")
    clean_evidence = _redact_value(dict(evidence or {}))
    return {
        "check_id": check_id,
        "category": category,
        "status": status,
        "summary": _redact_text(summary),
        "evidence": clean_evidence,
        "remediation": _redact_text(remediation) if remediation else None,
        "duration_ms": max(0, int(duration_ms)),
        "sensitive": bool(sensitive),
        # Compatibility aliases for the classic report consumers.  The
        # canonical status remains pass/warning/blocked/skipped.
        "name": check_id,
        "message": _redact_text(summary),
        "required": status == "blocked",
    }


def _timed(check_id: str, category: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:  # diagnostics must report, not crash the CLI
        result = _check(
            check_id,
            category,
            "blocked",
            "健檢執行失敗",
            evidence={"error_code": type(exc).__name__},
            remediation="檢查本機環境後重新執行 doctor。",
        )
    result["duration_ms"] = round((time.perf_counter() - started) * 1000)
    return result


def _resolve_executable(command: object) -> str | None:
    value = str(command or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return str(resolved) if resolved.is_file() else None
    return shutil.which(value)


def _command_check(
    check_id: str,
    command: object,
    *,
    mode: str,
    category: str = "runtime.media",
    flag: str = "-version",
) -> dict[str, Any]:
    configured = str(command or "").strip()
    resolved = _resolve_executable(configured)
    if not configured or not resolved:
        return _check(check_id, category, "blocked", "必要 executable 不可用", evidence={"available": False}, remediation="安裝或在設定中提供必要 executable。")
    if mode == "quick":
        return _check(check_id, category, "pass", "executable 可找到；quick mode 未執行行為 probe", evidence={"available": True, "probe": "skipped"})
    try:
        result = subprocess.run([resolved, flag], capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return _check(check_id, category, "blocked", "executable probe timeout", evidence={"available": True, "probe": "timeout"}, remediation="確認 executable 可正常啟動。")
    except OSError as exc:
        return _check(check_id, category, "blocked", "executable probe failed", evidence={"available": True, "error_code": type(exc).__name__}, remediation="確認 executable 與執行權限。")
    first_line = (result.stdout or result.stderr or "").splitlines()
    version = _redact_text(first_line[0] if first_line else "")
    if result.returncode != 0:
        return _check(check_id, category, "blocked", "executable probe returned non-zero", evidence={"available": True, "returncode": result.returncode, "version": version}, remediation="確認安裝版本與 PATH/設定。")
    return _check(check_id, category, "pass", "executable 行為 probe 通過", evidence={"available": True, "returncode": 0, "version": version})


def _directory_check(check_id: str, path: Path, *, writable: bool, category: str = "storage") -> dict[str, Any]:
    try:
        exists = path.expanduser().is_dir()
        readable = exists and os.access(path, os.R_OK)
    except OSError:
        exists = readable = False
    writable_access = exists and os.access(path, os.W_OK) if writable else False
    evidence = {"exists": exists, "readable": readable, "writable": writable_access}
    if not exists or not readable:
        return _check(check_id, category, "blocked", "目錄不存在或不可讀取", evidence=evidence, remediation="確認設定的目錄存在且目前使用者可讀取。")
    if writable and not writable_access:
        return _check(check_id, category, "blocked", "目錄不可寫入", evidence=evidence, remediation="確認目錄權限或改用可寫入的 library root。")
    if not writable:
        return _check(check_id, category, "pass", "目錄可讀取", evidence=evidence)
    return _check(check_id, category, "pass", "目錄可讀寫", evidence=evidence)


def _config_check(config_file: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not config_file.expanduser().is_file():
        return dict(DEFAULT_CONFIG), _check("configuration.parse", "configuration", "blocked", "config 檔案不存在", evidence={"exists": False}, remediation="建立可解析的 config.yaml。")
    try:
        raw = config_file.read_text(encoding="utf-8")
        malformed = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#") and ":" not in line]
        if malformed:
            raise ValueError("設定行缺少 ':'")
        cfg = load_config(str(config_file))
        if not isinstance(cfg.get("library_root"), str) or not str(cfg.get("library_root")).strip():
            raise ValueError("library_root 未設定")
        if not isinstance(cfg.get("ffmpeg_path"), str) or not isinstance(cfg.get("ffprobe_path"), str):
            raise ValueError("ffmpeg_path / ffprobe_path 必須是字串")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return dict(DEFAULT_CONFIG), _check("configuration.parse", "configuration", "blocked", "config 無法讀取或解析", evidence={"error_code": type(exc).__name__}, remediation="修正 config.yaml 格式與必要欄位。")
    return cfg, _check("configuration.parse", "configuration", "pass", "config 可讀取且可解析", evidence={"parsed": True})


def _node_check(repo: Path, mode: str) -> dict[str, Any]:
    package = repo / "web" / "package.json"
    if not package.is_file():
        return _check("frontend.node", "frontend", "blocked", "web/package.json 不存在", evidence={"package_json": False}, remediation="恢復 WebUI package.json。")
    return _node_engine_check(package, mode=mode)


def _node_engine_check(package_json: Path, *, mode: str = "default") -> dict[str, Any]:
    """Check the declared Node engine without adding a semver dependency."""

    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
        required = str((package.get("engines") or {}).get("node") or "").strip()
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return _check("frontend.node", "frontend", "blocked", "無法讀取 Node engines", evidence={"error_code": type(exc).__name__}, remediation="修正 web/package.json。")
    if not required:
        return _check("frontend.node", "frontend", "warning", "web/package.json 未宣告 engines.node", evidence={"engine_declared": False})
    resolved = shutil.which("node")
    if not resolved:
        return _check("frontend.node", "frontend", "blocked", "Node executable 不可用", evidence={"engine": required, "available": False}, remediation="安裝符合 WebUI/HyperFrames contract 的 Node。")
    if mode == "quick":
        return _check("frontend.node", "frontend", "pass", "Node executable 可找到；quick mode 未執行 version probe", evidence={"engine": required, "available": True, "probe": "skipped"})
    try:
        result = subprocess.run([resolved, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("frontend.node", "frontend", "blocked", "Node version probe failed", evidence={"engine": required, "error_code": type(exc).__name__}, remediation="確認 Node 可正常啟動。")
    raw_version = (result.stdout or result.stderr or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    match = re.search(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw_version)
    if result.returncode != 0 or not match:
        return _check("frontend.node", "frontend", "blocked", "無法判讀 Node version", evidence={"engine": required, "probe": "failed"}, remediation="安裝可辨識版本的 Node。")
    actual = tuple(int(match.group(index) or 0) for index in (1, 2, 3))
    comparisons: list[tuple[str, tuple[int, int, int]]] = []
    for clause in required.replace(",", " ").split():
        parsed = re.fullmatch(r"(>=|<=|>|<|=)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", clause)
        if not parsed:
            return _check("frontend.node", "frontend", "warning", "無法解析 engines.node", evidence={"engine": required, "version": _redact_text(raw_version)})
        comparisons.append((parsed.group(1) or "=", tuple(int(parsed.group(index) or 0) for index in (2, 3, 4))))
    def satisfies(operator: str, expected: tuple[int, int, int]) -> bool:
        return {">=": actual >= expected, ">": actual > expected, "<=": actual <= expected, "<": actual < expected, "=": actual == expected}[operator]
    compatible = all(satisfies(operator, expected) for operator, expected in comparisons)
    return _check("frontend.node", "frontend", "pass" if compatible else "warning", f"Node {_redact_text(raw_version)} {'符合' if compatible else '不符合'} engines.node", evidence={"engine": required, "version": _redact_text(raw_version), "compatible": compatible}, remediation=None if compatible else "切換至符合 engines.node 的 Node 版本。")


def _hyperframes_check(repo: Path, mode: str = "default") -> dict[str, Any]:
    runtime = repo / "tools" / "hyperframes"
    package = runtime / "package.json"
    package_lock = runtime / "package-lock.json"
    node_modules = runtime / "node_modules"
    if not package.is_file():
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames package 不存在", evidence={"package_json": False}, remediation="恢復 pinned HyperFrames runtime。")
    try:
        package_data = json.loads(package.read_text(encoding="utf-8"))
        lock_data = json.loads(package_lock.read_text(encoding="utf-8")) if package_lock.is_file() else {}
        declared = str((package_data.get("dependencies") or {}).get("hyperframes") or "")
        locked = str(((lock_data.get("packages") or {}).get("node_modules/hyperframes") or {}).get("version") or "")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames package/lockfile 無法解析", evidence={"package_json": package.is_file(), "package_lock": package_lock.is_file(), "error_code": type(exc).__name__}, remediation="修正 pinned HyperFrames package-lock.json。")
    if not package_lock.is_file() or not declared or not locked or not _semver_satisfies(locked, declared):
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames package-lock 與 declared dependency 不一致", evidence={"package_lock": package_lock.is_file(), "declared": declared, "locked": locked, "contract_consistent": False}, remediation="使用 repository 內 pinned lockfile 安裝，不要由 doctor 自動 npm install。")
    if not node_modules.is_dir():
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames node_modules 不存在；未自動下載", evidence={"package_json": True, "package_lock": True, "node_modules": False, "probe": "not_run"}, remediation="由使用者在隔離/受控流程安裝 pinned dependencies。")
    node = shutil.which("node.exe") or shutil.which("node")
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not node or not npx:
        return _check("frontend.hyperframes", "frontend", "blocked", "Node/npx 不可用，無法驗證 HyperFrames subprocess", evidence={"node": bool(node), "npx": bool(npx), "probe": "not_run"}, remediation="安裝符合 HyperFrames engines 的 Node.js 與 npm。")
    if mode == "quick":
        return _check("frontend.hyperframes", "frontend", "pass", "HyperFrames package/lock/node_modules presence sanity 通過；quick 未執行 subprocess", evidence={"package_json": True, "package_lock": True, "node_modules": True, "probe": "presence_sanity", "subprocess": "skipped"})
    env = os.environ.copy()
    env.update({"HYPERFRAMES_NO_TELEMETRY": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_NO_AUTO_INSTALL": "1", "DO_NOT_TRACK": "1"})
    fixture = tempfile.mkdtemp(prefix="video-vault-doctor-hyperframes-")
    fixture_path = Path(fixture)
    result_check: dict[str, Any]
    try:
        command = [npx, "--no-install", "--prefix", str(runtime), "hyperframes", "render", "--help"]
        result = subprocess.run(command, cwd=fixture_path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
        ok = result.returncode == 0
        result_check = _check("frontend.hyperframes", "frontend", "pass" if ok else "blocked", "HyperFrames offline formal subprocess probe passed" if ok else "HyperFrames offline formal subprocess probe failed", evidence={"package_json": True, "package_lock": True, "node_modules": True, "probe": "formal_subprocess", "offline": True, "no_install": True, "returncode": result.returncode}, remediation=None if ok else "確認 pinned HyperFrames runtime 可在 offline/no-install 模式啟動。")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result_check = _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames offline formal subprocess probe failed", evidence={"probe": "formal_subprocess", "offline": True, "no_install": True, "error_code": type(exc).__name__}, remediation="確認 Node、npx 與 pinned HyperFrames runtime。")
    finally:
        shutil.rmtree(fixture_path, ignore_errors=True)
    result_check["evidence"]["fixture_cleaned_up"] = not fixture_path.exists()
    if not result_check["evidence"]["fixture_cleaned_up"]:
        result_check["status"] = "blocked"
        result_check["summary"] = "HyperFrames probe fixture cleanup failed"
    return result_check


def _semver_satisfies(version: str, constraint: str) -> bool:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(version).strip())
    if not match:
        return False
    actual = tuple(int(match.group(index) or 0) for index in (1, 2, 3))
    clauses = str(constraint).replace(",", " ").split()
    for clause in clauses:
        item = re.fullmatch(r"(>=|<=|>|<|=|\^)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", clause)
        if not item:
            return False
        expected = tuple(int(item.group(index) or 0) for index in (2, 3, 4))
        op = item.group(1) or "="
        if op == "^":
            if actual < expected or actual[0] != expected[0]:
                return False
        elif not {">=": actual >= expected, ">": actual > expected, "<=": actual <= expected, "<": actual < expected, "=": actual == expected}[op]:
            return False
    return True


def _provider_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    ai = dict(cfg.get("ai") or {})
    provider = str(ai.get("provider") or "mock").lower()
    if provider == "mock":
        return _check("provider.active", "provider", "pass", "mock provider 可用且不會發生外部請求", evidence={"provider": "mock", "network_request": False})
    if provider == "local":
        local = dict(ai.get("local") or {})
        base_url = str(local.get("base_url") or local.get("lmstudio_url") or "").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return _check("provider.active", "provider", "blocked", "local provider endpoint 設定無效", evidence={"provider": "local", "endpoint_valid": False}, remediation="設定可用的 OpenAI-compatible local endpoint。")
        if mode != "full":
            return _check("provider.active", "provider", "skipped", "quick/default mode 未連線 local provider", evidence={"provider": "local", "connectivity_probe": "skipped"}, remediation="使用 --full 執行 local endpoint connectivity probe。")
        scope = _validate_local_endpoint_scope(base_url)
        if scope.get("status") != "pass":
            return _check("provider.active", "provider", "blocked", "local provider endpoint 不符合 loopback-only network policy", evidence={"provider": "local", **scope}, remediation="只允許解析至 loopback 的 127.0.0.1、::1 或 localhost endpoint。")
        try:
            with urlopen(str(scope["validated_endpoint"]) + "/models", timeout=2) as response:
                ok = 200 <= int(response.status) < 300
        except (OSError, URLError, ValueError):
            ok = False
        return _check("provider.active", "provider", "pass" if ok else "blocked", "local provider connectivity probe 通過" if ok else "local provider 無法連線", evidence={"provider": "local", "connectivity_probe": "pass" if ok else "failed"}, remediation=None if ok else "啟動 local provider 或修正 endpoint。")
    if provider == "cloud":
        cloud = dict(ai.get("cloud") or {})
        env_name = str(cloud.get("api_key_env") or "OPENAI_API_KEY")
        present = bool(os.environ.get(env_name))
        return _check("provider.active", "provider", "pass" if present else "blocked", "cloud provider 設定存在；未發送付費請求" if present else "cloud provider API key 缺失", evidence={"provider": "cloud", "api_key_present": present, "network_request": False}, remediation=None if present else "由使用者設定 API key，再重新執行 doctor。")
    return _check("provider.active", "provider", "blocked", "不支援的 AI provider", evidence={"provider": provider}, remediation="改用 mock、local 或 cloud provider。")


def _local_models(base_url: str) -> tuple[bool, list[dict[str, Any]]]:
    try:
        with urlopen(base_url.rstrip("/") + "/models", timeout=2) as response:
            if not (200 <= int(response.status) < 300):
                return False, []
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, Mapping) else []
        return True, [dict(item) for item in data if isinstance(item, Mapping)]
    except (OSError, URLError, ValueError, TypeError, json.JSONDecodeError):
        return False, []


def _validate_local_endpoint_scope(base_url: str) -> dict[str, Any]:
    """Validate and pin a local endpoint to a loopback address before any request."""

    evidence: dict[str, Any] = {
        "network_scope_policy": "loopback_only",
        "configured_endpoint": str(base_url),
        "validated_network_scope": "blocked",
        "resolved_addresses": [],
        "dns_validation": "not_attempted",
    }
    parsed = urlparse(str(base_url).rstrip("/"))
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return {**evidence, "status": "blocked", "error_code": "invalid_local_endpoint"}

    resolved: list[str] = []
    if host == "localhost":
        try:
            infos = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
            for info in infos:
                address = str(info[4][0]).split("%", 1)[0]
                if address not in resolved:
                    resolved.append(address)
            evidence["dns_validation"] = "all_addresses_checked"
        except (OSError, socket.gaierror):
            return {**evidence, "status": "blocked", "error_code": "loopback_hostname_unresolved", "dns_validation": "failed"}
    else:
        try:
            parsed_ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return {**evidence, "status": "blocked", "error_code": "host_not_loopback_allowlist", "dns_validation": "not_applicable"}
        resolved = [str(parsed_ip)]
        evidence["dns_validation"] = "ip_literal"

    evidence["resolved_addresses"] = resolved
    try:
        addresses = [ipaddress.ip_address(address) for address in resolved]
    except ValueError:
        return {**evidence, "status": "blocked", "error_code": "dns_address_invalid"}
    if not addresses or not all(address.is_loopback for address in addresses):
        return {**evidence, "status": "blocked", "error_code": "dns_resolved_non_loopback"}

    selected = next((address for address in addresses if address.version == 4), addresses[0])
    selected_host = str(selected)
    netloc = f"[{selected_host}]" if selected.version == 6 else selected_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    validated_endpoint = parsed._replace(netloc=netloc).geturl().rstrip("/")
    return {
        **evidence,
        "status": "pass",
        "validated_network_scope": "loopback",
        "validated_endpoint": validated_endpoint,
        "selected_loopback_address": selected_host,
    }


def _response_text_content(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [str(item.get("text")) for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
        return "".join(text_parts) if text_parts else None
    return None


def _parse_probe_json(content: object) -> Mapping[str, Any] | None:
    text = _response_text_content(content)
    if not text:
        return None
    normalized = text.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        normalized = normalized[3:-3].strip()
        if normalized.lower().startswith("json"):
            normalized = normalized[4:].lstrip()
    try:
        parsed = json.loads(normalized)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _select_probe_colors() -> list[str]:
    return random.SystemRandom().sample(list(_DOCTOR_PROBE_IMAGES), 3)


def _local_multi_image_behavior_probe(base_url: str, model_name: str, *, endpoint_scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Prove ordered runtime color understanding at a validated local endpoint."""
    scope = dict(endpoint_scope or _validate_local_endpoint_scope(base_url))
    challenge_order = _select_probe_colors()
    images = [
        {"label": label, "data_url": f"data:image/png;base64,{_DOCTOR_PROBE_IMAGES[label]}"}
        for label in challenge_order
    ]
    request_body = {
        "model": str(model_name),
        "temperature": 0,
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": "Identify the dominant color of each of the three images in input order. Use one simple English color name per image, not hex codes. Return only a JSON object with an image_count integer and an order array. Do not explain."}]
            + [{"type": "image_url", "image_url": {"url": image["data_url"]}} for image in images],
        }],
    }
    evidence: dict[str, Any] = {
        "probe": "local_multi_image_behavior",
        "endpoint": "validated_loopback_endpoint_only",
        "image_count": 3,
        "expected_image_labels": [image["label"] for image in images],
        "challenge_selection": "runtime_random_three_of_five",
        "expected_response": {"image_count": 3, "order": challenge_order},
        "validated_network_scope": scope.get("validated_network_scope", "blocked"),
        "resolved_addresses": scope.get("resolved_addresses", []),
        "network_scope_policy": scope.get("network_scope_policy", "loopback_only"),
        "cloud_fallback": False,
        "paid_call": False,
        "config_mutated": False,
        "model_download": False,
        "generation_post_attempted": False,
        "generation_post_calls": 0,
    }
    if scope.get("status") != "pass":
        return {**evidence, "status": "blocked", "error_code": scope.get("error_code", "local_endpoint_scope_blocked"), "scope_validation": scope}
    request = Request(
        str(scope["validated_endpoint"]).rstrip("/") + "/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        evidence["generation_post_attempted"] = True
        evidence["generation_post_calls"] = 1
        with urlopen(request, timeout=30) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read().decode("utf-8", errors="replace")
        evidence["http_status"] = status
        if status < 200 or status >= 300:
            return {**evidence, "status": "endpoint_unavailable", "error_code": "http_non_success"}
        payload = json.loads(raw)
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
        parsed = _parse_probe_json(content)
        expected = evidence["expected_response"]
        if parsed is None:
            return {**evidence, "status": "malformed_response", "error_code": "missing_chat_completion_content"}
        semantic_valid = parsed.get("image_count") == expected["image_count"] and parsed.get("order") == expected["order"]
        if not semantic_valid:
            return {**evidence, "status": "blocked", "error_code": "image_semantics_mismatch", "parsed_response": dict(parsed)}
        return {**evidence, "status": "pass", "capability": "verified_by_behavior", "response_shape": "chat_completion", "parsed_response": dict(parsed), "semantic_validation": "image_count_and_order_match"}
    except HTTPError as exc:
        evidence.update({"http_status": int(exc.code), "error_code": "http_error"})
        if int(exc.code) in {400, 405, 415, 422}:
            return {**evidence, "status": "model_incapable"}
        return {**evidence, "status": "endpoint_unavailable"}
    except (URLError, TimeoutError, OSError) as exc:
        return {**evidence, "status": "endpoint_unavailable", "error_code": type(exc).__name__}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {**evidence, "status": "malformed_response", "error_code": "invalid_json_response"}


def _provider_model_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    ai = dict(cfg.get("ai") or {})
    provider = str(ai.get("provider") or "mock").lower()
    if provider == "mock":
        return _check("provider.model", "provider", "pass", "mock model contract 可用", evidence={"provider": "mock", "model": "rules", "model_exists": True, "network_request": False})
    if provider == "cloud":
        cloud = dict(ai.get("cloud") or {})
        model = str(cloud.get("model") or "").strip()
        key_present = bool(os.environ.get(str(cloud.get("api_key_env") or "OPENAI_API_KEY")))
        return _check("provider.model", "provider", "pass" if model and key_present else "blocked", "cloud model contract 已設定；doctor 不呼叫 provider" if model and key_present else "cloud model 或 API key contract 缺失", evidence={"provider": "cloud", "model_configured": bool(model), "api_key_present": key_present, "model_exists": "unverified", "network_request": False}, remediation=None if model and key_present else "設定 cloud model 與 API key。")
    if provider != "local":
        return _check("provider.model", "provider", "blocked", "不支援的 provider model contract", evidence={"provider": provider, "model_exists": False}, remediation="改用支援的 mock、local 或 cloud provider。")
    local = dict(ai.get("local") or {})
    base_url = str(local.get("base_url") or local.get("lmstudio_url") or "").rstrip("/")
    model = str(local.get("model") or "").strip()
    if not base_url or not model:
        return _check("provider.model", "provider", "blocked", "local endpoint/model contract 缺失", evidence={"provider": "local", "endpoint_configured": bool(base_url), "model_configured": bool(model)}, remediation="設定 local provider endpoint 與 model。")
    if mode != "full":
        return _check("provider.model", "provider", "skipped", "default/quick 未查詢 local model existence", evidence={"provider": "local", "model_configured": True, "model_exists": "unverified", "probe": "skipped"}, remediation="使用 --full 查詢 local /models。")
    scope = _validate_local_endpoint_scope(base_url)
    if scope.get("status") != "pass":
        return _check("provider.model", "provider", "blocked", "local endpoint 不符合 loopback-only network policy", evidence={"provider": "local", "model_configured": True, **scope}, remediation="只允許解析至 loopback 的 127.0.0.1、::1 或 localhost endpoint。")
    reachable, models = _local_models(str(scope["validated_endpoint"]))
    names = {str(item.get("id") or item.get("name") or "") for item in models}
    exists = model in names
    return _check("provider.model", "provider", "pass" if reachable and exists else "blocked", "local configured model 存在" if reachable and exists else "local endpoint/model 不可用", evidence={"provider": "local", "connectivity": reachable, "model_exists": exists, "model_configured": True, "model_count": len(models)}, remediation=None if reachable and exists else "啟動 local endpoint 並確認設定 model 與 /models 一致。")


def _provider_capability_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    ai = dict(cfg.get("ai") or {})
    provider = str(ai.get("provider") or "mock").lower()
    required = [str(item) for item in (ai.get("required_capabilities") or ["vision", "multi_image"]) if str(item).strip()]
    supported = {"vision", "multi_image"}
    if provider == "mock":
        return _check("provider.capabilities", "provider", "pass", "mock vision/multi-image capability contract 可用", evidence={"provider": "mock", "required": required, "verified": sorted(set(required) & supported), "network_request": False})
    if provider == "cloud":
        return _check("provider.capabilities", "provider", "skipped", "cloud capability 不以 doctor 發送付費 request 驗證", evidence={"provider": "cloud", "required": required, "verified": [], "network_request": False}, remediation="由 cloud provider integration acceptance 驗證 capability。")
    if provider != "local":
        return _check("provider.capabilities", "provider", "blocked", "unsupported provider capability contract", evidence={"provider": provider, "required": required}, remediation="改用支援 provider。")
    if mode != "full":
        return _check("provider.capabilities", "provider", "skipped", "default/quick 未驗證 local vision/multi-image capability", evidence={"provider": "local", "required": required, "verified": [], "metadata_capability": {"status": "not_checked"}, "behavior_capability": {"status": "skipped"}, "probe": "skipped"}, remediation="使用 --full 執行 local metadata 與 behavior capability probe。")
    local = dict(ai.get("local") or {})
    base_url = str(local.get("base_url") or local.get("lmstudio_url") or "").rstrip("/")
    model_name = str(local.get("model") or "")
    scope = _validate_local_endpoint_scope(base_url)
    if scope.get("status") != "pass":
        behavior = _local_multi_image_behavior_probe(base_url, model_name, endpoint_scope=scope)
        evidence = {"provider": "local", "model_exists": False, "required": required, "verified": [], "missing": sorted(set(required) - supported), "metadata_capability": {"status": "not_checked", "source": "network_scope_blocked"}, "behavior_capability": behavior, "capability_source": "unverified"}
        return _check("provider.capabilities", "provider", "blocked", "local multi-image behavior probe blocked by network scope", evidence=evidence, remediation="只允許解析至 loopback 的 127.0.0.1、::1 或 localhost endpoint。")
    reachable, models = _local_models(str(scope["validated_endpoint"]))
    model = next((item for item in models if str(item.get("id") or item.get("name") or "") == model_name), None)
    raw_caps = (model or {}).get("capabilities") if isinstance(model, Mapping) else None
    if isinstance(raw_caps, Mapping):
        verified = {str(key) for key, value in raw_caps.items() if value}
    elif isinstance(raw_caps, (list, tuple, set)):
        verified = {str(value) for value in raw_caps}
    else:
        verified = set()
    missing = sorted(set(required) - verified)
    metadata_capability = {
        "status": "verified" if not missing and model is not None else "missing" if model is not None else "unavailable",
        "verified": sorted(verified),
        "missing": missing,
        "source": "provider_model_metadata" if raw_caps is not None else "missing",
    }
    if not reachable:
        behavior = {"status": "endpoint_unavailable", "probe": "not_run", "reason": "models_endpoint_unavailable"}
    elif model is None:
        behavior = {"status": "model_incapable", "probe": "not_run", "reason": "configured_model_missing"}
    else:
        behavior = _local_multi_image_behavior_probe(base_url, model_name, endpoint_scope=scope)
    evidence = {
        "provider": "local",
        "model_exists": model is not None,
        "required": required,
        "verified": sorted(verified),
        "missing": missing,
        "metadata_capability": metadata_capability,
        "behavior_capability": behavior,
        "capability_source": "verified_by_behavior" if behavior.get("status") == "pass" and missing else "provider_metadata+verified_by_behavior" if behavior.get("status") == "pass" else "provider_metadata" if not missing else "unverified",
    }
    if behavior.get("status") == "pass":
        explicit_false = isinstance(raw_caps, Mapping) and raw_caps.get("multi_image") is False
        status = "warning" if explicit_false else "pass"
        summary = "local multi-image behavior probe 通過；metadata 未宣告，已 verified_by_behavior" if missing else "local multi-image metadata/behavior probe 通過"
    else:
        status = "blocked"
        summary = f"local multi-image behavior probe {behavior.get('status') or 'failed'}"
    return _check("provider.capabilities", "provider", status, summary, evidence=evidence, remediation=None if status == "pass" else "確認 local endpoint/model 與 multi-image response contract；doctor 不會 cloud fallback、下載模型或修改設定。")


def _story_provider_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    story = dict(cfg.get("story") or {})
    provider = str(story.get("provider") or "mock").lower()
    model = str(story.get("model") or "")
    local = dict((cfg.get("ai") or {}).get("local") or {})
    context_length = story.get("context_length") or story.get("max_context_length") or story.get("context_capacity_tokens")
    context_source = story.get("context_source")
    if context_length in (None, ""):
        context_length = local.get("context_length") or local.get("max_context_length") or local.get("context_capacity_tokens")
        context_source = context_source or ("config.ai.local.context_length" if context_length not in (None, "") else None)
    try:
        context_length = int(context_length) if context_length not in (None, "") and int(context_length) > 0 else None
    except (TypeError, ValueError):
        context_length = None
    if provider == "mock" and context_length is None:
        context_length = 1_000_000
        context_source = "built_in_mock_contract"
    context_evidence = {
        "context_capacity": context_length,
        "context_capacity_tokens": context_length,
        "context_metadata_source": str(context_source or ("built_in_mock_contract" if provider == "mock" else "unknown")),
        "context_metadata_status": "verified" if context_length or provider == "mock" else "unknown",
        "context_capacity_status": "known" if context_length else "unknown",
        "generation_preflight": "story generation independently fail closed before request",
    }
    if provider == "mock":
        return _check("provider.story", "provider", "pass", "Story Provider mock contract 可用", evidence={"provider": "mock", "model": model or "mock", "model_exists": True, "network_request": False, **context_evidence})
    if provider not in {"local_text", "local"}:
        return _check("provider.story", "provider", "blocked", "不支援的 Story Provider", evidence={"provider": provider, "model": model, "model_exists": False, **context_evidence}, remediation="改用 mock 或 local_text Story Provider。")
    base_url = str(story.get("base_url") or "").rstrip("/")
    if not base_url or not model:
        return _check("provider.story", "provider", "blocked", "Story Provider endpoint/model contract 缺失", evidence={"provider": provider, "model": model, "endpoint_configured": bool(base_url), "model_configured": bool(model), **context_evidence}, remediation="設定 story.base_url 與 story.model。")
    if mode != "full":
        return _check("provider.story", "provider", "skipped", "default/quick 未查詢 Story Provider model existence", evidence={"provider": provider, "model": model, "model_configured": True, "model_exists": "unverified", "probe": "skipped", **context_evidence}, remediation="使用 --full 查詢 Story Provider /models。")
    scope = _validate_local_endpoint_scope(base_url)
    if scope.get("status") != "pass":
        return _check("provider.story", "provider", "blocked", "Story Provider endpoint 不符合 loopback-only network policy", evidence={"provider": provider, "model": model, **scope, **context_evidence}, remediation="只允許解析至 loopback 的 127.0.0.1、::1 或 localhost endpoint。")
    reachable, models = _local_models(str(scope["validated_endpoint"]))
    model_record = next((item for item in models if str(item.get("id") or item.get("name") or "") == model), None)
    exists = model_record is not None
    if context_length is None and model_record is not None:
        model_context = model_record.get("context_length") or model_record.get("max_context_length") or model_record.get("context_window") or model_record.get("context_capacity_tokens")
        try:
            context_length = int(model_context) if model_context not in (None, "") and int(model_context) > 0 else None
        except (TypeError, ValueError):
            context_length = None
        if context_length:
            context_source = "local_endpoint.model_metadata"
            context_evidence.update({"context_capacity": context_length, "context_capacity_tokens": context_length, "context_metadata_source": context_source, "context_metadata_status": "verified"})
    estimated_input = story.get("estimated_input_tokens") or story.get("input_token_estimate")
    try:
        estimated_input = int(estimated_input) if estimated_input not in (None, "") and int(estimated_input) > 0 else None
    except (TypeError, ValueError):
        estimated_input = None
    context_evidence["reserved_output_tokens"] = _STORY_RESERVED_OUTPUT_TOKENS
    context_evidence["estimated_input_tokens"] = estimated_input
    if context_length is None:
        context_evidence["context_capacity_status"] = "unknown"
    elif context_length <= _STORY_RESERVED_OUTPUT_TOKENS or (estimated_input is not None and context_length < estimated_input + _STORY_RESERVED_OUTPUT_TOKENS):
        context_evidence["context_capacity_status"] = "insufficient"
    else:
        context_evidence["context_capacity_status"] = "known"
    if not reachable or not exists:
        status = "blocked"
        summary = "Story Provider model probe failed"
    elif context_evidence["context_capacity_status"] == "insufficient":
        status = "blocked"
        summary = "Story Provider context capacity 明顯不足，generation preflight 將 blocked"
    elif context_evidence["context_capacity_status"] == "unknown":
        status = "warning"
        summary = "Story Provider model 可用，但 context capacity unknown；generation preflight 仍會 fail closed"
    else:
        status = "pass"
        summary = "Story Provider model/context contract 通過"
    return _check("provider.story", "provider", status, summary, evidence={"provider": provider, "model": model, "connectivity": reachable, "model_exists": exists, "model_count": len(models), "network_scope": "local", **context_evidence}, remediation=None if status == "pass" else "提供可稽核 context metadata，或縮減 Story input / 改用足夠 context 的本地模型；不會自動切 cloud。")


def _cloud_config_check(cfg: Mapping[str, Any]) -> dict[str, Any]:
    perception_raw = cfg.get("perception")
    if perception_raw in (None, ""):
        perception: Mapping[str, Any] = {}
    elif isinstance(perception_raw, Mapping):
        perception = perception_raw
    else:
        return _check("provider.cloud_review", "provider", "blocked", "perception 設定必須是 object", evidence={"error_code": "expected_object", "network_request": False}, remediation="修正 perception 設定格式。")
    review_raw = perception.get("cloud_review")
    if review_raw in (None, ""):
        review: dict[str, Any] = {}
    elif isinstance(review_raw, Mapping):
        review = dict(review_raw)
    else:
        return _check("provider.cloud_review", "provider", "blocked", "cloud_review 設定必須是 object", evidence={"error_code": "expected_object", "network_request": False}, remediation="修正 perception.cloud_review 設定格式。")
    raw_enabled = review.get("enabled")
    enabled, enabled_error = _optional_bool(raw_enabled)
    if enabled_error:
        return _check("provider.cloud_review", "provider", "blocked", "cloud review enabled 設定無效", evidence={"enabled": "invalid", "error_code": enabled_error, "network_request": False}, remediation="enabled 只能使用 true/false、yes/no、on/off 或 1/0。")
    if not enabled:
        return _check("provider.cloud_review", "provider", "skipped", "cloud review 未啟用，未檢查或呼叫付費 provider", evidence={"enabled": False, "network_request": False})
    ai_raw = cfg.get("ai")
    ai = ai_raw if isinstance(ai_raw, Mapping) else {}
    cloud_raw = ai.get("cloud")
    cloud = dict(cloud_raw) if isinstance(cloud_raw, Mapping) else {}
    key_name = str(cloud.get("api_key_env") or "OPENAI_API_KEY")
    timeout, timeout_error = _safe_float(review.get("timeout_seconds"), default=0.0)
    complete = not timeout_error and bool(review.get("provider")) and bool(cloud.get("model")) and bool(os.environ.get(key_name)) and timeout > 0
    return _check("provider.cloud_review", "provider", "pass" if complete else "blocked", "cloud review config/key contract 通過；未發送付費請求" if complete else "cloud review enabled contract 不完整", evidence={"enabled": True, "provider_configured": bool(review.get("provider")), "model_configured": bool(cloud.get("model")), "api_key_present": bool(os.environ.get(key_name)), "timeout_configured": timeout > 0 if not timeout_error else False, "timeout_error_code": timeout_error, "network_request": False}, remediation=None if complete else "設定 cloud review provider、model、API key 與有效 timeout；doctor 不會發送付費 request。")


def _sqlite_fixture_check(repo: Path, mode: str) -> dict[str, Any]:
    if mode != "full":
        return _check("storage.sqlite", "storage", "skipped", "quick/default mode 未建立隔離 SQLite fixture", evidence={"fixture": "skipped"})
    fixture_root: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="video-vault-doctor-") as raw:
            fixture_root = Path(raw)
            db = fixture_root / "健檢 fixture" / "doctor.sqlite3"
            db.parent.mkdir(parents=True)
            legacy_connection = sqlite3.connect(db)
            try:
                legacy_connection.executescript(
                    """
                    create table videos (
                        id integer primary key,
                        original_path text unique,
                        current_path text,
                        filename text,
                        category text,
                        status text default 'new'
                    );
                    create table frames (
                        id integer primary key,
                        video_id integer,
                        timestamp_seconds real,
                        frame_path text,
                        vision_summary text
                    );
                    create table segments (
                        id integer primary key,
                        video_id integer,
                        start_seconds real,
                        end_seconds real,
                        segment_type text,
                        title text,
                        reason text,
                        tags text,
                        score real,
                        suggested_use text
                    );
                    create table projects (
                        id integer primary key,
                        name text,
                        status text default 'draft'
                    );
                    create table project_videos (
                        project_id integer not null,
                        video_id integer not null,
                        summary_override text,
                        primary key(project_id, video_id)
                    );
                    create table bgm_tracks (
                        id integer primary key,
                        title text,
                        artist text,
                        file_path text,
                        source_url text,
                        license_name text,
                        license_url text,
                        attribution_required integer default 0
                    );
                    """
                )
                legacy_connection.execute(
                    "insert into videos(id, original_path, current_path, filename, category, status) values(1, ?, ?, ?, ?, ?)",
                    ("legacy-source", "legacy-source", "legacy-clip.mp4", "travel", "perceived"),
                )
                legacy_connection.execute(
                    "insert into frames(id, video_id, timestamp_seconds, frame_path, vision_summary) values(1, 1, 0, ?, ?)",
                    ("legacy-frame", "legacy AI snapshot"),
                )
                legacy_connection.execute(
                    "insert into segments(id, video_id, start_seconds, end_seconds, segment_type, title) values(7, 1, 0, 2, ?, ?)",
                    ("content", "legacy segment"),
                )
                legacy_connection.execute("insert into projects(id, name) values(3, ?)", ("legacy project",))
                legacy_connection.execute(
                    "insert into project_videos(project_id, video_id, summary_override) values(3, 1, ?)",
                    ("legacy user summary",),
                )
                legacy_connection.execute(
                    "insert into bgm_tracks(id, title, license_name, license_url, attribution_required) values(9, ?, ?, ?, 0)",
                    ("legacy bgm", "CC0", "https://license.invalid/cc0"),
                )
                legacy_connection.commit()
            finally:
                legacy_connection.close()

            # This is intentionally the production migration entry point.  Do
            # not create with the current SCHEMA and then validate that same
            # schema; the legacy fixture above must be upgraded in place.
            from .database import init_db

            init_db(db)
            gc.collect()
            connection = sqlite3.connect(db)
            try:
                required_tables = {"videos", "frames", "segments", "analysis_runs", "projects", "project_videos", "bgm_tracks", "story_generations"}
                table_cursor = connection.execute("select name from sqlite_master where type='table'")
                present_tables = {str(row[0]) for row in table_cursor.fetchall()}
                table_cursor.close()
                missing_tables = sorted(required_tables - present_tables)
                expected_columns = {
                    "segments": {"segment_uuid", "revision", "natural_audio_recommendation", "confidence"},
                    "project_videos": {"project_media_uuid", "display_name", "user_summary", "summary_migration_state", "ownership_state"},
                    "bgm_tracks": {"attribution_status", "license_status", "verification_source", "verification_provenance"},
                }
                missing_columns: dict[str, list[str]] = {}
                for table, columns in expected_columns.items():
                    column_cursor = connection.execute(f"pragma table_info({table})")
                    actual_columns = {str(row[1]) for row in column_cursor.fetchall()}
                    column_cursor.close()
                    missing_columns[table] = sorted(columns - actual_columns)
                segment_row = connection.execute("select segment_uuid from segments where id=7").fetchone()
                project_media_row = connection.execute("select project_media_uuid, display_name, user_summary, summary_migration_state from project_videos where project_id=3 and video_id=1").fetchone()
                bgm_row = connection.execute("select attribution_status, license_status, verification_source from bgm_tracks where id=9").fetchone()
                backfill = {
                    "segment_uuid": bool(segment_row and segment_row[0]),
                    "project_media_uuid": bool(project_media_row and project_media_row[0]),
                    "display_name": bool(project_media_row and project_media_row[1] == "legacy-clip.mp4"),
                    "user_summary": bool(project_media_row and project_media_row[2] == "legacy user summary"),
                    "summary_migration_state": bool(project_media_row and project_media_row[3] == "migrated"),
                    "bgm_license_state": bool(bgm_row and bgm_row[0] and bgm_row[1] and bgm_row[2] == "legacy_migration"),
                }
                index_cursor = connection.execute("select name from sqlite_master where type='index'")
                indexes = {str(row[0]) for row in index_cursor.fetchall()}
                index_cursor.close()
                required_indexes = {"idx_segments_segment_uuid", "idx_project_videos_media_uuid"}
                indexes_ok = required_indexes <= indexes
                connection.execute("create table if not exists doctor_probe (value text)")
                connection.execute("begin")
                connection.execute("insert into doctor_probe values ('rollback')")
                connection.rollback()
                remaining_cursor = connection.execute("select count(*) from doctor_probe")
                remaining = int(remaining_cursor.fetchone()[0])
                remaining_cursor.close()
                # Release the read transaction explicitly before Windows tries
                # to remove the temporary SQLite file and directory.
                connection.commit()
            finally:
                connection.close()
        # Windows may retain a SQLite handle until its last Python wrapper is
        # collected; retry cleanup without turning an unverified fixture into
        # a false pass.
        gc.collect()
        for _ in range(3):
            if not fixture_root.exists():
                break
            shutil.rmtree(fixture_root, ignore_errors=True)
            if fixture_root.exists():
                time.sleep(0.05)
        cleaned = fixture_root is not None and not fixture_root.exists()
        valid = remaining == 0 and not missing_tables and all(not values for values in missing_columns.values()) and all(backfill.values()) and indexes_ok and cleaned
        evidence = {
            "fixture": "isolated_legacy_schema",
            "production_init_db": True,
            "migration_contract": "database.init_db",
            "required_table_count": len(required_tables),
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "backfill": backfill,
            "indexes": {name: name in indexes for name in sorted(required_indexes)},
            "rollback_clean": remaining == 0,
            "fixture_cleaned_up": cleaned,
        }
        return _check("storage.sqlite", "storage", "pass" if valid else "blocked", "isolated legacy SQLite migration/backfill/rollback probe passed" if valid else "isolated legacy SQLite migration/backfill/rollback probe failed", evidence=evidence, remediation=None if valid else "檢查 production database.init_db migration、backfill、index 與 temporary fixture cleanup。")
    except Exception as exc:
        return _check("storage.sqlite", "storage", "blocked", "isolated SQLite fixture failed", evidence={"fixture": "isolated", "error_code": type(exc).__name__, "error_message": _redact_text(exc)}, remediation="檢查 Python SQLite runtime 與 migration。")


def _windows_long_path_fixture_path(fixture_root: Path) -> Path:
    """Build a >MAX_PATH fixture from ordinary-sized nested components."""

    components: list[str] = []
    output = fixture_root / "long-path-probe.mp4"
    while len(components) < 4 or len(str(output)) <= _WINDOWS_LONG_PATH_THRESHOLD:
        index = len(components) + 1
        component = f"nested-long-path-{index:02d}-" + ("segment-" * 4) + "fixture"
        if len(component) > _WINDOWS_LONG_PATH_COMPONENT_LIMIT:
            raise ValueError("long-path fixture component exceeds safety limit")
        components.append(component)
        output = fixture_root.joinpath(*components, "long-path-probe.mp4")
    return output


def _windows_extended_path(path: Path) -> str:
    """Return a Windows extended-length path while remaining portable in tests."""
    raw = os.path.abspath(str(path))
    if os.name != "nt":
        return raw
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw.lstrip("\\")
    return "\\\\?\\" + raw


def _extended_path_exists(path: Path) -> bool:
    try:
        return os.path.exists(_windows_extended_path(path))
    except OSError:
        return False


def _media_fixture_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    if mode != "full":
        return _check("media.behavior", "runtime.media", "skipped", "quick/default mode 未執行 FFmpeg/FFprobe behavior probe", evidence={"probe": "skipped"})
    ffmpeg = _resolve_executable(cfg.get("ffmpeg_path"))
    ffprobe = _resolve_executable(cfg.get("ffprobe_path"))
    if not ffmpeg or not ffprobe:
        return _check("media.behavior", "runtime.media", "blocked", "FFmpeg/FFprobe 不可用，無法執行 fixture probe", evidence={"ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe)}, remediation="先修正 FFmpeg/FFprobe dependency。")
    evidence: dict[str, Any] = {"ffmpeg": True, "ffprobe": True, "unicode_path": False, "unicode_verified": False, "codec_h264": False, "codec_aac": False, "long_path_threshold": _WINDOWS_LONG_PATH_THRESHOLD, "long_path_length": None, "max_component_length": None, "long_path_attempted": False, "long_path_verified": "not_verified", "long_path_status": "not_verified", "long_path_mkdir": "not_attempted", "long_path_copy": "not_attempted", "long_path_read": "not_attempted", "long_path_cleanup_attempted": False, "long_path_cleanup_verified": "not_verified", "fixture_cleaned_up": False, "fixture_cleanup_attempted": False, "fixture_cleanup_verified": False}
    fixture_root: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="video-vault-doctor-media-") as raw:
            fixture_root = Path(raw)
            unicode_dir = fixture_root / "VID-6 測試資料" / "旅行素材 中文"
            unicode_dir.mkdir(parents=True)
            output = unicode_dir / "旅遊片段 H264 AAC.mp4"
            evidence["unicode_path"] = any(ord(char) > 127 for char in str(output))
            generated = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:r=5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(output)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
            if generated.returncode != 0 or not output.is_file():
                evidence["ffmpeg_returncode"] = generated.returncode
                evidence["ffmpeg_error"] = "encode_failed"
            else:
                probed = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(output)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
                try:
                    streams = json.loads(probed.stdout or "{}").get("streams") or []
                except (TypeError, ValueError):
                    streams = []
                codec_names = {str(stream.get("codec_name") or "").lower() for stream in streams if isinstance(stream, Mapping)}
                evidence["codec_h264"] = "h264" in codec_names
                evidence["codec_aac"] = "aac" in codec_names
                evidence["unicode_verified"] = probed.returncode == 0 and evidence["unicode_path"] and evidence["codec_h264"] and evidence["codec_aac"]
                long_output = _windows_long_path_fixture_path(fixture_root)
                fixture_components = long_output.relative_to(fixture_root).parts
                evidence["long_path_length"] = len(str(long_output))
                evidence["max_component_length"] = max(len(component) for component in fixture_components)
                if os.name == "nt":
                    long_dir = long_output.parent
                    evidence["long_path_attempted"] = True
                    evidence["long_path_verified"] = False
                    evidence["long_path_mkdir"] = False
                    evidence["long_path_copy"] = False
                    evidence["long_path_read"] = False
                    if evidence["long_path_length"] <= _WINDOWS_LONG_PATH_THRESHOLD or evidence["max_component_length"] > _WINDOWS_LONG_PATH_COMPONENT_LIMIT:
                        evidence["long_path_status"] = "blocked"
                    else:
                        try:
                            Path(_windows_extended_path(long_dir)).mkdir(parents=True)
                            evidence["long_path_mkdir"] = _extended_path_exists(long_dir)
                            shutil.copyfile(_windows_extended_path(output), _windows_extended_path(long_output))
                            evidence["long_path_copy"] = _extended_path_exists(long_output)
                            with open(_windows_extended_path(long_output), "rb") as copied, open(_windows_extended_path(output), "rb") as source:
                                evidence["long_path_read"] = copied.read() == source.read()
                            evidence["long_path_verified"] = bool(evidence["long_path_mkdir"] and evidence["long_path_copy"] and evidence["long_path_read"])
                            evidence["long_path_status"] = "pass" if evidence["long_path_verified"] else "blocked"
                        except OSError as exc:
                            evidence["long_path_error_code"] = type(exc).__name__
                            evidence["long_path_status"] = "blocked"
                    evidence["long_path_cleanup_attempted"] = True
                    try:
                        if _extended_path_exists(long_dir):
                            shutil.rmtree(_windows_extended_path(long_dir))
                        evidence["long_path_cleanup_verified"] = not _extended_path_exists(long_dir)
                    except OSError as exc:
                        evidence["long_path_cleanup_error_code"] = type(exc).__name__
                        evidence["long_path_cleanup_verified"] = False
                else:
                    evidence["long_path_status"] = "skipped"
                    evidence["long_path_cleanup_verified"] = "not_applicable"
            evidence["fixture_cleanup_attempted"] = True
        evidence["fixture_cleaned_up"] = fixture_root is not None and not fixture_root.exists()
        evidence["fixture_cleanup_verified"] = bool(evidence["fixture_cleaned_up"])
        codec_ok = bool(evidence["unicode_verified"])
        long_ok = evidence["long_path_status"] in {"pass", "skipped"} and evidence["long_path_cleanup_verified"] in {True, "not_applicable"}
        cleanup_ok = bool(evidence["fixture_cleaned_up"]) and bool(evidence["fixture_cleanup_verified"])
        status = "blocked" if not codec_ok or not long_ok or not cleanup_ok else "pass"
        summary = "isolated H.264/AAC Unicode-path and Windows long-path probe passed" if status == "pass" and evidence["long_path_status"] == "pass" else "isolated H.264/AAC probe passed; Windows long-path not applicable on this OS" if status == "pass" else "isolated media probe failed or fixture cleanup was incomplete"
        return _check("media.behavior", "runtime.media", status, summary, evidence=evidence, remediation=None if status == "pass" else "確認 FFmpeg codec、Windows long-path policy 與 temporary fixture cleanup。")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("media.behavior", "runtime.media", "blocked", "media fixture probe failed", evidence={"error_code": type(exc).__name__}, remediation="確認 FFmpeg/FFprobe timeout、codec 與暫存權限。")


def _loopback_fixture_check(mode: str) -> dict[str, Any]:
    if mode != "full":
        return _check("web.loopback", "frontend", "skipped", "quick/default mode 未啟動 loopback fixture", evidence={"probe": "skipped"})
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])
        thread = threading.Thread(target=lambda: _accept_probe(server), daemon=True)
        thread.start()
        with closing(socket.create_connection(("127.0.0.1", port), timeout=2)) as client:
            client.sendall(b"doctor")
            response = client.recv(16)
        thread.join(timeout=2)
        return _check("web.loopback", "frontend", "pass" if response == b"ok" else "blocked", "isolated loopback probe passed" if response == b"ok" else "loopback probe returned unexpected response", evidence={"bind": "127.0.0.1", "network_scope": "loopback"})
    except OSError as exc:
        return _check("web.loopback", "frontend", "blocked", "loopback probe failed", evidence={"error_code": type(exc).__name__}, remediation="確認本機 loopback socket 未被政策阻擋。")
    finally:
        server.close()


def _accept_probe(server: socket.socket) -> None:
    try:
        connection, _ = server.accept()
        with closing(connection) as client:
            client.recv(16)
            client.sendall(b"ok")
    except OSError:
        return


def _library_layout_checks(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    library_value = str(cfg.get("library_root") or "").strip()
    if not library_value:
        return [_check("storage.library_layout", "storage", "blocked", "library_root 未設定", evidence={"configured": False}, remediation="設定 library_root 與可用的 inbox/project/render/cache 目錄。")]
    library = Path(library_value).expanduser()
    configured_inbox = str(cfg.get("inbox_dir") or "00_inbox")
    locations = {
        "storage.library_root": library,
        "storage.inbox": Path(configured_inbox) if Path(configured_inbox).is_absolute() else library / configured_inbox,
        "storage.project": library / "08_projects",
        "storage.render": library / "99_exports",
        "storage.cache": library / "02_proxy",
    }
    return [_directory_check(check_id, path, writable=True) for check_id, path in locations.items()]


def _temp_directory_check(mode: str) -> dict[str, Any]:
    temp_root = Path(tempfile.gettempdir())
    if not temp_root.is_dir() or not os.access(temp_root, os.W_OK):
        return _check("runtime.temp", "runtime", "blocked", "OS temporary directory 不可寫入", evidence={"exists": temp_root.is_dir(), "writable": False}, remediation="確認 OS temporary directory 權限。")
    if mode != "full":
        return _check("runtime.temp", "runtime", "pass", "OS temporary directory 可用；未執行 full fixture", evidence={"exists": True, "writable": True, "probe": "access_only"})
    fixture_root = Path(tempfile.mkdtemp(prefix="video-vault-doctor-temp-"))
    cleaned = False
    try:
        probe = fixture_root / "CJK 測試 temp.txt"
        probe.write_text("doctor", encoding="utf-8")
        ok = probe.read_text(encoding="utf-8") == "doctor"
    except (OSError, UnicodeError) as exc:
        return _check("runtime.temp", "runtime", "blocked", "OS temporary directory fixture failed", evidence={"exists": True, "writable": True, "error_code": type(exc).__name__}, remediation="確認 OS temporary directory 可建立、讀取與清理檔案。")
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)
        cleaned = not fixture_root.exists()
    status = "pass" if ok and cleaned else "blocked"
    return _check("runtime.temp", "runtime", status, "OS temporary directory fixture passed" if status == "pass" else "OS temporary directory fixture cleanup failed", evidence={"exists": True, "writable": True, "unicode_file": ok, "fixture_cleaned_up": cleaned}, remediation=None if status == "pass" else "確認 temporary fixture cleanup 與權限。")


def _volume_probe(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return disk evidence for the configured filesystem, never temp fallback."""

    target = path.expanduser()
    probe_path = target
    while not probe_path.exists() and probe_path != probe_path.parent:
        probe_path = probe_path.parent
    try:
        usage = shutil.disk_usage(probe_path)
    except OSError as exc:
        return None, {"error_code": type(exc).__name__, "path_exists": target.exists()}
    drive, _ = os.path.splitdrive(str(probe_path))
    return {"free_bytes": int(usage.free), "total_bytes": int(usage.total), "path_exists": target.exists(), "volume": drive or probe_path.anchor or "unknown"}, None


def _free_disk_checks(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    temp_path = Path(tempfile.gettempdir())
    library_value = str(cfg.get("library_root") or "").strip()
    library_path = Path(library_value).expanduser() if library_value else Path("")
    render_path = library_path / "99_exports" if library_value else Path("")
    temp_evidence, temp_error = _volume_probe(temp_path)
    temp_status = "blocked" if temp_error or temp_evidence is None else "pass"
    temp_payload = {"filesystem": "os_temp", "configured_path": str(temp_path), **(temp_evidence or temp_error or {})}
    temp_check = _check("runtime.free_disk.temp", "runtime", temp_status, "OS temporary volume free disk 可查詢" if temp_status == "pass" else "無法取得 OS temporary volume free disk", evidence=temp_payload, remediation=None if temp_status == "pass" else "確認 OS temporary volume 可查詢磁碟空間。")

    minimum, minimum_error = _safe_float(((cfg.get("render") or {}).get("minimum_free_disk_bytes") if isinstance(cfg.get("render") or {}, Mapping) else None), default=0.0)
    if minimum_error is None and minimum < 0:
        minimum_error = "negative_number"
    render_evidence, render_error = _volume_probe(render_path) if library_value else (None, {"error_code": "library_root_missing"})
    render_status = "blocked" if minimum_error or render_error or render_evidence is None else "pass" if render_evidence["free_bytes"] >= minimum else "blocked"
    render_payload = {
        "filesystem": "render_library",
        "configured_path": str(render_path),
        "minimum_required_bytes": int(minimum) if minimum >= 0 else 0,
        "minimum_error_code": minimum_error,
        "minimum_applied_to": "render/library volume",
        **(render_evidence or render_error or {}),
    }
    render_check = _check("runtime.free_disk.render", "runtime", render_status, "render/library volume free disk 足夠" if render_status == "pass" else "render/library volume free disk 不足或無法驗證", evidence=render_payload, remediation=None if render_status == "pass" else "確認實際 render/library volume 可用空間與 minimum_free_disk_bytes。")
    return [temp_check, render_check]


def _free_disk_check(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper for callers that expect one render-volume check."""

    return _free_disk_checks(cfg)[1]


def _asset_checks(repo: Path, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    package_json = repo / "web" / "package.json"
    package_lock = repo / "web" / "package-lock.json"
    hyperframes_lock = repo / "tools" / "hyperframes" / "package-lock.json"
    checks = [
        _check("configuration.web_package", "configuration", "pass" if package_json.is_file() else "blocked", "WebUI package.json 存在" if package_json.is_file() else "WebUI package.json 不存在", evidence={"exists": package_json.is_file()}, remediation="恢復 WebUI package.json。" if not package_json.is_file() else None),
        _check("configuration.web_lockfile", "configuration", "pass" if package_lock.is_file() else "blocked", "WebUI lockfile 存在" if package_lock.is_file() else "WebUI lockfile 不存在", evidence={"exists": package_lock.is_file()}, remediation="補齊 package-lock.json 以固定 dependency。" if not package_lock.is_file() else None),
        _check("configuration.web_lockfile_parse", "configuration", "pass", "WebUI lockfile 可解析", evidence={"parse": True}) if package_lock.is_file() and _parse_json_file(package_lock) else _check("configuration.web_lockfile_parse", "configuration", "blocked" if package_lock.is_file() else "skipped", "WebUI lockfile 無法解析" if package_lock.is_file() else "未檢查缺失 WebUI lockfile", evidence={"parse": False}, remediation="修正 web/package-lock.json。" if package_lock.is_file() else None),
        _check("configuration.hyperframes_lockfile", "configuration", "pass" if hyperframes_lock.is_file() else "blocked", "HyperFrames lockfile 存在" if hyperframes_lock.is_file() else "HyperFrames lockfile 不存在", evidence={"exists": hyperframes_lock.is_file()}, remediation="補齊 pinned HyperFrames package-lock.json。" if not hyperframes_lock.is_file() else None),
    ]
    sections: dict[str, dict[str, Any]] = {}
    for section_name in ("render", "color", "bgm", "retention"):
        raw_section = cfg.get(section_name)
        if raw_section in (None, ""):
            sections[section_name] = {}
        elif isinstance(raw_section, Mapping):
            sections[section_name] = dict(raw_section)
        else:
            checks.append(_check(f"configuration.{section_name}", "configuration", "blocked", f"{section_name} 設定必須是 object", evidence={"valid": False, "error_code": "expected_object"}, remediation=f"修正 {section_name} 設定格式。"))
            sections[section_name] = {}
    render = sections["render"]
    font = str((render.get("visual_font_path") or "")).strip()
    if not font:
        checks.append(_check("asset.font", "assets", "skipped", "未指定自訂字型，使用系統 fallback", evidence={"configured": False}))
    else:
        exists = Path(font).expanduser().is_file()
        checks.append(_check("asset.font", "assets", "pass" if exists else "warning", "自訂字型可用" if exists else "自訂字型不存在，將使用 fallback", evidence={"configured": True, "exists": exists}, remediation=None if exists else "修正字型設定或移除自訂字型路徑。"))
    color = sections["color"]
    color_mode = str(color.get("default_mode") or color.get("mode") or "none")
    if color_mode in {"dji_lut", "dji_dlog", "dji_dlog_m"}:
        lut = str(color.get("lut_path") or "")
        checks.append(_check("asset.lut", "assets", "pass" if Path(lut).is_file() else "blocked", "啟用 LUT 可用" if Path(lut).is_file() else "啟用 LUT 但 LUT asset 缺失", evidence={"enabled": True, "exists": Path(lut).is_file()}, remediation=None if Path(lut).is_file() else "提供設定的 .cube LUT asset。"))
    else:
        checks.append(_check("asset.lut", "assets", "skipped", "未啟用 LUT；略過 optional asset", evidence={"enabled": False}))
    bgm = sections["bgm"]
    bgm_enabled, bgm_error = _optional_bool(bgm.get("enabled"))
    if bgm_error:
        checks.append(_check("asset.bgm", "assets", "blocked", "BGM enabled 設定無效", evidence={"enabled": "invalid", "error_code": bgm_error}, remediation="bgm.enabled 只能使用 true/false、yes/no、on/off 或 1/0。"))
    elif bgm_enabled:
        bgm_root = str(bgm.get("root") or bgm.get("directory") or "")
        checks.append(_check("asset.bgm", "assets", "pass" if bgm_root and Path(bgm_root).is_dir() else "blocked", "啟用 BGM asset 可用" if bgm_root and Path(bgm_root).is_dir() else "啟用 BGM 但 asset directory 缺失", evidence={"enabled": True, "exists": bool(bgm_root and Path(bgm_root).is_dir())}, remediation=None if bgm_root and Path(bgm_root).is_dir() else "提供 BGM asset directory。"))
    else:
        checks.append(_check("asset.bgm", "assets", "skipped", "未啟用 BGM；略過 optional asset", evidence={"enabled": False}))
    retention = sections["retention"]
    retention_enabled, retention_error = _optional_bool(retention.get("enabled"))
    if retention_error:
        checks.append(_check("asset.retention", "assets", "blocked", "retention enabled 設定無效", evidence={"enabled": "invalid", "error_code": retention_error}, remediation="retention.enabled 只能使用 true/false、yes/no、on/off 或 1/0。"))
    elif retention_enabled:
        policy = retention.get("policy") if isinstance(retention.get("policy"), Mapping) else retention
        if not isinstance(policy, Mapping):
            checks.append(_check("asset.retention", "assets", "blocked", "retention policy 必須是 object", evidence={"enabled": True, "policy_valid": False, "error_code": "expected_object"}, remediation="修正 retention policy 格式。"))
        else:
            numeric_errors: dict[str, str] = {}
            numeric_values: dict[str, float] = {}
            for key in ("cache_max_age_days", "preview_max_age_days", "failed_grace_days"):
                numeric_values[key], error = _safe_float(policy.get(key), default=0.0)
                if error:
                    numeric_errors[key] = error
                elif numeric_values[key] < 0:
                    numeric_errors[key] = "negative_number"
            valid = not numeric_errors
            checks.append(_check("asset.retention", "assets", "pass" if valid else "blocked", "retention policy contract 可用" if valid else "retention policy contract 無效", evidence={"enabled": True, "policy_valid": valid, "numeric_errors": numeric_errors}, remediation=None if valid else "修正 retention policy 的有效非負數欄位。"))
    else:
        checks.append(_check("asset.retention", "assets", "skipped", "未啟用 retention；略過 optional policy", evidence={"enabled": False}))
    return checks


def _parse_json_file(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, UnicodeError, TypeError, ValueError):
        return False


def collect_doctor_report(
    config_file: str | Path = "config.yaml",
    *,
    mode: str = "default",
    dev: bool = False,
    repo_root: Path | None = None,
    config_override: Mapping[str, Any] | None = None,
    check_id: str | None = None,
) -> dict[str, Any]:
    """Collect a versioned report without changing production state."""

    selected_mode = "full" if dev and mode == "default" else str(mode or "default").lower()
    if selected_mode not in DOCTOR_MODES:
        raise ValueError(f"doctor mode must be one of {sorted(DOCTOR_MODES)}")
    repo = (repo_root or _repo_root()).resolve()
    if config_override is None:
        cfg, config_result = _config_check(Path(config_file))
    else:
        cfg = dict(config_override)
        config_result = _check("configuration.parse", "configuration", "pass", "server config 已由 UI 載入", evidence={"parsed": True, "source": "server_config"})
    checks: list[dict[str, Any]] = []
    requested_check = str(check_id or "").strip() or None

    def add(check: dict[str, Any] | Callable[[], dict[str, Any]], identifier: str | None = None) -> None:
        check_id_value = identifier or (check["check_id"] if isinstance(check, Mapping) else "")
        is_free_disk_compatibility_alias = requested_check == "runtime.free_disk" and check_id_value in {"runtime.free_disk.temp", "runtime.free_disk.render"}
        if requested_check is not None and check_id_value != requested_check and not is_free_disk_compatibility_alias:
            return
        result = check() if callable(check) else check
        checks.append(_timed(result["check_id"], result["category"], lambda result=result: result))

    if requested_check in {None, config_result["check_id"]}:
        add(config_result)
    minimum = _minimum_python(repo)
    actual_python = (sys.version_info.major, sys.version_info.minor)
    add(_check("runtime.python", "runtime", "pass" if actual_python >= minimum else "blocked", f"Python {actual_python[0]}.{actual_python[1]} {'符合' if actual_python >= minimum else '低於'}最低需求", evidence={"major": actual_python[0], "minor": actual_python[1], "minimum": f">={minimum[0]}.{minimum[1]}"}, remediation=None if actual_python >= minimum else "使用符合 pyproject requires-python 的 Python。"))

    library_value = str(cfg.get("library_root") or "").strip()
    if library_value:
        add(_directory_check("storage.library_root", Path(library_value), writable=True))
        database_parent = db_path(cfg).parent
        add(_directory_check("storage.database_parent", database_parent, writable=True))
    else:
        add(_check("storage.library_root", "storage", "blocked", "library_root 未設定", evidence={"configured": False}, remediation="設定 library_root。"))
        add(_check("storage.database_parent", "storage", "blocked", "無法計算 SQLite parent", evidence={"configured": False}, remediation="先修正 library_root。"))
    add(lambda: _command_check("runtime.media.ffmpeg", cfg.get("ffmpeg_path"), mode=selected_mode), "runtime.media.ffmpeg")
    add(lambda: _command_check("runtime.media.ffprobe", cfg.get("ffprobe_path"), mode=selected_mode), "runtime.media.ffprobe")
    add(lambda: _node_check(repo, selected_mode), "frontend.node")
    for item in _library_layout_checks(cfg):
        if item["check_id"] != "storage.library_root":
            add(item)
    add(lambda: _hyperframes_check(repo, selected_mode), "frontend.hyperframes")
    add(lambda: _provider_check(cfg, selected_mode), "provider.active")
    add(lambda: _provider_model_check(cfg, selected_mode), "provider.model")
    add(lambda: _provider_capability_check(cfg, selected_mode), "provider.capabilities")
    add(lambda: _story_provider_check(cfg, selected_mode), "provider.story")
    add(lambda: _cloud_config_check(cfg), "provider.cloud_review")
    add(lambda: _media_fixture_check(cfg, selected_mode), "media.behavior")
    add(lambda: _temp_directory_check(selected_mode), "runtime.temp")
    for item in _free_disk_checks(cfg):
        add(item)
    add(lambda: _sqlite_fixture_check(repo, selected_mode), "storage.sqlite")
    add(lambda: _loopback_fixture_check(selected_mode), "web.loopback")
    for item in _asset_checks(repo, cfg):
        add(item)
    if requested_check and not checks:
        raise ValueError(f"unknown doctor check_id: {requested_check}")
    counts = {status: sum(1 for item in checks if item["status"] == status) for status in sorted(DOCTOR_STATUSES)}
    overall = "blocked" if counts["blocked"] else "warning" if counts["warning"] or counts["skipped"] else "pass"
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "mode": selected_mode,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": overall,
        "ok": overall != "blocked",
        "summary": counts,
        "checks": checks,
        "sensitive_data_redacted": True,
    }


def collect_doctor_report_from_config(cfg: Mapping[str, Any], *, mode: str = "default", repo_root: Path | None = None, check_id: str | None = None) -> dict[str, Any]:
    """Collect a report from the already-loaded UI config without re-reading it."""
    return collect_doctor_report("config.yaml", mode=mode, repo_root=repo_root, config_override=cfg, check_id=check_id)


def run_doctor(
    config_file: str | Path = "config.yaml",
    *,
    json_output: bool | str | Path = False,
    mode: str = "default",
    dev: bool = False,
    check_id: str | None = None,
) -> int:
    report = collect_doctor_report(config_file, mode=mode, dev=dev, check_id=check_id)
    if json_output:
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if isinstance(json_output, (str, Path)) and str(json_output) not in {"", "-", "True", "true"}:
            output = Path(json_output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded + "\n", encoding="utf-8")
        else:
            print(encoded)
    else:
        print(f"Doctor {report['mode']}：{report['status']}（pass={report['summary']['pass']} warning={report['summary']['warning']} blocked={report['summary']['blocked']} skipped={report['summary']['skipped']}）")
        for item in report["checks"]:
            print(f"[{str(item['status']).upper()}] {item['check_id']}: {item['summary']}")
    return 0 if report["ok"] else 1


__all__ = [
    "DOCTOR_MODES",
    "DOCTOR_SCHEMA_VERSION",
    "DOCTOR_STATUSES",
    "collect_doctor_report",
    "collect_doctor_report_from_config",
    "run_doctor",
]
