import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

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
    assert calls == [( ("config.yaml",), {"json_output": "-", "mode": "default", "dev": True, "check_id": None})]


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
    monkeypatch.setattr(doctor, "_library_layout_checks", lambda *args: [doctor._check("storage.library_root", "storage", "pass", "fixture")])
    report = doctor.collect_doctor_report(config, mode="full", repo_root=Path(__file__).resolve().parents[1])
    assert report["summary"]["skipped"] > 0
    assert report["status"] == "warning"
    assert report["ok"] is True


def test_check_recursively_redacts_nested_evidence_and_url_credentials():
    check = doctor._check(
        "sensitive.probe",
        "provider",
        "blocked",
        "probe",
        evidence={
            "nested": {
                "api_key": "sk-secret-value",
                "path": r"C:\素材\旅遊片段.mp4",
                "items": ["Bearer secret-token", "https://user:password@example.test/models"],
            },
            "safe": {"api_key_present": True, "count": 2},
        },
    )
    encoded = json.dumps(check, ensure_ascii=False)
    assert "sk-secret-value" not in encoded
    assert "旅遊片段.mp4" not in encoded
    assert "password@example.test" not in encoded
    assert check["evidence"]["nested"]["api_key"] == "<redacted>"
    assert check["evidence"]["safe"]["api_key_present"] is True


