from pathlib import Path
import hashlib
import json

import pytest

import video_vault.delivery_qa as delivery_qa
from video_vault.database import create_project_row, init_db, project_revision
from video_vault.delivery_qa import DeliveryQAError, QAReviewVersionConflict, delivery_qa_for_api, review_delivery_qa, run_delivery_qa
from video_vault.bgm_pipeline import bgm_fingerprint
from video_vault.media_probe import MediaProbe
from video_vault.project import project_dir
from video_vault.render_job_store import RenderJobStore
from video_vault.segment_provenance import segment_approval_provenance


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, monkeypatch, *, analysis=None, loudness=True):
    library = tmp_path / "library"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"}
    db = library / "video_vault.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "QA", content_type="travel_diary")
    folder = project_dir(cfg, project_id)
    source = tmp_path / "private-source-name.mp4"
    source.write_bytes(b"source unchanged")
    output = folder / "renders" / "formal-private-name.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"formal output")
    manifest_hash = "a" * 64
    manifest = {
        "project_id": project_id,
        "manifest_hash": manifest_hash,
        "profile": {"width": 1920, "height": 1080, "fps": 30.0, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        "segments": [{"segment_id": "segment-uuid-1", "source_file": str(source), "source_in_seconds": 0, "source_out_seconds": 4, "timeline_duration_seconds": 4, "audio_role": "keep_original", "group_id": "chapter-1"}],
        "visual_timeline": {"resolved_items": [{"stable_id": "chapter-card-1", "type": "chapter_card", "group_id": "chapter-1", "start_seconds": 0, "duration_seconds": 1, "style_id": "location-lower-left"}]},
        "settings": {},
        "bgm": [],
    }
    snapshot = {
        "snapshot_id": "approval-1",
        "snapshot_hash": "b" * 64,
        "approved_project_revision": project_revision(db, project_id),
        "manifest_hash": manifest_hash,
        "manifest": manifest,
        "assets": [{"canonical_path": str(source), "sha256": _sha(source), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns, "kind": "source_media"}],
    }
    (folder / "review_status.json").write_text(json.dumps({
        "status": "approved",
        "approved_by_user": True,
        "approval_snapshot_id": snapshot["snapshot_id"],
        "approval_snapshot_hash": snapshot["snapshot_hash"],
    }), encoding="utf-8")
    render_report = {
        "manifest_hash": manifest_hash,
        "output_sha256": _sha(output),
        "loudness": {"final": {"measured_I": -14.0, "measured_TP": -1.2}} if loudness else {},
        "segments": [{
            "segment_id": "segment-uuid-1",
            "cache_key": "artifact-cache-key-v5",
            "approval_provenance_version": segment_approval_provenance(manifest, manifest["segments"][0], source_fingerprint=snapshot["assets"][0])["version"],
            "approval_provenance_hash": segment_approval_provenance(manifest, manifest["segments"][0], source_fingerprint=snapshot["assets"][0])["hash"],
        }],
        "bgm": {"used": False, "fingerprint": {}},
        "qc": {"passed": True},
        "measurements": {"decode": {"ok": True}, "timestamp_monotonic": True},
    }
    output.with_name(output.name + ".render.json").write_text(json.dumps(render_report), encoding="utf-8")
    probe = MediaProbe(output, 4.0, True, True, 1920, 1080, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2, frame_count=120, video_end_seconds=4.0, audio_end_seconds=4.0)
    monkeypatch.setattr(delivery_qa, "probe_media", lambda *args: probe)
    monkeypatch.setattr(delivery_qa, "_probe_stream_details", lambda *args: {"ok": True, "format_name": "mov,mp4", "video_stream_count": 1, "audio_stream_count": 1, "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9", "rotation_degrees": 0, "audio_channel_layout": "stereo", "faststart": True})
    monkeypatch.setattr(delivery_qa, "_analyze_output", lambda *args, **kwargs: analysis or {"ok": True, "events": {"black": [], "freeze": [], "silence": [], "flash": []}, "brightness_sample_count": 3, "brightness_range": {"min": 16, "max": 235}, "flash_detection_reason": "brightness_samples_sufficient_for_reversal", "crop_observations": [[1920, 1080, 0, 0]], "max_volume_db": -1.3, "invalid_sample_count": 0})

    def evidence(*args, **kwargs):
        del kwargs
        Path(args[2]).write_bytes(b"jpeg evidence")
        return True

    monkeypatch.setattr(delivery_qa, "_generate_contact_sheet", evidence)
    monkeypatch.setattr(delivery_qa, "_generate_event_strip", evidence)
    monkeypatch.setattr(delivery_qa, "_generate_dense_contact_sheet", evidence)
    store = RenderJobStore(cfg)
    job = store.create(project_id=project_id, manifest_hash=manifest_hash, approved_manifest_hash=manifest_hash, approval_snapshot_id=snapshot["snapshot_id"], approval_snapshot_hash=snapshot["snapshot_hash"], approval_snapshot=snapshot)
    store.update(job["job_id"], status="succeeded", stage="done", percent=100, output_path=str(output), finished_at="now")
    return cfg, db, project_id, folder, source, output, snapshot, job["job_id"]


def test_run_persists_safe_versioned_contract_and_human_gate(tmp_path: Path, monkeypatch):
    cfg, db, project_id, folder, source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch)
    source_hash = _sha(source)
    output_hash = _sha(output)
    revision = project_revision(db, project_id)

    report = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)

    assert report["schema_version"] == 1
    assert report["contract"]["version"] == "delivery-qa-v1"
    assert report["lifecycle_status"] == "qa_needs_review"
    assert report["deliverable_ready"] is False
    assert report["summary"] == {"pass": 8, "warning": 0, "blocked": 0, "skipped": 0}
    assert all(check["evidence_artifact_ids"] for check in report["checks"])
    assert all(check["status"] == "pass" for check in report["checks"])
    serialized = (folder / "qa" / report["qa_run_uuid"] / "report.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert source.name not in serialized
    assert output.name not in serialized
    assert '"sensitive_data_redacted": true' in serialized

    reviewed = review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="confirm", expected_version=1)
    assert reviewed["deliverable_ready"] is True
    assert reviewed["lifecycle_status"] == "deliverable_ready"
    assert reviewed["human_review"]["review_version"] == 2
    assert project_revision(db, project_id) == revision
    assert _sha(source) == source_hash
    assert _sha(output) == output_hash


