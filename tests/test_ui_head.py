from io import BytesIO

from video_vault import ui


def _handler_class(monkeypatch, tmp_path):
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            return

    (tmp_path / "05_index").mkdir()
    monkeypatch.setattr(ui, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(ui.RenderJobManager, "start", lambda self: None)
    monkeypatch.setattr(ui, "FORM_CSRF_TOKEN", "")
    ui.run_ui({"library_root": str(tmp_path)}, port=18765)
    return captured["handler"]


def _bare_handler(handler_class, *, head_only):
    handler = object.__new__(handler_class)
    handler._head_only = head_only
    handler.wfile = BytesIO()
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    return handler


def test_do_head_dispatches_without_resetting_head_mode(monkeypatch, tmp_path):
    handler_class = _handler_class(monkeypatch, tmp_path)
    handler = object.__new__(handler_class)
    seen = []
    handler._handle_get = lambda *, head_only: seen.append(head_only)

    handler_class.do_HEAD(handler)

    assert seen == [True]


def test_head_json_and_html_write_headers_but_no_body(monkeypatch, tmp_path):
    handler_class = _handler_class(monkeypatch, tmp_path)
    head = _bare_handler(handler_class, head_only=True)
    get = _bare_handler(handler_class, head_only=False)

    handler_class._json(head, {"ok": True})
    handler_class._html(head, "<p>body</p>")
    handler_class._json(get, {"ok": True})

    assert head.wfile.getvalue() == b""
    assert get.wfile.getvalue()
