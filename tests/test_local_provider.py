from video_vault.analyzer.local_provider import ensure_local_model_server


def test_ensure_local_model_server_starts_and_loads(monkeypatch):
    calls = []
    ready = iter([False, False, True])

    monkeypatch.setattr("video_vault.analyzer.local_provider._model_ready", lambda *args, **kwargs: next(ready))
    monkeypatch.setattr("video_vault.analyzer.local_provider.shutil.which", lambda name: "lms")
    monkeypatch.setattr("video_vault.analyzer.local_provider.subprocess.run", lambda cmd, **kwargs: calls.append(cmd))

    ensure_local_model_server({"ai": {"local": {"context_length": 8192, "parallel": 1, "gpu": "max", "ttl_seconds": 300}}}, "http://127.0.0.1:1234/v1", "gemma-4-12b-it")

    assert calls[0] == ["lms", "server", "start"]
    assert calls[1] == ["lms", "load", "gemma-4-12b-it", "--gpu", "max", "-c", "8192", "--parallel", "1", "--ttl", "300", "--identifier", "gemma-4-12b-it", "-y"]