def test_warning_requires_per_check_reason_and_stale_write_conflicts(tmp_path: Path, monkeypatch):
    cfg, db, project_id, _folder, _source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch, loudness=False)
    report = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    assert next(check for check in report["checks"] if check["check_id"] == "audio")["status"] == "warning"

    with pytest.raises(DeliveryQAError) as missing:
        review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="confirm", expected_version=1)
    assert missing.value.code == "warning_reason_required"

    reviewed = review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="confirm", expected_version=1, warning_acceptances={"audio": "刻意保留低音量氣氛"})
    assert reviewed["deliverable_ready"] is True
    with pytest.raises(QAReviewVersionConflict):
        review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="reject", expected_version=1, reason="stale")


def test_blocked_or_skipped_cannot_be_overridden(tmp_path: Path, monkeypatch):
    analysis = {"ok": False, "error_code": r"C:\secret\素材.mp4 token=abc", "events": {}, "crop_observations": []}
    cfg, db, project_id, folder, _source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch, analysis=analysis)
    report = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    assert report["summary"]["blocked"] >= 1
    serialized = (folder / "qa" / report["qa_run_uuid"] / "report.json").read_text(encoding="utf-8")
    assert "secret" not in serialized
    assert "token=abc" not in serialized
    with pytest.raises(DeliveryQAError) as blocked:
        review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="confirm", expected_version=1)
    assert blocked.value.code == "qa_not_deliverable"


