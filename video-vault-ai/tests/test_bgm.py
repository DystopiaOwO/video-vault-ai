from pathlib import Path

from video_vault.bgm import download_online_bgm, import_bgm, list_bgm, youtube_credits
from video_vault.database import init_db


def test_import_bgm_tracks_source_and_license(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    monkeypatch.setattr("video_vault.bgm.audio_duration", lambda path, cfg: 12.3)
    track_id = import_bgm(
        {"library_root": str(tmp_path)},
        db,
        audio,
        {
            "title": "Song",
            "artist": "Artist",
            "source_url": "https://example.com/song",
            "license_name": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution_required": True,
            "mood": "calm",
        },
    )
    tracks = list_bgm(db)
    assert track_id == 1
    assert tracks[0]["source_url"] == "https://example.com/song"
    assert tracks[0]["license_name"] == "CC BY 4.0"
    assert "Song" in youtube_credits(db)


def test_download_online_bgm_imports_track(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    monkeypatch.setattr("video_vault.bgm.audio_duration", lambda path, cfg: 12.3)
    monkeypatch.setattr(
        "video_vault.bgm._fetch_text",
        lambda url: """<div class='mTitle'>Chill Test</div><div class='mAuthor'><a>Artist</a></div>
        <a id='dbmp3_0' href='/music/test.mp3'>MP3</a>
        <div class='creditTextExample'><b>Chill Test by Artist<br/>CC BY</b></div>""",
    )

    def fake_download(url, out):
        Path(out).write_bytes(b"mp3")

    monkeypatch.setattr("video_vault.bgm._download", fake_download)
    track = download_online_bgm({"library_root": str(tmp_path)}, db, "travel")

    assert track["title"] == "Chill Test"
    assert track["artist"] == "Artist"
    assert "CC BY" in track["attribution_text"]
