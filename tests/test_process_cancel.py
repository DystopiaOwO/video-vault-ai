import pytest
from video_vault.process_manager import FFmpegProcessManager


def test_unknown_job_is_explicit():
    with pytest.raises(KeyError): FFmpegProcessManager().terminate("missing")