def test_new_run_makes_previous_run_historical_without_mutating_snapshot(tmp_path: Path, monkeypatch):
    cfg, db, project_id, _folder, _source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch)
    first = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    second = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    assert first["qa_run_uuid"] != second["qa_run_uuid"]
    assert delivery_qa_for_api(cfg, project_id)["qa_run_uuid"] == second["qa_run_uuid"]
    with pytest.raises(DeliveryQAError) as stale:
        review_delivery_qa(cfg, project_id, first["qa_run_uuid"], action="confirm", expected_version=1)
    assert stale.value.code == "stale_delivery_qa_run"


def test_profile_thresholds_are_content_aware_and_record_sources(tmp_path: Path):
    cfg = {"library_root": str(tmp_path), "delivery_qa": {"profiles": {"travel_diary": {"silence_warning_seconds": 12}}, "threshold_overrides": {"black_block_seconds": 2}}}
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "travel", content_type="travel_diary")
    profile = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert profile["profile_id"] == "travel_diary"
    assert profile["resolved_thresholds"]["silence_warning_seconds"] == 12
    assert profile["resolved_thresholds"]["black_block_seconds"] == 2
    assert profile["threshold_sources"]["silence_warning_seconds"] == "config.profile"
    assert profile["threshold_sources"]["black_block_seconds"] == "config.global"


