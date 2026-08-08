"""Read-only, auditable local environment health checks.

The doctor is deliberately separate from repair workflows.  It never installs
packages, edits configuration, downloads models, or writes production library
data.  ``full`` mode may create and remove a temporary fixture only.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from .config import DEFAULT_CONFIG, load_config
from .paths import db_path


DOCTOR_SCHEMA_VERSION = "doctor-v1"
DOCTOR_STATUSES = frozenset({"pass", "warning", "blocked", "skipped"})
DOCTOR_MODES = frozenset({"default", "quick", "full"})


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
    text = re.sub(r"(?i)(api[_-]?key|token|authorization|secret)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer <redacted>", text)
    text = re.sub(r"(?<![\w])(?:[A-Za-z]:[\\/]|/)(?:[^\s,;]+)", "<redacted-path>", text)
    return text[:240]


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
    clean_evidence = dict(evidence or {})
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


def _hyperframes_check(repo: Path) -> dict[str, Any]:
    runtime = repo / "tools" / "hyperframes"
    package = runtime / "package.json"
    node_modules = runtime / "node_modules"
    if not package.is_file():
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames package 不存在", evidence={"package_json": False}, remediation="恢復 pinned HyperFrames runtime。")
    if not node_modules.is_dir():
        return _check("frontend.hyperframes", "frontend", "blocked", "HyperFrames node_modules 不存在；未自動下載", evidence={"package_json": True, "node_modules": False}, remediation="由使用者在隔離/受控流程安裝 pinned dependencies。")
    return _check("frontend.hyperframes", "frontend", "pass", "HyperFrames runtime 檔案存在", evidence={"package_json": True, "node_modules": True})


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
        try:
            with urlopen(base_url + "/models", timeout=2) as response:
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


def _cloud_config_check(cfg: Mapping[str, Any]) -> dict[str, Any]:
    raw_enabled = ((cfg.get("perception") or {}).get("cloud_review") or {}).get("enabled")
    enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled or "").strip().lower() not in {"", "0", "false", "no", "off"}
    if not enabled:
        return _check("provider.cloud_review", "provider", "skipped", "cloud review 未啟用，未檢查或呼叫付費 provider", evidence={"enabled": False, "network_request": False})
    return _check("provider.cloud_review", "provider", "warning", "cloud review 已啟用；doctor 僅檢查設定，不發送付費請求", evidence={"enabled": True, "network_request": False}, remediation="正式使用前確認 quota、timeout 與 provider budget。")


def _sqlite_fixture_check(repo: Path, mode: str) -> dict[str, Any]:
    if mode != "full":
        return _check("storage.sqlite", "storage", "skipped", "quick/default mode 未建立隔離 SQLite fixture", evidence={"fixture": "skipped"})
    try:
        with tempfile.TemporaryDirectory(prefix="video-vault-doctor-") as raw:
            db = Path(raw) / "健檢 fixture" / "doctor.sqlite3"
            db.parent.mkdir(parents=True)
            from .database import init_db

            init_db(db)
            connection = sqlite3.connect(db)
            try:
                connection.execute("create table if not exists doctor_probe (value text)")
                connection.execute("begin")
                connection.execute("insert into doctor_probe values ('rollback')")
                connection.rollback()
                remaining = int(connection.execute("select count(*) from doctor_probe").fetchone()[0])
            finally:
                connection.close()
        return _check("storage.sqlite", "storage", "pass" if remaining == 0 else "blocked", "isolated SQLite migration/rollback probe passed" if remaining == 0 else "SQLite rollback probe failed", evidence={"fixture": "isolated", "rollback_clean": remaining == 0})
    except Exception as exc:
        return _check("storage.sqlite", "storage", "blocked", "isolated SQLite fixture failed", evidence={"fixture": "isolated", "error_code": type(exc).__name__}, remediation="檢查 Python SQLite runtime 與 migration。")


def _media_fixture_check(cfg: Mapping[str, Any], mode: str) -> dict[str, Any]:
    if mode != "full":
        return _check("media.behavior", "runtime.media", "skipped", "quick/default mode 未執行 FFmpeg/FFprobe behavior probe", evidence={"probe": "skipped"})
    ffmpeg = _resolve_executable(cfg.get("ffmpeg_path"))
    ffprobe = _resolve_executable(cfg.get("ffprobe_path"))
    if not ffmpeg or not ffprobe:
        return _check("media.behavior", "runtime.media", "blocked", "FFmpeg/FFprobe 不可用，無法執行 fixture probe", evidence={"ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe)}, remediation="先修正 FFmpeg/FFprobe dependency。")
    try:
        with tempfile.TemporaryDirectory(prefix="video-vault-doctor-media-") as raw:
            output = Path(raw) / "unicode fixture.mp4"
            generated = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:r=5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "0.5", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(output)], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
            if generated.returncode != 0 or not output.is_file():
                return _check("media.behavior", "runtime.media", "blocked", "FFmpeg fixture encode failed", evidence={"ffmpeg_returncode": generated.returncode}, remediation="確認 FFmpeg codec 與暫存目錄能力。")
            probed = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(output)], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
            duration = (probed.stdout or "").strip()
            ok = probed.returncode == 0 and bool(duration)
        return _check("media.behavior", "runtime.media", "pass" if ok else "blocked", "isolated FFmpeg/FFprobe fixture probe passed" if ok else "FFprobe fixture probe failed", evidence={"ffmpeg": True, "ffprobe": ok, "unicode_path": True})
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


def _asset_checks(repo: Path, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    package_json = repo / "web" / "package.json"
    package_lock = repo / "web" / "package-lock.json"
    checks = [
        _check("configuration.web_package", "configuration", "pass" if package_json.is_file() else "blocked", "WebUI package.json 存在" if package_json.is_file() else "WebUI package.json 不存在", evidence={"exists": package_json.is_file()}, remediation="恢復 WebUI package.json。" if not package_json.is_file() else None),
        _check("configuration.web_lockfile", "configuration", "pass" if package_lock.is_file() else "warning", "WebUI lockfile 存在" if package_lock.is_file() else "WebUI lockfile 不存在", evidence={"exists": package_lock.is_file()}, remediation="補齊 package-lock.json 以固定 dependency。" if not package_lock.is_file() else None),
    ]
    font = str(((cfg.get("render") or {}).get("visual_font_path") or "")).strip()
    if not font:
        checks.append(_check("asset.font", "assets", "skipped", "未指定自訂字型，使用系統 fallback", evidence={"configured": False}))
    else:
        exists = Path(font).expanduser().is_file()
        checks.append(_check("asset.font", "assets", "pass" if exists else "warning", "自訂字型可用" if exists else "自訂字型不存在，將使用 fallback", evidence={"configured": True, "exists": exists}, remediation=None if exists else "修正字型設定或移除自訂字型路徑。"))
    checks.append(_check("asset.bgm", "assets", "skipped", "未要求特定 BGM asset；doctor 不下載或修改素材", evidence={"network_request": False}))
    return checks


def collect_doctor_report(
    config_file: str | Path = "config.yaml",
    *,
    mode: str = "default",
    dev: bool = False,
    repo_root: Path | None = None,
    config_override: Mapping[str, Any] | None = None,
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
    checks: list[dict[str, Any]] = [_timed(config_result["check_id"], config_result["category"], lambda result=config_result: result)]
    minimum = _minimum_python(repo)
    actual_python = (sys.version_info.major, sys.version_info.minor)
    checks.append(_timed("runtime.python", "runtime", lambda: _check("runtime.python", "runtime", "pass" if actual_python >= minimum else "blocked", f"Python {actual_python[0]}.{actual_python[1]} {'符合' if actual_python >= minimum else '低於'}最低需求", evidence={"major": actual_python[0], "minor": actual_python[1], "minimum": f">={minimum[0]}.{minimum[1]}"}, remediation=None if actual_python >= minimum else "使用符合 pyproject requires-python 的 Python。")))

    library_value = str(cfg.get("library_root") or "").strip()
    if library_value:
        checks.append(_timed("storage.library_root", "storage", lambda: _directory_check("storage.library_root", Path(library_value), writable=True)))
        database_parent = db_path(cfg).parent
        checks.append(_timed("storage.database_parent", "storage", lambda: _directory_check("storage.database_parent", database_parent, writable=True)))
    else:
        checks.extend([
            _check("storage.library_root", "storage", "blocked", "library_root 未設定", evidence={"configured": False}, remediation="設定 library_root。"),
            _check("storage.database_parent", "storage", "blocked", "無法計算 SQLite parent", evidence={"configured": False}, remediation="先修正 library_root。"),
        ])
    checks.append(_timed("runtime.media.ffmpeg", "runtime.media", lambda: _command_check("runtime.media.ffmpeg", cfg.get("ffmpeg_path"), mode=selected_mode)))
    checks.append(_timed("runtime.media.ffprobe", "runtime.media", lambda: _command_check("runtime.media.ffprobe", cfg.get("ffprobe_path"), mode=selected_mode)))
    checks.append(_timed("frontend.node", "frontend", lambda: _node_check(repo, selected_mode)))
    checks.append(_timed("frontend.hyperframes", "frontend", lambda: _hyperframes_check(repo)))
    checks.append(_timed("provider.active", "provider", lambda: _provider_check(cfg, selected_mode)))
    checks.append(_timed("provider.cloud_review", "provider", lambda: _cloud_config_check(cfg)))
    checks.append(_timed("media.behavior", "runtime.media", lambda: _media_fixture_check(cfg, selected_mode)))
    checks.append(_timed("storage.sqlite", "storage", lambda: _sqlite_fixture_check(repo, selected_mode)))
    checks.append(_timed("web.loopback", "frontend", lambda: _loopback_fixture_check(selected_mode)))
    checks.extend(_asset_checks(repo, cfg))
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


def collect_doctor_report_from_config(cfg: Mapping[str, Any], *, mode: str = "default", repo_root: Path | None = None) -> dict[str, Any]:
    """Collect a report from the already-loaded UI config without re-reading it."""
    return collect_doctor_report("config.yaml", mode=mode, repo_root=repo_root, config_override=cfg)


def run_doctor(
    config_file: str | Path = "config.yaml",
    *,
    json_output: bool | str | Path = False,
    mode: str = "default",
    dev: bool = False,
) -> int:
    report = collect_doctor_report(config_file, mode=mode, dev=dev)
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