def test_full_media_probe_verifies_unicode_h264_aac_long_path_and_cleanup():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe unavailable on this host")
    result = doctor._media_fixture_check({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"}, "full")
    assert result["status"] in {"pass", "warning"}
    assert result["evidence"]["unicode_path"] is True
    assert result["evidence"]["unicode_verified"] is True
    assert result["evidence"]["codec_h264"] is True
    assert result["evidence"]["codec_aac"] is True
    assert result["evidence"]["long_path_threshold"] == 260
    assert result["evidence"]["long_path_length"] > result["evidence"]["long_path_threshold"]
    assert result["evidence"]["max_component_length"] <= doctor._WINDOWS_LONG_PATH_COMPONENT_LIMIT
    if os.name == "nt":
        assert result["evidence"]["long_path_attempted"] is True
        assert result["evidence"]["long_path_status"] in {"pass", "blocked"}
        if result["evidence"]["long_path_status"] == "pass":
            assert result["evidence"]["long_path_verified"] is True
            assert result["evidence"]["long_path_mkdir"] is True
            assert result["evidence"]["long_path_copy"] is True
            assert result["evidence"]["long_path_read"] is True
        else:
            assert result["evidence"]["long_path_verified"] is False
    else:
        assert result["evidence"]["long_path_status"] == "skipped"
        assert result["evidence"]["long_path_verified"] == "not_verified"
    assert result["evidence"]["fixture_cleaned_up"] is True


def test_windows_long_path_fixture_uses_safe_nested_components(tmp_path: Path):
    long_output = doctor._windows_long_path_fixture_path(tmp_path)
    components = long_output.relative_to(tmp_path).parts

    assert len(str(long_output)) > doctor._WINDOWS_LONG_PATH_THRESHOLD
    assert len(components) >= 5
    assert all(len(component) <= doctor._WINDOWS_LONG_PATH_COMPONENT_LIMIT for component in components)
    assert max(len(component) for component in components) <= doctor._WINDOWS_LONG_PATH_COMPONENT_LIMIT


def test_hyperframes_full_probe_is_offline_no_install_and_missing_modules_blocks(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "tools" / "hyperframes"
    runtime.mkdir(parents=True)
    (runtime / "package.json").write_text(json.dumps({"dependencies": {"hyperframes": "0.7.76"}}), encoding="utf-8")
    (runtime / "package-lock.json").write_text(json.dumps({"packages": {"": {}, "node_modules/hyperframes": {"version": "0.7.76"}}}), encoding="utf-8")
    missing = doctor._hyperframes_check(tmp_path, "full")
    assert missing["status"] == "blocked"
    (runtime / "node_modules").mkdir()
    calls = []
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "node" if name.startswith("node") else "npx")
    monkeypatch.setattr(doctor.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    result = doctor._hyperframes_check(tmp_path, "full")
    assert result["status"] == "pass"
    assert result["evidence"]["offline"] is True
    assert result["evidence"]["no_install"] is True
    assert result["evidence"]["fixture_cleaned_up"] is True
    assert "--no-install" in calls[0][0]
    assert "render" in calls[0][0]


def test_media_probe_reports_encode_failure_and_cleans_fixture(monkeypatch):
    monkeypatch.setattr(doctor, "_resolve_executable", lambda value: str(value or "ffmpeg"))
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="encoder unavailable"))
    result = doctor._media_fixture_check({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"}, "full")
    assert result["status"] == "blocked"
    assert result["evidence"]["ffmpeg_error"] == "encode_failed"
    assert result["evidence"]["fixture_cleaned_up"] is True


def test_media_probe_cleanup_failure_is_blocked_not_pass(monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe unavailable on this host")
    monkeypatch.setattr(doctor.shutil, "rmtree", lambda *_args, **_kwargs: None)
    result = doctor._media_fixture_check({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"}, "full")
    assert result["status"] == "blocked"
    assert result["evidence"]["fixture_cleanup_attempted"] is True
    assert result["evidence"]["fixture_cleanup_verified"] is False


class _JsonResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_provider_matrix_model_missing_capability_missing_and_story_contract(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(doctor, "urlopen", lambda *args, **kwargs: _JsonResponse({"data": [{"id": "other-model"}]}))
    cfg = {"ai": {"provider": "local", "local": {"base_url": "http://127.0.0.1:1234/v1", "model": "missing-model"}}, "story": {"provider": "local_text", "base_url": "http://127.0.0.1:1234/v1", "model": "missing-story-model"}}
    model = doctor._provider_model_check(cfg, "full")
    caps = doctor._provider_capability_check(cfg, "full")
    story = doctor._story_provider_check(cfg, "full")
    assert model["status"] == "blocked"
    assert caps["status"] == "blocked"
    assert story["status"] == "blocked"
    assert story["evidence"]["context_capacity"] is None
    assert story["evidence"]["context_metadata_source"] == "unknown"


def test_local_multi_image_behavior_probe_verifies_missing_metadata_without_cloud(monkeypatch):
    calls = []

    def local_urlopen(request, **kwargs):
        calls.append(request)
        url = str(getattr(request, "full_url", request))
        if url.endswith("/models"):
            return _JsonResponse({"data": [{"id": "vision-model"}]})
        return _JsonResponse({"choices": [{"message": {"content": '{"summary":"ok"}'}}]})

    monkeypatch.setattr(doctor, "urlopen", local_urlopen)
    cfg = {"ai": {"provider": "local", "local": {"base_url": "http://127.0.0.1:1234/v1", "model": "vision-model"}}}
    result = doctor._provider_capability_check(cfg, "full")

    assert result["status"] == "pass"
    assert result["evidence"]["metadata_capability"]["status"] == "missing"
    assert result["evidence"]["behavior_capability"]["status"] == "pass"
    assert result["evidence"]["capability_source"] == "verified_by_behavior"
    assert result["evidence"]["behavior_capability"]["cloud_fallback"] is False
    assert len(calls) == 2
    assert str(calls[1].full_url).endswith("/chat/completions")


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("endpoint", "endpoint_unavailable"), ("model", "model_incapable"), ("malformed", "malformed_response")],
)
def test_local_multi_image_behavior_probe_failure_is_classified(monkeypatch, failure, expected):
    def local_urlopen(request, **kwargs):
        url = str(getattr(request, "full_url", request))
        if url.endswith("/models"):
            if failure == "endpoint":
                raise doctor.URLError("offline")
            return _JsonResponse({"data": [{"id": "vision-model"}]})
        if failure == "model":
            raise doctor.HTTPError(url, 400, "images unsupported", {}, None)
        return _JsonResponse({"not": "a chat completion"})

    monkeypatch.setattr(doctor, "urlopen", local_urlopen)
    cfg = {"ai": {"provider": "local", "local": {"base_url": "http://127.0.0.1:1234/v1", "model": "vision-model"}}}
    result = doctor._provider_capability_check(cfg, "full")

    assert result["status"] == "blocked"
    assert result["evidence"]["behavior_capability"]["status"] == expected


def test_story_doctor_reports_known_insufficient_and_unknown_context(monkeypatch):
    def local_urlopen(request, **kwargs):
        return _JsonResponse({"data": [{"id": "story-model"}]})

    monkeypatch.setattr(doctor, "urlopen", local_urlopen)
    base = {"provider": "local_text", "base_url": "http://127.0.0.1:1234/v1", "model": "story-model"}
    known = doctor._story_provider_check({"story": {**base, "context_length": 32768, "context_source": "test.model_metadata"}}, "full")
    insufficient = doctor._story_provider_check({"story": {**base, "context_length": 8192, "estimated_input_tokens": 26000}}, "full")
    unknown = doctor._story_provider_check({"story": base}, "full")

    assert known["status"] == "pass"
    assert known["evidence"]["model"] == "story-model"
    assert known["evidence"]["context_capacity_status"] == "known"
    assert known["evidence"]["context_capacity_tokens"] == 32768
    assert known["evidence"]["context_metadata_source"] == "test.model_metadata"
    assert insufficient["status"] == "blocked"
    assert insufficient["evidence"]["context_capacity_status"] == "insufficient"
    assert "fail closed" in insufficient["evidence"]["generation_preflight"]
    assert unknown["status"] == "warning"
    assert unknown["evidence"]["context_capacity_status"] == "unknown"

    monkeypatch.setattr(doctor, "urlopen", lambda *args, **kwargs: _JsonResponse({"data": [{"id": "story-model", "context_length": 65536}]}))
    endpoint_known = doctor._story_provider_check({"story": base}, "full")
    assert endpoint_known["status"] == "pass"
    assert endpoint_known["evidence"]["context_capacity_tokens"] == 65536
    assert endpoint_known["evidence"]["context_metadata_source"] == "local_endpoint.model_metadata"


def test_cloud_contract_never_calls_network_and_reports_key_presence(monkeypatch):
    monkeypatch.setenv("VID6_CLOUD_KEY", "secret-value")
    monkeypatch.setattr(doctor, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cloud doctor must not call network")))
    cfg = {"ai": {"cloud": {"model": "test-model", "api_key_env": "VID6_CLOUD_KEY"}}, "perception": {"cloud_review": {"enabled": True, "provider": "mock", "timeout_seconds": 30}}}
    result = doctor._cloud_config_check(cfg)
    assert result["status"] == "pass"
    assert result["evidence"]["api_key_present"] is True
    assert result["evidence"]["network_request"] is False


def test_asset_lock_parse_and_optional_asset_contracts(tmp_path: Path):
    (tmp_path / "web").mkdir()
    (tmp_path / "tools" / "hyperframes").mkdir(parents=True)
    (tmp_path / "web" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "web" / "package-lock.json").write_text("not-json", encoding="utf-8")
    (tmp_path / "tools" / "hyperframes" / "package-lock.json").write_text("{}", encoding="utf-8")
    checks = {item["check_id"]: item for item in doctor._asset_checks(tmp_path, {})}
    assert checks["configuration.web_lockfile_parse"]["status"] == "blocked"
    assert checks["asset.lut"]["status"] == "skipped"
    assert checks["asset.bgm"]["status"] == "skipped"
    assert checks["asset.retention"]["status"] == "skipped"


def test_full_sqlite_fixture_checks_schema_and_cleanup():
    result = doctor._sqlite_fixture_check(Path.cwd(), "full")
    assert result["status"] == "pass", result["evidence"]
    assert result["evidence"]["production_init_db"] is True
    assert result["evidence"]["missing_tables"] == []
    assert all(not values for values in result["evidence"]["missing_columns"].values())
    assert all(result["evidence"]["backfill"].values())
    assert all(result["evidence"]["indexes"].values())
    assert result["evidence"]["fixture_cleaned_up"] is True


def test_single_check_does_not_run_unrequested_expensive_probe(tmp_path: Path, monkeypatch):
    config, _, _, _ = _config(tmp_path)
    monkeypatch.setattr(doctor, "_media_fixture_check", lambda *args: (_ for _ in ()).throw(AssertionError("media probe must not run")))
    report = doctor.collect_doctor_report(config, mode="full", check_id="provider.active", repo_root=Path(__file__).resolve().parents[1])
    assert report["checks"][0]["check_id"] == "provider.active"


def test_free_disk_checks_use_temp_and_render_library_volumes_separately(monkeypatch):
    calls = []

    def fake_volume_probe(path: Path):
        # Keep the C:/D: regression platform-neutral: pathlib on Ubuntu does
        # not parse Windows drive paths, while the production probe is tested
        # against real paths by the isolated Doctor acceptance.
        path_text = str(path)
        calls.append(path_text)
        if path_text.upper().startswith("C:"):
            return {"free_bytes": 10_000, "total_bytes": 20_000, "path_exists": True, "volume": "C:"}, None
        return {"free_bytes": 100, "total_bytes": 20_000, "path_exists": True, "volume": "D:"}, None

    monkeypatch.setattr(doctor, "_volume_probe", fake_volume_probe)
    monkeypatch.setattr(doctor.tempfile, "gettempdir", lambda: "C:\\temp")
    checks = {item["check_id"]: item for item in doctor._free_disk_checks({"library_root": "D:\\VideoLibrary", "render": {"minimum_free_disk_bytes": 150}})}

    assert checks["runtime.free_disk.temp"]["status"] == "pass"
    assert checks["runtime.free_disk.render"]["status"] == "blocked"
    assert checks["runtime.free_disk.render"]["evidence"]["minimum_applied_to"] == "render/library volume"
    assert any(path.upper().startswith("C:") for path in calls)
    assert any(path.upper().startswith("D:") for path in calls)


def test_legacy_free_disk_check_id_returns_both_volume_checks(tmp_path: Path):
    report = doctor.collect_doctor_report_from_config({"library_root": str(tmp_path)}, check_id="runtime.free_disk", repo_root=Path(__file__).resolve().parents[1])
    assert {item["check_id"] for item in report["checks"]} == {"runtime.free_disk.temp", "runtime.free_disk.render"}


def test_optional_disabled_assets_skip_and_malformed_enabled_is_blocked(tmp_path: Path):
    disabled = {"library_root": str(tmp_path), "bgm": {"enabled": "false"}, "retention": {"enabled": "off", "cache_max_age_days": "not-a-number"}}
    checks = {item["check_id"]: item for item in doctor._asset_checks(Path(__file__).resolve().parents[1], disabled)}
    assert checks["asset.bgm"]["status"] == "skipped"
    assert checks["asset.retention"]["status"] == "skipped"

    malformed_enabled = {"library_root": str(tmp_path), "bgm": {"enabled": 2}, "retention": {"enabled": "maybe"}}
    malformed_checks = {item["check_id"]: item for item in doctor._asset_checks(Path(__file__).resolve().parents[1], malformed_enabled)}
    assert malformed_checks["asset.bgm"]["status"] == "blocked"
    assert malformed_checks["asset.bgm"]["evidence"]["error_code"] == "invalid_boolean_number"
    assert malformed_checks["asset.retention"]["status"] == "blocked"

    malformed_policy = {"library_root": str(tmp_path), "retention": {"enabled": True, "cache_max_age_days": "not-a-number"}}
    policy_checks = {item["check_id"]: item for item in doctor._asset_checks(Path(__file__).resolve().parents[1], malformed_policy)}
    assert policy_checks["asset.retention"]["status"] == "blocked"
    assert policy_checks["asset.retention"]["evidence"]["numeric_errors"]["cache_max_age_days"] == "invalid_number"


def test_malformed_optional_config_is_structured_blocked_for_cli_and_api(tmp_path: Path):
    config, _, _, _ = _config(tmp_path)
    config.write_text(
        "\n".join(
            (
                f'library_root: "{tmp_path}"',
                "bgm:",
                "  enabled: 2",
                "retention:",
                "  enabled: maybe",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    report = doctor.collect_doctor_report(config, repo_root=Path(__file__).resolve().parents[1])
    checks = {item["check_id"]: item for item in report["checks"]}
    assert checks["asset.bgm"]["status"] == "blocked"
    assert checks["asset.retention"]["status"] == "blocked"
    api_report = doctor.collect_doctor_report_from_config({"library_root": str(tmp_path), "bgm": {"enabled": 2}, "retention": {"enabled": "maybe"}}, repo_root=Path(__file__).resolve().parents[1])
    api_checks = {item["check_id"]: item for item in api_report["checks"]}
    assert api_checks["asset.bgm"]["status"] == "blocked"
    assert api_checks["asset.retention"]["status"] == "blocked"
