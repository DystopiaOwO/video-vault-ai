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

    assert before == after
    assert not (library / "05_index" / "video_vault.sqlite3").exists()
    assert not list(library.glob(".video_vault_doctor_*"))
    assert report["schema_version"] == "doctor-v1"
    assert all({"check_id", "category", "status", "summary", "evidence", "remediation", "duration_ms", "sensitive"} <= set(item) for item in report["checks"])
    assert next(item for item in report["checks"] if item["check_id"] == "storage.database_parent")["status"] == "pass"


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
    assert checks["runtime.media.ffmpeg"]["status"] == "blocked"
    assert checks["runtime.media.ffprobe"]["status"] == "blocked"
    assert checks["runtime.media.ffprobe"]["evidence"]["probe"] == "timeout"


def test_doctor_rejects_malformed_config(tmp_path: Path):
    config, _, _, _ = _config(tmp_path)
    config.write_text("library_root: ok\nthis line is not yaml\n", encoding="utf-8")

    report = doctor.collect_doctor_report(config, repo_root=tmp_path)

    config_check = next(item for item in report["checks"] if item["check_id"] == "configuration.parse")
    assert config_check["status"] == "blocked"
    assert "解析" in config_check["summary"]


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

    assert result in {0, 1}
    assert payload["schema_version"] == "doctor-v1"
    assert "must-not-appear" not in json.dumps(payload, ensure_ascii=False)


def test_cli_doctor_does_not_initialize_database(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_doctor", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr(cli, "init_db", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("doctor must be read-only")))

    assert cli.main(["doctor", "--json", "--dev"]) == 0
    assert calls == [( ("config.yaml",), {"json_output": "-", "mode": "default", "dev": True})]


def test_doctor_validates_declared_node_engine(tmp_path: Path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"engines": {"node": ">=22 <23"}}), encoding="utf-8")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v22.4.1\n", stderr=""))
    check = doctor._node_engine_check(package)
    assert check["status"] == "pass"
    assert check["required"] is False


def test_doctor_warns_for_incompatible_node_engine(tmp_path: Path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"engines": {"node": ">=22 <23"}}), encoding="utf-8")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v20.19.0\n", stderr=""))
    check = doctor._node_engine_check(package)
    assert check["status"] == "warning"
    assert check["required"] is False


def test_quick_mode_never_runs_subprocess_and_marks_behavior_skipped(tmp_path: Path, monkeypatch):
    config, _, _, _ = _config(tmp_path)
    _pass_python(monkeypatch)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("quick doctor must not run subprocess")))
    report = doctor.collect_doctor_report(config, mode="quick", repo_root=Path(__file__).resolve().parents[1])
    media = next(item for item in report["checks"] if item["check_id"] == "media.behavior")
    assert media["status"] == "skipped"
    assert media["evidence"]["probe"] == "skipped"


def test_report_redacts_sensitive_values_and_paths(tmp_path: Path, monkeypatch):
    config, library, _, _ = _config(tmp_path)
    _pass_python(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    payload = json.dumps(doctor.collect_doctor_report(config, repo_root=Path(__file__).resolve().parents[1]), ensure_ascii=False)
    assert "super-secret-key" not in payload
    assert str(library) not in payload


def test_doctor_summary_redaction_does_not_destroy_normal_slashes():
    check = doctor._check("probe", "runtime", "pass", "FFmpeg/FFprobe fixture probe passed")
    assert check["summary"] == "FFmpeg/FFprobe fixture probe passed"


def test_report_surfaces_skipped_checks_as_warning_instead_of_total_green(tmp_path: Path, monkeypatch):
    config, _, _, _ = _config(tmp_path)
    _pass_python(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "tool.exe")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v22.4.1\n", stderr=""))
    monkeypatch.setattr(doctor, "_hyperframes_check", lambda *args: doctor._check("frontend.hyperframes", "frontend", "pass", "fixture"))
    monkeypatch.setattr(doctor, "_media_fixture_check", lambda *args: doctor._check("media.behavior", "runtime.media", "pass", "fixture"))
    monkeypatch.setattr(doctor, "_sqlite_fixture_check", lambda *args: doctor._check("storage.sqlite", "storage", "pass", "fixture"))
    monkeypatch.setattr(doctor, "_loopback_fixture_check", lambda *args: doctor._check("web.loopback", "frontend", "pass", "fixture"))
    report = doctor.collect_doctor_report(config, mode="full", repo_root=Path(__file__).resolve().parents[1])
    assert report["summary"]["skipped"] > 0
    assert report["status"] == "warning"
    assert report["ok"] is True
