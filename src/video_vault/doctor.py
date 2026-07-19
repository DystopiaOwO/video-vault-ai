"""Non-destructive environment diagnostics for local development and rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .config import DEFAULT_CONFIG, load_config
from .paths import db_path


def _check(name: str, status: str, required: bool, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "required": required, "message": message}
    if details:
        result["details"] = details
    return result


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _minimum_python(repo_root: Path) -> tuple[int, int]:
    try:
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return (3, 11)
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)', text)
    return (int(match.group(1)), int(match.group(2))) if match else (3, 11)


def _version_check(command: str, timeout: float = 5.0) -> tuple[str | None, str | None, dict[str, Any]]:
    resolved = shutil.which(command)
    if not resolved:
        return None, "command not found", {}
    try:
        result = subprocess.run([resolved, "-version"], capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return resolved, "command timed out", {"resolved": resolved, "timeout_seconds": timeout}
    except OSError as exc:
        return resolved, str(exc), {"resolved": resolved}
    output = (result.stdout or result.stderr or "").splitlines()
    first_line = output[0] if output else ""
    details = {"resolved": resolved, "returncode": result.returncode, "version": first_line}
    if result.returncode != 0:
        return resolved, f"command failed with exit code {result.returncode}", details
    return resolved, first_line, details


def _resolve_executable(command: str) -> str | None:
    candidate = Path(command).expanduser()
    has_path = candidate.is_absolute() or candidate.parent != Path(".")
    if has_path:
        resolved = candidate.resolve()
        return str(resolved) if resolved.is_file() else None
    return shutil.which(command)


def _executable_check(name: str, configured: object) -> dict[str, Any]:
    command = str(configured or "")
    if not command:
        return _check(name, "failed", True, "未設定 executable path")
    resolved = _resolve_executable(command)
    if not resolved:
        return _check(name, "failed", True, f"找不到 {command}", configured=command)
    try:
        result = subprocess.run([resolved, "-version"], capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return _check(name, "failed", True, "version command timeout", configured=command, resolved=resolved)
    except OSError as exc:
        return _check(name, "failed", True, str(exc), configured=command, resolved=resolved)
    first_line = (result.stdout or result.stderr or "").splitlines()
    details = {"configured": command, "resolved": resolved, "returncode": result.returncode, "version": first_line[0] if first_line else ""}
    if result.returncode != 0:
        return _check(name, "failed", True, f"version command failed with exit code {result.returncode}", **details)
    return _check(name, "ok", True, details["version"] or "可執行", **details)


def _path_check(name: str, path: Path, *, writable: bool) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    details = {"resolved": str(resolved)}
    if not resolved.is_dir():
        return _check(name, "failed", True, "不是存在的目錄", **details)
    if not os.access(resolved, os.R_OK):
        return _check(name, "failed", True, "目錄不可讀取", **details)
    if writable:
        probe: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".video_vault_doctor_", dir=resolved, delete=False) as handle:
                handle.write(b"probe")
                probe = Path(handle.name)
        except OSError as exc:
            return _check(name, "failed", True, f"目錄不可寫入：{exc}", **details)
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)
    return _check(name, "ok", True, "目錄可讀寫" if writable else "目錄可讀", **details)


def _config_check(config_file: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = config_file.expanduser().resolve()
    if not resolved.is_file():
        return dict(DEFAULT_CONFIG), _check("config", "failed", True, "config 檔案不存在", path=str(resolved))
    try:
        text = resolved.read_text(encoding="utf-8")
        malformed = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and ":" not in line
        ]
        if malformed:
            raise ValueError(f"缺少 ':' 的設定行：{malformed[0]}")
        cfg = load_config(str(resolved))
        if not isinstance(cfg.get("library_root"), str) or not str(cfg.get("library_root")).strip():
            raise ValueError("library_root 未設定")
        if not isinstance(cfg.get("ffmpeg_path"), str) or not isinstance(cfg.get("ffprobe_path"), str):
            raise ValueError("ffmpeg_path / ffprobe_path 必須是字串")
    except (OSError, UnicodeError, ValueError) as exc:
        return dict(DEFAULT_CONFIG), _check("config", "failed", True, f"config 無法讀取或解析：{exc}", path=str(resolved))
    return cfg, _check("config", "ok", True, "config 可讀取且可解析", path=str(resolved))


def collect_doctor_report(config_file: str | Path = "config.yaml", *, dev: bool = False, repo_root: Path | None = None) -> dict[str, Any]:
    repo = (repo_root or _repo_root()).resolve()
    cfg, config_result = _config_check(Path(config_file))
    checks = [config_result]

    minimum = _minimum_python(repo)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_ok = (sys.version_info.major, sys.version_info.minor) >= minimum
    checks.append(_check("python", "ok" if python_ok else "failed", True, f"Python {version}", minimum=f">={minimum[0]}.{minimum[1]}", executable=sys.executable))

    library_value = str(cfg.get("library_root", "")).strip()
    library = Path(library_value) if library_value else Path()
    if library_value:
        checks.append(_path_check("library root", library, writable=True))
        database = db_path(cfg)
        parent = database.parent.resolve()
        checks.append(_check("database path", "ok" if parent.is_dir() and os.access(parent, os.R_OK | os.W_OK) else "failed", True, "SQLite parent directory 可用" if parent.is_dir() else "SQLite parent directory 不存在", path=str(database.resolve()), parent=str(parent)))
    else:
        checks.append(_check("library root", "failed", True, "未設定 library_root"))
        checks.append(_check("database path", "failed", True, "無法計算 SQLite path"))

    checks.append(_executable_check("ffmpeg", cfg.get("ffmpeg_path")))
    checks.append(_executable_check("ffprobe", cfg.get("ffprobe_path")))

    assets = repo / "web" / "dist" / "index.html"
    checks.append(_check("React assets", "ok" if assets.is_file() else "warning", False, "React build assets 存在" if assets.is_file() else "web/dist 尚未建立，將使用 classic UI", path=str(assets)))

    if dev:
        package_json = repo / "web" / "package.json"
        package_lock = repo / "web" / "package-lock.json"
        checks.append(_node_engine_check(package_json))
        checks.append(_dev_command_check("npm", "--version"))
        checks.append(_check("web/package.json", "ok" if package_json.is_file() else "warning", False, "檔案存在" if package_json.is_file() else "檔案不存在", path=str(package_json)))
        checks.append(_check("web/package-lock.json", "ok" if package_lock.is_file() else "warning", False, "檔案存在" if package_lock.is_file() else "檔案不存在", path=str(package_lock)))

    return {"ok": not any(item["required"] and item["status"] == "failed" for item in checks), "checks": checks}


def _dev_command_check(name: str, version_flag: str) -> dict[str, Any]:
    resolved = shutil.which(name)
    if not resolved:
        return _check(name, "warning", False, "開發工具未安裝")
    try:
        result = subprocess.run([resolved, version_flag], capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(name, "warning", False, f"無法讀取版本：{exc}", resolved=resolved)
    version = (result.stdout or result.stderr or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    return _check(name, "ok" if result.returncode == 0 else "warning", False, version or "version command failed", resolved=resolved, returncode=result.returncode)


def _node_engine_check(package_json: Path) -> dict[str, Any]:
    """Validate the repository's declared Node range without adding semver dependencies."""
    if not package_json.is_file():
        return _check("node", "warning", False, "找不到 web/package.json，無法驗證 Node 相容性")
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
        required = str(package.get("engines", {}).get("node") or "").strip()
    except (OSError, UnicodeError, ValueError) as exc:
        return _check("node", "warning", False, f"無法讀取 Node engines：{exc}")
    if not required:
        return _check("node", "warning", False, "web/package.json 未宣告 engines.node")
    resolved = shutil.which("node")
    if not resolved:
        return _check("node", "warning", False, "開發工具未安裝", required_engine=required)
    try:
        result = subprocess.run([resolved, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("node", "warning", False, f"無法讀取版本：{exc}", required_engine=required, resolved=resolved)
    raw_version = (result.stdout or result.stderr or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    match = re.search(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw_version)
    if result.returncode != 0 or not match:
        return _check("node", "warning", False, raw_version or "version command failed", required_engine=required, resolved=resolved, returncode=result.returncode)
    actual = tuple(int(match.group(index) or 0) for index in (1, 2, 3))
    clauses = required.replace(",", " ").split()
    comparisons: list[tuple[str, tuple[int, int, int]]] = []
    for clause in clauses:
        part = re.fullmatch(r"(>=|<=|>|<|=)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", clause)
        if not part:
            return _check("node", "warning", False, f"無法解析 engines.node：{required}", required_engine=required, resolved=resolved, version=raw_version)
        comparisons.append((part.group(1) or "=", tuple(int(part.group(index) or 0) for index in (2, 3, 4))))

    def satisfies(operator: str, expected: tuple[int, int, int]) -> bool:
        if operator == ">=":
            return actual >= expected
        if operator == ">":
            return actual > expected
        if operator == "<=":
            return actual <= expected
        if operator == "<":
            return actual < expected
        return actual == expected

    compatible = all(satisfies(operator, expected) for operator, expected in comparisons)
    return _check(
        "node",
        "ok" if compatible else "warning",
        False,
        f"Node {raw_version} {'符合' if compatible else '不符合'} engines.node {required}",
        required_engine=required,
        resolved=resolved,
        version=raw_version,
    )


def run_doctor(config_file: str | Path = "config.yaml", *, json_output: bool = False, dev: bool = False) -> int:
    report = collect_doctor_report(config_file, dev=dev)
    if json_output:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        for item in report["checks"]:
            prefix = {"ok": "OK", "warning": "WARN", "failed": "FAIL"}[item["status"]]
            print(f"[{prefix}] {item['name']}: {item['message']}")
    return 0 if report["ok"] else 1


__all__ = ["collect_doctor_report", "run_doctor"]
