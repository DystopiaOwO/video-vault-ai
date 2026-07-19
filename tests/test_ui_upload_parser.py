from io import BytesIO
from types import SimpleNamespace

import pytest

import video_vault.ui as ui


def _body(parts, boundary="video-vault-test"):
    chunks = []
    for name, filename, payload in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append((disposition + "\r\n").encode())
        if filename is not None:
            chunks.append(b"Content-Type: video/mp4\r\n")
        chunks.append(b"\r\n")
        chunks.append(payload if isinstance(payload, bytes) else str(payload).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _handler(body, boundary, stream=None, content_length=None):
    headers = SimpleNamespace()
    values = {"content-length": str(len(body) if content_length is None else content_length), "content-type": f"multipart/form-data; boundary={boundary}"}
    headers.get = values.get
    return SimpleNamespace(headers=headers, rfile=stream or BytesIO(body))


def test_multipart_upload_parser_preserves_text_and_file_fields():
    body, boundary = _body([("project_id", None, "7"), ("file", "clip.mp4", b"fake video bytes")])
    handler = _handler(body, boundary)

    form = ui._multipart_form(handler)

    assert form["project_id"][0].value == "7"
    assert form["file"][0].filename == "clip.mp4"
    assert form["file"][0].file.read() == b"fake video bytes"
    ui._close_form(form)


class FixedChunkStream(BytesIO):
    def __init__(self, value: bytes, maximum: int = ui.UPLOAD_READ_CHUNK):
        super().__init__(value)
        self.maximum = maximum
        self.read_sizes = []

    def read(self, size=-1):
        if size is None or size < 0 or size > self.maximum:
            raise AssertionError(f"unbounded read: {size}")
        self.read_sizes.append(size)
        return super().read(size)


def test_parser_reads_request_in_bounded_chunks_and_rolls_large_file_to_disk(monkeypatch):
    monkeypatch.setattr(ui, "UPLOAD_SPOOL_THRESHOLD", 8)
    body, boundary = _body([("file", "large clip.mp4", b"0123456789abcdef")])
    stream = FixedChunkStream(body)
    form = ui._multipart_form(_handler(body, boundary, stream=stream))
    try:
        item = form["file"][0]
        assert getattr(item.file, "_rolled", False) is True
        assert max(stream.read_sizes) <= ui.UPLOAD_READ_CHUNK
        assert item.file.read() == b"0123456789abcdef"
    finally:
        ui._close_form(form)


def test_parser_supports_multiple_files_and_chinese_filename():
    body, boundary = _body([("project_id", None, "7"), ("file", "南港 早上.mp4", b"one"), ("file", "咖啡.mp4", b"two")])
    form = ui._multipart_form(_handler(body, boundary))
    try:
        assert _values(form, "project_id") == ["7"]
        assert [item.filename for item in form["file"]] == ["南港 早上.mp4", "咖啡.mp4"]
        assert [item.file.read() for item in form["file"]] == [b"one", b"two"]
    finally:
        ui._close_form(form)


def _values(form, name):
    return [item.value for item in form[name]]


def test_truncated_multipart_is_rejected_without_publishing():
    body, boundary = _body([("file", "clip.mp4", b"payload")])
    with pytest.raises(ui.MultipartFormError, match="截斷|結束"):
        ui._multipart_form(_handler(body[:-5], boundary, content_length=len(body) - 5))


def test_missing_file_field_returns_structured_upload_error(tmp_path):
    body, boundary = _body([("project_id", None, "7")])
    result = ui.upload(_handler(body, boundary), {"library_root": str(tmp_path), "inbox_dir": "inbox"})
    assert result == {"ok": False, "error": "缺少 file 欄位", "files": []}


def test_metadata_failure_removes_staged_file_and_preserves_existing_source(monkeypatch, tmp_path):
    body, boundary = _body([("project_id", None, "7"), ("file", "clip.mp4", b"new")])
    source_dir = tmp_path / "project" / "source"
    source_dir.mkdir(parents=True)
    existing = source_dir / "clip.mp4"
    existing.write_bytes(b"old")
    monkeypatch.setattr(ui, "project_dir", lambda cfg, project_id: tmp_path / "project")
    monkeypatch.setattr(ui, "project_videos", lambda db, project_id: [])
    monkeypatch.setattr(ui, "metadata", lambda *args: (_ for _ in ()).throw(RuntimeError("probe failed")))
    result = ui.upload_project(_handler(body, boundary), {"library_root": str(tmp_path)}, tmp_path / "db.sqlite3")
    assert result["ok"] is False
    assert "metadata" in result["error"]
    assert existing.read_bytes() == b"old"
    assert not list(source_dir.glob(".video-vault-upload-*"))


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"content-length": "not-a-number", "content-type": "multipart/form-data; boundary=x"}, "Content-Length"),
        ({"content-length": "0", "content-type": "multipart/form-data"}, "boundary"),
    ],
)
def test_invalid_multipart_headers_are_structured_errors(headers, message):
    header = SimpleNamespace()
    header.get = headers.get
    with pytest.raises(ui.MultipartFormError, match=message):
        ui._multipart_form(SimpleNamespace(headers=header, rfile=BytesIO()))