def test_container_contract_fails_closed_on_duration_faststart_and_channel_layout():
    probe = MediaProbe(Path("formal.mp4"), 4.0, True, True, 1920, 1080, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2, frame_count=120, video_end_seconds=4.0, audio_end_seconds=4.0)
    manifest = {
        "expected_duration_seconds": 10.0,
        "profile": {"width": 1920, "height": 1080, "fps": 30.0, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
    }
    stream = {"ok": True, "audio_channel_layout": "mono", "faststart": False}
    report = {"manifest_hash": "a" * 64, "output_sha256": "b" * 64, "qc": {"passed": True}, "measurements": {"decode": {"ok": True}, "timestamp_monotonic": True}}
    result = delivery_qa._container_check(probe, "", stream, manifest, report, "a" * 64, {"sha256": "b" * 64}, delivery_qa._GENERAL_THRESHOLDS)
    assert result["status"] == "blocked"
    assert "duration" in result["summary"]
    assert "channel layout" in result["summary"]
    assert "fast-start" in result["summary"]


def test_black_flash_distinguishes_edge_fade_from_interior_black_and_flash_clusters():
    thresholds = delivery_qa._PROFILE_THRESHOLDS["travel_diary"]
    edge = {"ok": True, "brightness_sample_count": 3, "events": {"black": [{"start_seconds": 0, "end_seconds": 1.2, "duration_seconds": 1.2}], "flash": []}}
    interior = {"ok": True, "brightness_sample_count": 3, "events": {"black": [{"start_seconds": 3, "end_seconds": 4.2, "duration_seconds": 1.2}], "flash": []}}
    flashing = {"ok": True, "brightness_sample_count": 3, "events": {"black": [], "flash": [1, 1.1, 1.2, 1.3, 1.4]}}
    assert delivery_qa._black_flash_check(edge, thresholds, 10)["status"] == "warning"
    assert delivery_qa._black_flash_check(interior, thresholds, 10)["status"] == "blocked"
    assert delivery_qa._black_flash_check(flashing, thresholds, 10)["status"] == "warning"


def test_flash_parser_uses_brightness_reversal_not_scene_cuts_and_keeps_fade_separate():
    thresholds = delivery_qa._PROFILE_THRESHOLDS["travel_diary"]
    log = "\n".join([
        "[Parsed_metadata_1] frame:0 pts:0 pts_time:0",
        "[Parsed_metadata_1] lavfi.signalstats.YAVG=16",
        "[Parsed_metadata_1] frame:1 pts:1 pts_time:0.033333",
        "[Parsed_metadata_1] lavfi.signalstats.YAVG=235",
        "[Parsed_metadata_1] frame:2 pts:2 pts_time:0.066667",
        "[Parsed_metadata_1] lavfi.signalstats.YAVG=16",
    ])
    parsed = delivery_qa._parse_analysis_log(log)
    flashes = delivery_qa._detect_flash_events(parsed["brightness_samples"], thresholds)
    assert [event["timestamp_seconds"] for event in flashes] == [pytest.approx(0.033333), pytest.approx(0.066667)]
    assert delivery_qa._black_flash_check({"ok": True, "brightness_sample_count": 3, "events": {"flash": flashes, "scene_change": [0.033333]}}, thresholds, 1)["status"] == "warning"

    hard_cut = delivery_qa._detect_flash_events([
        {"timestamp_seconds": 0, "yavg": 16},
        {"timestamp_seconds": 0.033, "yavg": 235},
        {"timestamp_seconds": 0.066, "yavg": 235},
    ], thresholds)
    assert hard_cut == []
    fade = delivery_qa._black_flash_check({"ok": True, "brightness_sample_count": 3, "events": {"black": [{"start_seconds": 0, "end_seconds": 1.2, "duration_seconds": 1.2}], "flash": []}}, thresholds, 10)
    assert fade["status"] == "warning"
    assert fade["metrics"]["flash_event_count"] == 0


def test_black_flash_fails_closed_when_ffmpeg_succeeds_without_reversal_evidence():
    thresholds = delivery_qa._PROFILE_THRESHOLDS["travel_diary"]
    no_samples = delivery_qa._black_flash_check({"ok": True, "brightness_sample_count": 0, "events": {"black": [], "flash": []}}, thresholds, 10)
    assert no_samples["status"] == "blocked"
    assert no_samples["metrics"]["brightness_sample_count"] == 0
    assert no_samples["metrics"]["flash_detection"]["reason"] == "no_brightness_samples"

    insufficient = delivery_qa._black_flash_check({"ok": True, "brightness_sample_count": 2, "events": {"black": [], "flash": []}}, thresholds, 10)
    assert insufficient["status"] == "blocked"
    assert insufficient["metrics"]["brightness_sample_count"] == 2
    assert insufficient["metrics"]["flash_detection"]["reason"] == "insufficient_brightness_samples"

    sufficient = delivery_qa._black_flash_check({"ok": True, "brightness_sample_count": 3, "events": {"black": [], "flash": []}}, thresholds, 10)
    assert sufficient["status"] == "pass"
    assert sufficient["metrics"]["flash_detection"]["status"] == "ready"


def test_travel_freeze_and_silence_only_block_when_overlap_exceeds_profile_threshold():
    thresholds = delivery_qa._PROFILE_THRESHOLDS["travel_diary"]
    atmosphere = {"ok": True, "events": {"freeze": [{"start_seconds": 0, "end_seconds": 5, "duration_seconds": 5}], "silence": [{"start_seconds": 0, "end_seconds": 5, "duration_seconds": 5}]}}
    stalled = {"ok": True, "events": {"freeze": [{"start_seconds": 0, "end_seconds": 7, "duration_seconds": 7}], "silence": [{"start_seconds": 0, "end_seconds": 7, "duration_seconds": 7}]}}
    assert delivery_qa._freeze_silence_check(atmosphere, thresholds, "travel_diary")["status"] == "warning"
    assert delivery_qa._freeze_silence_check(stalled, thresholds, "travel_diary")["status"] == "blocked"


def test_audio_check_audits_clipping_invalid_samples_bgm_coverage_and_effective_roles(tmp_path: Path):
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"segment")
    bgm_source = tmp_path / "bgm.mp3"
    bgm_source.write_bytes(b"bgm")
    probe = MediaProbe(Path("formal.mp4"), 10.0, True, True, 1920, 1080, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2, frame_count=300, video_end_seconds=10.0, audio_end_seconds=10.0)
    manifest = {
        "expected_duration_seconds": 10,
        "segments": [{"segment_id": "segment-1", "source_file": str(source), "audio_role": "keep_original", "audio": {"role": "mute", "fade_in_seconds": 0.1, "fade_out_seconds": 0.1}}],
        "bgm": [{"track_id": 1, "source_path": str(bgm_source), "loop": False, "start_seconds": 0, "duration_seconds": 2, "fade_in_seconds": 0, "fade_out_seconds": 0}],
    }
    approval = {"assets": [{"canonical_path": str(source), "kind": "source_media", "sha256": _sha(source), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns}]}
    provenance = segment_approval_provenance(manifest, manifest["segments"][0], source_fingerprint=approval["assets"][0])
    render_report = {
        "loudness": {"final": {"measured_I": -14, "measured_TP": -1.2}},
        "segments": [{"segment_id": "segment-1", "cache_key": "resolved-v5-cache", "approval_provenance_version": provenance["version"], "approval_provenance_hash": provenance["hash"]}],
        "bgm": {"used": True, "fingerprint": bgm_fingerprint(manifest["bgm"][0])},
    }
    warning = delivery_qa._audio_check({"ok": True, "events": {"silence": []}, "max_volume_db": -0.01, "invalid_sample_count": 0}, probe, render_report, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)
    assert warning["status"] == "warning"
    assert warning["metrics"]["audio_roles"] == {"mute": 1}
    assert "提前終止" in warning["summary"]
    blocked = delivery_qa._audio_check({"ok": True, "events": {"silence": []}, "max_volume_db": 0, "invalid_sample_count": 1}, probe, render_report, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)
    assert blocked["status"] == "blocked"


