from io import BytesIO
from types import SimpleNamespace
import errno
import hashlib
from pathlib import Path

import pytest

import video_vault.ui as ui
from video_vault.database import connect, create_project_row, init_db, project_videos


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
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "test")
    cfg = {"library_root": str(tmp_path / "library")}
    source_dir = ui.project_dir(cfg, project_id) / "source"
    existing = source_dir / "old.mp4"
    existing.write_bytes(b"old")
    monkeypatch.setattr(ui, "metadata", lambda *args: (_ for _ in ()).throw(RuntimeError("probe failed")))
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "clip.mp4", b"new")])
    result = ui.upload_project(_handler(body, boundary), cfg, db)
    assert result["ok"] is False
    assert "metadata" in result["error"]
    assert existing.read_bytes() == b"old"
    assert not list(source_dir.glob(".video-vault-upload-*"))


def _project_upload_setup(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "upload test")
    cfg = {"library_root": str(tmp_path / "library")}
    folder = ui.project_dir(cfg, project_id)
    return cfg, db, project_id, folder, folder / "source"


def _metadata(*args):
    return {"duration_seconds": 1.0, "width": 1280, "height": 720, "fps": 30.0, "codec": "h264", "file_size": 3}


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_project_duplicate_filename_is_rejected_without_upsert(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    existing = source_dir / "clip.mp4"
    existing.write_bytes(b"original")
    digest = _sha256(existing)
    calls = []
    monkeypatch.setattr(ui, "upsert_video", lambda *args: calls.append(args) or 99)
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "clip.mp4", b"new")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert result.get("code") == "duplicate_filename", result
    assert "同名" in result["error"]
    assert _sha256(existing) == digest
    assert calls == []
    assert not list(source_dir.glob(".video-vault-upload-*"))


def test_project_upload_uses_successful_no_clobber_fallback(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    monkeypatch.setattr(ui, "metadata", _metadata)

    def no_hard_links(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(ui.os, "link", no_hard_links)
    payload = b"fallback video bytes"
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "fallback.mp4", payload)])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    destination = source_dir / "fallback.mp4"
    assert result["ok"] is True
    assert destination.read_bytes() == payload
    assert destination.stat().st_size == len(payload)
    assert not list(source_dir.glob(".video-vault-upload-*"))
    assert len(ui.videos(db)) == 1
    assert len(project_videos(db, project_id)) == 1


