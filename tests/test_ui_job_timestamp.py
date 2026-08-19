from video_vault.ui import _job_updated_at_sort_key, _jobs_panel


def test_job_timestamp_sort_key_normalizes_numeric_and_timezone_aware_iso_values():
    newer = _job_updated_at_sort_key("2026-08-19T20:36:26.344+08:00")
    same_utc = _job_updated_at_sort_key("2026-08-19T12:36:26.344Z")
    numeric = _job_updated_at_sort_key(newer)

    assert newer == same_utc
    assert numeric == newer


def test_jobs_panel_mixed_timestamps_renders_newest_first_without_mutation():
    jobs = [
        {"kind": "舊工作", "status": "done", "message": "old", "done": 1, "total": 1, "percent": 100, "updated_at": "2026-08-19T12:00:00Z", "project_id": 7},
        {"kind": "最新工作", "status": "running", "message": "new", "done": 1, "total": 2, "percent": 50, "updated_at": 1787143200.0, "project_id": 7},
    ]
    original = [dict(job) for job in jobs]

    html = _jobs_panel(jobs)

    assert html.index("最新工作") < html.index("舊工作")
    assert 'class="pill wait"' in html
    assert "1/2｜50%" in html
    assert 'class="bar"' in html
    assert "停止目前工作" in html
    assert jobs == original


def test_job_timestamp_invalid_values_are_oldest_and_ties_preserve_input_order():
    invalid_values = [None, "", "invalid", float("nan"), float("inf"), True, object()]

    keys = [_job_updated_at_sort_key(value) for value in invalid_values]
    assert all(key == float("-inf") for key in keys)

    jobs = [
        {"kind": "無效 A", "status": "failed", "updated_at": None},
        {"kind": "無效 B", "status": "failed", "updated_at": "not-a-date"},
    ]
    html = _jobs_panel(jobs)
    assert html.index("無效 A") < html.index("無效 B")


def test_job_timestamp_naive_iso_is_rejected_instead_of_using_local_timezone():
    assert _job_updated_at_sort_key("2026-08-19T12:36:26.344") == float("-inf")
