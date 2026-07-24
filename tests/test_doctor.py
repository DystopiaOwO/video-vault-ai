import json
from pathlib import Path
from types import SimpleNamespace

import video_vault.cli as cli
import video_vault.doctor as doctor


def _config(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    library = tmp_path / "影片資料庫 with spaces"
    (library / "05_index").mkdir(parents=True)
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"fake")
    ffprobe.write_bytes(b"fake")
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            (
                f'library_root: "{library}"',
                f'ffmpeg_path: "{ffmpeg}"',
                f'ffprobe_path: "{ffprobe}"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config, library, ffmpeg, ffprobe


def _pass_python(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", SimpleNamespace(major=3, minor=11, micro=0))


def test_doctor_report_is_read_only_and_checks_real_paths(tmp_path: Path, monkeypatch):
    config, library, _, _ = _config(tmp_path)
    _pass_python(monkeypatch)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="tool version 1\n", stderr=""),
    )

    before = sorted(path.name for path in library.iterdir())
    report = doctor.collect_doctor_report(config, repo_root=tmp_path)
    after = sorted(path.name for path in library.iterdir())

    assert report["ok"] is True
    assert before == after
    assert not (library / "05_index" / "video_vault.sqlite3").exists()
    assert not list(library.glob(".video_vault_doctor_*"))
    assert next(item for item in report["checks"] if item["name"] == "database path")["status"] == "ok"


def test_doctor_reports_missing_and_timeout_tools(tmp_path: Path, monkeypatch):
    config, _, _, ffprobe = _config(tmp_path)
    _pass_python(monkeypatch)
    config.write_text(
        config.read_text(encoding="utf-8").replace("ffmpeg.exe", "missing-ffmpeg.exe"),
        encoding="utf-8",
    )

    def run(*args, **kwargs):
        if str(args[0][0]).endswith(str(ffprobe)):
            raise doctor.subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 5))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    report = doctor.collect_doctor_report(config, repo_root=tmp_path)
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert checks["ffmpeg"]["status"] == "failed"
    assert checks["ffprobe"]["status"] == "failed"
    assert "timeout" in checks["ffprobe"]["message"]


def test_doctor_rejects_malformed_config(tmp_path: Path):
    config, _, _, _ = _config(tmp_path)
    config.write_text("library_root: ok\nthis line is not yaml\n", encoding="utf-8")

    report = doctor.collect_doctor_report(config, repo_root=tmp_path)

    config_check = next(item for item in report["checks"] if item["name"] == "config")
    assert config_check["status"] == "failed"
    assert "解析" in config_check["message"]


def test_doctor_json_is_machine_readable_without_secrets(tmp_path: Path, monkeypatch, capsys):
    config, _, _, _ = _config(tmp_path)
    _pass_python(monkeypatch)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="tool version\n", stderr=""),
    )
    monkeypatch.setenv("VIDEO_VAULT_DOCTOR_SECRET", "must-not-appear")

    result = doctor.run_doctor(config, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert "must-not-appear" not in json.dumps(payload, ensure_ascii=False)


def test_cli_doctor_does_not_initialize_database(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_doctor", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr(cli, "init_db", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("doctor must be read-only")))

    assert cli.main(["doctor", "--json", "--dev"]) == 0
    assert calls == [( ("config.yaml",), {"json_output": True, "dev": True})]


def test_doctor_validates_declared_node_engine(tmp_path: Path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"engines": {"node": ">=22 <23"}}), encoding="utf-8")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v22.4.1\n", stderr=""))
    check = doctor._node_engine_check(package)
    assert check["status"] == "ok"
    assert check["required"] is False


def test_doctor_warns_for_incompatible_node_engine(tmp_path: Path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"engines": {"node": ">=22 <23"}}), encoding="utf-8")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v20.19.0\n", stderr=""))
    check = doctor._node_engine_check(package)
    assert check["status"] == "warning"
    assert check["required"] is False