def test_audio_provenance_ignores_cache_key_but_blocks_semantic_mutations(tmp_path: Path):
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"segment")
    bgm_source = tmp_path / "bgm.mp3"
    bgm_source.write_bytes(b"bgm")
    manifest = {
        "expected_duration_seconds": 10,
        "segments": [{
            "segment_id": "segment-1",
            "source_file": str(source),
            "source_in_seconds": 0,
            "source_out_seconds": 10,
            "timeline_duration_seconds": 10,
            "audio_role": "mute",
            "audio": {"role": "mute", "fade_in_seconds": 0.1, "fade_out_seconds": 0.2},
        }],
        "bgm": [{
            "track_id": 1,
            "source_path": str(bgm_source),
            "loop": True,
            "start_seconds": 0,
            "duration_seconds": 2,
            "fade_in_seconds": 0.2,
            "fade_out_seconds": 0.2,
        }],
    }
    probe = MediaProbe(Path("formal.mp4"), 10.0, True, True, 1920, 1080, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2, frame_count=300, video_end_seconds=10.0, audio_end_seconds=10.0)
    analysis = {"ok": True, "events": {"silence": []}, "max_volume_db": -1.3, "invalid_sample_count": 0}
    approval = {"assets": [{"canonical_path": str(source), "kind": "source_media", "sha256": _sha(source), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns}]}
    provenance = segment_approval_provenance(manifest, manifest["segments"][0], source_fingerprint=approval["assets"][0])
    correct = {
        "loudness": {"final": {"measured_I": -14, "measured_TP": -1.2}},
        "segments": [{"segment_id": "segment-1", "cache_key": "resolved-nvenc-v5", "approval_provenance_version": provenance["version"], "approval_provenance_hash": provenance["hash"]}],
        "bgm": {"used": True, "fingerprint": bgm_fingerprint(manifest["bgm"][0])},
    }
    assert delivery_qa._audio_check(analysis, probe, correct, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)["status"] == "pass"

    keep_manifest = {**manifest, "segments": [{**manifest["segments"][0], "audio": {"role": "keep", "fade_in_seconds": 0.1, "fade_out_seconds": 0.2}}]}
    wrong_segment_provenance = segment_approval_provenance(keep_manifest, keep_manifest["segments"][0], source_fingerprint=approval["assets"][0])
    wrong_segment = {**correct, "segments": [{**correct["segments"][0], "approval_provenance_hash": wrong_segment_provenance["hash"]}]}
    assert delivery_qa._audio_check(analysis, probe, wrong_segment, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)["status"] == "blocked"

    fade_manifest = {**manifest, "segments": [{**manifest["segments"][0], "audio": {"role": "lower", "fade_in_seconds": 0.1, "fade_out_seconds": 0.8}}]}
    wrong_fade_provenance = segment_approval_provenance(fade_manifest, fade_manifest["segments"][0], source_fingerprint=approval["assets"][0])
    wrong_fade = {**correct, "segments": [{**correct["segments"][0], "approval_provenance_hash": wrong_fade_provenance["hash"]}]}
    assert delivery_qa._audio_check(analysis, probe, wrong_fade, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)["status"] == "blocked"

    wrong_bgm = {**correct, "bgm": {"used": False, "fingerprint": {}}}
    assert delivery_qa._audio_check(analysis, probe, wrong_bgm, manifest, delivery_qa._GENERAL_THRESHOLDS, approval_snapshot=approval)["status"] == "blocked"

    second_segment = {**manifest["segments"][0], "segment_id": "segment-2", "source_in_seconds": 10, "source_out_seconds": 20}
    two_segment_manifest = {**manifest, "segments": [manifest["segments"][0], second_segment]}
    first = segment_approval_provenance(two_segment_manifest, two_segment_manifest["segments"][0], source_fingerprint=approval["assets"][0])
    second = segment_approval_provenance(two_segment_manifest, two_segment_manifest["segments"][1], source_fingerprint=approval["assets"][0])
    exact_report = {**correct, "segments": [
        {"segment_id": "segment-1", "cache_key": "cache-a", "approval_provenance_version": first["version"], "approval_provenance_hash": first["hash"]},
        {"segment_id": "segment-2", "cache_key": "cache-b", "approval_provenance_version": second["version"], "approval_provenance_hash": second["hash"]},
    ]}
    exact = delivery_qa._audio_render_provenance(two_segment_manifest, exact_report, approval_snapshot=approval)
    assert exact["failures"] == []
    assert exact["metrics"]["approved_segment_count"] == 2
    assert exact["metrics"]["reported_segment_count"] == 2
    assert exact["metrics"]["cache_audit"]["identity_used_for_gate"] == "approval_semantic_provenance"
    assert exact["metrics"]["cache_audit"]["cache_key_used_for_gate"] is False

    for reported_segments in (
        [exact_report["segments"][0], exact_report["segments"][1], "corrupt trailing entry"],
        [exact_report["segments"][0], "corrupt middle entry", exact_report["segments"][1]],
        [exact_report["segments"][0], {}],
        [exact_report["segments"][0], exact_report["segments"][1], exact_report["segments"][0]],
    ):
        malformed = delivery_qa._audio_render_provenance(two_segment_manifest, {**correct, "segments": reported_segments}, approval_snapshot=approval)
        assert malformed["failures"], malformed


