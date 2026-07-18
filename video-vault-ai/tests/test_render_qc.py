import json

from video_vault.render_qc import write_qc_report
from video_vault.render_types import QcReport


def test_qc_reports_are_written_as_json_and_markdown(tmp_path):
    report = QcReport(False, status="failed_qc", errors=["bad duration"], warnings=["vfr"])
    json_path, md_path = write_qc_report(report, tmp_path / "qc")
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "failed_qc"
    assert "bad duration" in md_path.read_text(encoding="utf-8")