def test_project_upload_fallback_partial_copy_rolls_back(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    monkeypatch.setattr(ui, "metadata", _metadata)
    real_fdopen = ui.os.fdopen

    def no_hard_links(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    class FailingOutput:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.inner.close()
            return False

        def write(self, value):
            self.inner.write(value[:1])
            raise OSError("destination write failed")

        def flush(self):
            return self.inner.flush()

        def fileno(self):
            return self.inner.fileno()

    monkeypatch.setattr(ui.os, "link", no_hard_links)
    monkeypatch.setattr(ui.os, "fdopen", lambda fd, mode: FailingOutput(real_fdopen(fd, mode)))
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "partial.mp4", b"partial payload")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert "素材發布失敗" in result["error"]
    assert not (source_dir / "partial.mp4").exists()
    assert not list(source_dir.glob(".video-vault-upload-*"))
    assert ui.videos(db) == []
    assert project_videos(db, project_id) == []


def test_project_upload_fallback_race_preserves_existing_destination(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    monkeypatch.setattr(ui, "metadata", _metadata)
    real_open = ui.os.open

    def no_hard_links(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    def racing_open(path, flags, mode=0o777):
        if Path(path).name == "raced.mp4":
            Path(path).write_bytes(b"racing upload")
            raise FileExistsError(errno.EEXIST, "destination appeared")
        return real_open(path, flags, mode)

    monkeypatch.setattr(ui.os, "link", no_hard_links)
    monkeypatch.setattr(ui.os, "open", racing_open)
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "raced.mp4", b"new payload")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert result.get("code") == "duplicate_filename", result
    assert (source_dir / "raced.mp4").read_bytes() == b"racing upload"
    assert not list(source_dir.glob(".video-vault-upload-*"))
    assert ui.videos(db) == []
    assert project_videos(db, project_id) == []


def test_upsert_failure_rolls_back_published_file_and_registration(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    monkeypatch.setattr(ui, "metadata", _metadata)
    monkeypatch.setattr(ui, "upsert_video", lambda *args: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "new.mp4", b"new")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert "資料庫登記失敗" in result["error"]
    assert not (source_dir / "new.mp4").exists()
    assert project_videos(db, project_id) == []
    assert not list(source_dir.glob(".video-vault-upload-*"))


@pytest.mark.parametrize("failure_name", ["set_project_videos", "sync_project_files"])
def test_project_registration_failure_restores_existing_relation_and_source(monkeypatch, tmp_path, failure_name):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    existing_source = source_dir / "old.mp4"
    existing_source.write_bytes(b"old")
    monkeypatch.setattr(ui, "metadata", _metadata)
    existing_video_id = ui.upsert_video(db, {"original_path": str(existing_source), "current_path": str(existing_source), "filename": existing_source.name, "category": "unknown", **_metadata(), "status": "uploaded"})
    ui.set_project_videos(db, project_id, [existing_video_id])
    original_digest = _sha256(existing_source)
    original_relation = [int(row["id"]) for row in project_videos(db, project_id)]
    if failure_name == "set_project_videos":
        monkeypatch.setattr(ui, "set_project_videos", lambda *args: (_ for _ in ()).throw(RuntimeError("relation unavailable")))
    else:
        monkeypatch.setattr(ui, "sync_project_files", lambda *args: (_ for _ in ()).throw(RuntimeError("sync unavailable")))
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "new.mp4", b"new")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert "關聯失敗" in result["error"] or "同步失敗" in result["error"]
    assert _sha256(existing_source) == original_digest
    assert [int(row["id"]) for row in project_videos(db, project_id)] == original_relation
    assert not (source_dir / "new.mp4").exists()
    assert not list(source_dir.glob(".video-vault-upload-*"))


def test_second_file_metadata_failure_is_request_level_all_or_nothing(monkeypatch, tmp_path):
    cfg, db, project_id, _, source_dir = _project_upload_setup(tmp_path)
    calls = []

    def metadata_for_file(path, _cfg):
        calls.append(path.name)
        if len(calls) == 2:
            raise RuntimeError("bad media")
        return _metadata()

    monkeypatch.setattr(ui, "metadata", metadata_for_file)
    body, boundary = _body([("project_id", None, str(project_id)), ("file", "good.mp4", b"good"), ("file", "bad.mp4", b"bad")])

    result = ui.upload_project(_handler(body, boundary), cfg, db)

    assert result["ok"] is False
    assert "metadata" in result["error"]
    assert len(calls) == 2
    assert not (source_dir / "good.mp4").exists()
    assert not (source_dir / "bad.mp4").exists()
    assert project_videos(db, project_id) == []
    assert not list(source_dir.glob(".video-vault-upload-*"))


def test_truncated_large_part_closes_rolled_spool(monkeypatch):
    monkeypatch.setattr(ui, "UPLOAD_SPOOL_THRESHOLD", 8)
    body, boundary = _body([("file", "large.mp4", b"0123456789abcdef")])
    real_spool = ui.tempfile.SpooledTemporaryFile

    class TrackingSpool:
        instances = []

        def __init__(self, *args, **kwargs):
            self.inner = real_spool(*args, **kwargs)
            self.closed = False
            self.rolled = False
            self.__class__.instances.append(self)

        def write(self, value):
            result = self.inner.write(value)
            self.rolled = bool(getattr(self.inner, "_rolled", False))
            return result

        def seek(self, *args):
            return self.inner.seek(*args)

        def close(self):
            self.closed = True
            return self.inner.close()

    monkeypatch.setattr(ui.tempfile, "SpooledTemporaryFile", TrackingSpool)
    with pytest.raises(ui.MultipartFormError):
        ui._multipart_form(_handler(body[:-5], boundary, content_length=len(body) - 5))
    assert TrackingSpool.instances[0].rolled is True
    assert TrackingSpool.instances[0].closed is True


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