def test_qa_settings_absent_valid_and_malformed_are_distinct_and_fail_closed(tmp_path: Path, monkeypatch):
    cfg = {"library_root": str(tmp_path)}
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "qa settings", content_type="travel_diary")
    folder = project_dir(cfg, project_id)

    absent = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert absent["threshold_validation"]["project_settings"]["status"] == "absent"
    assert delivery_qa._threshold_config_check(absent)["status"] == "pass"

    settings = folder / "qa_settings.json"
    settings.write_text(json.dumps({"threshold_overrides": {"flash_brightness_delta": 80}}), encoding="utf-8")
    valid = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert valid["threshold_validation"]["project_settings"]["status"] == "valid"
    assert valid["threshold_validation"]["project_settings"]["source"] == "project.qa_settings"
    assert valid["threshold_validation"]["project_settings"]["resolved_values"] == {"flash_brightness_delta": 80.0}
    assert delivery_qa._threshold_config_check(valid)["status"] == "pass"

    settings.write_text("{ malformed", encoding="utf-8")
    malformed = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert malformed["threshold_validation"]["project_settings"]["status"] == "blocked"
    assert delivery_qa._threshold_config_check(malformed)["status"] == "blocked"

    settings.write_text("[]", encoding="utf-8")
    non_object = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert non_object["threshold_validation"]["project_settings"]["status"] == "blocked"
    assert delivery_qa._threshold_config_check(non_object)["status"] == "blocked"

    (tmp_path / "run").mkdir()
    run_cfg, run_db, run_project, run_folder, _source, run_output, snapshot, job_id = _fixture(tmp_path / "run", monkeypatch)
    (run_folder / "qa_settings.json").write_text("{ malformed", encoding="utf-8")
    blocked_report = run_delivery_qa(run_cfg, run_db, run_project, render_job_uuid=job_id, output_path=run_output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    assert next(item for item in blocked_report["checks"] if item["check_id"] == "threshold_config")["status"] == "blocked"
    assert blocked_report["lifecycle_status"] == "qa_blocked"


def test_continuity_audits_duplicate_group_chapters_and_maps_events_to_stable_uuid():
    manifest = {
        "segments": [
            {"segment_id": "stable-1", "timeline_duration_seconds": 2, "group_id": "chapter-1", "duplicate_group": "dup"},
            {"segment_id": "stable-2", "timeline_duration_seconds": 2, "group_id": "chapter-2", "duplicate_group": "dup"},
        ],
        "visual_timeline": {"resolved_items": [{"stable_id": "chapter-card-1", "type": "chapter_card", "start_seconds": 0, "duration_seconds": 1}]},
    }
    result = delivery_qa._continuity_check(manifest, {}, delivery_qa._GENERAL_THRESHOLDS)
    assert result["status"] == "warning"
    assert result["metrics"]["duplicate_group_repeat_count"] == 1
    assert result["metrics"]["chapter_boundary_artifact_count"] == 1
    checks = [{"metrics": {"events": [{"start_seconds": 2.5}]}}]
    delivery_qa._annotate_check_events(checks, manifest)
    assert checks[0]["metrics"]["events"][0]["stable_segment_uuid"] == "stable-2"


def test_missing_human_evidence_blocks_delivery_instead_of_reporting_success(tmp_path: Path, monkeypatch):
    cfg, db, project_id, _folder, _source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(delivery_qa, "_generate_contact_sheet", lambda *args, **kwargs: False)
    monkeypatch.setattr(delivery_qa, "_generate_dense_contact_sheet", lambda *args, **kwargs: False)
    report = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    evidence = next(check for check in report["checks"] if check["check_id"] == "evidence_bundle")
    assert evidence["status"] == "blocked"
    assert report["lifecycle_status"] == "qa_blocked"


def test_output_metadata_change_invalidates_ready_state_without_mutating_approval(tmp_path: Path, monkeypatch):
    cfg, db, project_id, _folder, _source, output, snapshot, job_id = _fixture(tmp_path, monkeypatch)
    report = run_delivery_qa(cfg, db, project_id, render_job_uuid=job_id, output_path=output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    reviewed = review_delivery_qa(cfg, project_id, report["qa_run_uuid"], action="confirm", expected_version=1)
    assert reviewed["deliverable_ready"] is True
    output.write_bytes(output.read_bytes() + b"changed")
    historical = delivery_qa_for_api(cfg, project_id)
    assert historical["currentity"] == "historical"
    assert historical["deliverable_ready"] is False


def test_delivery_qa_schema_declares_fail_closed_status_and_redaction_invariant():
    schema = json.loads(Path(__file__).parents[1].joinpath("schemas", "delivery_qa_report.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["sensitive_data_redacted"]["const"] is True
    assert schema["$defs"]["check"]["properties"]["status"]["enum"] == ["pass", "warning", "blocked", "skipped"]


def test_render_report_required_provenance_is_fail_closed():
    probe = MediaProbe(Path("formal.mp4"), 4.0, True, True, 1920, 1080, 30.0, 30, 1, "yuv420p", "h264", "aac", 48000, 2, frame_count=120, video_end_seconds=4.0, audio_end_seconds=4.0)
    manifest = {"expected_duration_seconds": 4, "profile": {"width": 1920, "height": 1080, "fps": 30.0, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}}
    fingerprint = {"sha256": "b" * 64, "size_bytes": 10}
    missing = delivery_qa._container_check(probe, "", {"ok": True, "faststart": True}, manifest, {}, "a" * 64, fingerprint, delivery_qa._GENERAL_THRESHOLDS, render_report_status="missing")
    assert missing["status"] == "blocked"
    assert "Render Report" in missing["summary"]
    corrupt = delivery_qa._container_check(probe, "", {"ok": True, "faststart": True}, manifest, {}, "a" * 64, fingerprint, delivery_qa._GENERAL_THRESHOLDS, render_report_status="unparseable")
    assert corrupt["status"] == "blocked"
    assert corrupt["metrics"]["render_report"]["parseable"] is False
    incomplete = delivery_qa._container_check(probe, "", {"ok": True, "faststart": True}, manifest, {"manifest_hash": "a" * 64, "output_sha256": "b" * 64, "qc": {"passed": True}}, "a" * 64, fingerprint, delivery_qa._GENERAL_THRESHOLDS)
    assert incomplete["status"] == "blocked"
    assert "full decode" in incomplete["summary"]
    assert "timestamp continuity" in incomplete["summary"]


def test_invalid_threshold_override_blocks_and_valid_override_records_source(tmp_path: Path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "thresholds", content_type="travel_diary")
    cfg = {"library_root": str(tmp_path), "delivery_qa": {"threshold_overrides": {
        "black_block_seconds": "oops",
        "flash_brightness_delta": -1,
        "scene_change_threshold": float("nan"),
        "mystery_threshold": 3,
    }}}
    profile = delivery_qa.resolve_qa_profile(cfg, db, project_id)
    assert profile["threshold_validation"]["status"] == "blocked"
    assert len(profile["threshold_validation"]["invalid_overrides"]) == 3
    assert profile["threshold_validation"]["unknown_thresholds"][0]["key"] == "mystery_threshold"
    check = delivery_qa._threshold_config_check(profile)
    assert check["status"] == "blocked"

    valid_cfg = {"library_root": str(tmp_path), "delivery_qa": {"threshold_overrides": {"flash_brightness_delta": 80}}}
    valid = delivery_qa.resolve_qa_profile(valid_cfg, db, project_id)
    assert valid["resolved_thresholds"]["flash_brightness_delta"] == 80.0
    assert valid["threshold_sources"]["flash_brightness_delta"] == "config.global"
    assert valid["threshold_validation"]["valid_overrides"] == [{"source": "config.global", "key": "flash_brightness_delta", "resolved_value": 80.0}]

    (tmp_path / "run").mkdir()
    run_cfg, run_db, run_project, _folder, _source, run_output, snapshot, job_id = _fixture(tmp_path / "run", monkeypatch)
    run_cfg["delivery_qa"] = {"threshold_overrides": {"black_block_seconds": "oops"}}
    blocked_report = run_delivery_qa(run_cfg, run_db, run_project, render_job_uuid=job_id, output_path=run_output, approval_snapshot=snapshot, render_manifest_hash="a" * 64)
    assert next(item for item in blocked_report["checks"] if item["check_id"] == "threshold_config")["status"] == "blocked"
    assert blocked_report["lifecycle_status"] == "qa_blocked"
