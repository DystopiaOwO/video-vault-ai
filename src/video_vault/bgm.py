from __future__ import annotations

from pathlib import Path
import shutil
import re
import urllib.request

from .database import add_bgm_track, add_project_bgm, bgm_tracks
from .ffmpeg_tools import metadata

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def bgm_dir(cfg: dict) -> Path:
    path = Path(cfg["library_root"]) / "04_audio" / "bgm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_bgm(cfg: dict, db: Path, source: Path, info: dict) -> int:
    if source.suffix.lower() not in AUDIO_EXTS:
        raise ValueError("only audio files are supported")
    target = _unique(bgm_dir(cfg) / source.name)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    row = {
        "title": info.get("title") or target.stem,
        "artist": info.get("artist") or "",
        "file_path": str(target),
        "source_url": info["source_url"],
        "license_name": info["license_name"],
        "license_url": info.get("license_url", ""),
        "attribution_required": 1 if info.get("attribution_required") else 0,
        "attribution_text": info.get("attribution_text") or attribution(info.get("title") or target.stem, info.get("artist") or "", info.get("source_url") or "", info.get("license_name") or ""),
        "mood": info.get("mood", ""),
        "duration_seconds": audio_duration(target, cfg),
    }
    return add_bgm_track(db, row)


def list_bgm(db: Path) -> list[dict]:
    return [dict(row) for row in bgm_tracks(db)]


ONLINE_BGM = {
    "travel": "https://www.free-stock-music.com/alex-productions-chill-electronic-vlog-music-hook.html",
    "coffee": "https://www.free-stock-music.com/alex-productions-chill-vibes.html",
    "default": "https://www.free-stock-music.com/alex-productions-chill-electronic-vlog-music-hook.html",
}


def auto_assign_bgm(cfg: dict, db: Path, project_id: int, project_info: dict, groups: list[dict]) -> dict | None:
    recommendations = recommend_bgm_for_groups(cfg, db, project_id, project_info, groups)
    return recommendations[0]["track"] if recommendations else None


def recommend_bgm_for_groups(cfg: dict, db: Path, project_id: int, project_info: dict, groups: list[dict]) -> list[dict]:
    tracks = [dict(row) for row in bgm_tracks(db)]
    if not tracks:
        online = download_online_bgm(cfg, db, _online_key(project_info, groups))
        if online:
            tracks = [online]
    if not tracks:
        return []
    result = []
    for group in groups:
        target = _group_mood(project_info, group)
        chosen = sorted(tracks, key=lambda track: _bgm_score(track, target), reverse=True)[0]
        add_project_bgm(db, project_id, int(chosen["id"]))
        result.append({"group": group.get("label", ""), "activity": group.get("activity", ""), "mood": sorted(target), "track": chosen})
    return result


def download_online_bgm(cfg: dict, db: Path, key: str = "default") -> dict | None:
    url = ONLINE_BGM.get(key, ONLINE_BGM["default"])
    try:
        html = _fetch_text(url)
        mp3 = _first_match(r"id=['\"]dbmp3_0['\"][^>]+href=['\"]([^'\"]+\.mp3)['\"]", html) or _first_match(r"href=['\"]([^'\"]+\.mp3)['\"][^>]+id=['\"]dbmp3_0['\"]", html)
        if not mp3:
            return None
        mp3_url = urllib.parse.urljoin(url, mp3)
        title = _clean(_first_match(r"<div class=['\"]mTitle['\"]>(.*?)</div>", html)) or Path(mp3).stem
        artist = _clean(_first_match(r"<div class=['\"]mAuthor['\"][^>]*>.*?>(.*?)</a>", html)) or ""
        credit = _clean((_first_match(r"<div class=['\"]creditTextExample['\"]><b>(.*?)</b></div>", html) or "").replace("|BR|", "\n"))
        tmp = Path(cfg["library_root"]) / "04_audio" / "_online_bgm" / Path(mp3).name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        _download(mp3_url, tmp)
        track_id = import_bgm(
            cfg,
            db,
            tmp,
            {
                "title": title,
                "artist": artist,
                "source_url": url,
                "license_name": "Creative Commons / Attribution",
                "license_url": "https://creativecommons.org/licenses/",
                "attribution_required": True,
                "attribution_text": credit or attribution(title, artist, url, "Creative Commons / Attribution"),
                "mood": key,
            },
        )
        return next(track for track in list_bgm(db) if int(track["id"]) == track_id)
    except Exception:
        return None


def attribution(title: str, artist: str, source_url: str, license_name: str) -> str:
    bits = [f'"{title}"']
    if artist:
        bits.append(f"by {artist}")
    if license_name:
        bits.append(f"({license_name})")
    if source_url:
        bits.append(source_url)
    return " ".join(bits)


def youtube_credits(db: Path) -> str:
    lines = []
    for track in bgm_tracks(db):
        lines.append(track["attribution_text"] or attribution(track["title"], track["artist"], track["source_url"], track["license_name"]))
    return "\n".join(lines)


def _project_mood(project_info: dict, groups: list[dict]) -> set[str]:
    words = {str(project_info.get("category", "")).lower(), str(project_info.get("content_type", "")).lower()}
    for group in groups:
        words.add(str(group.get("activity", "")).lower())
        words.add(str(group.get("label", "")).lower())
    if {"travel", "travel_diary", "風景", "逛街"} & words:
        return {"travel", "bright", "uplifting", "cinematic", "vlog", "chill"}
    if {"coffee", "matcha", "food", "飲食"} & words:
        return {"coffee", "food", "warm", "chill", "lofi", "cozy"}
    return {"vlog", "chill", "bright"}


def _group_mood(project_info: dict, group: dict) -> set[str]:
    return _project_mood(project_info, [group])


def _online_key(project_info: dict, groups: list[dict]) -> str:
    mood = _project_mood(project_info, groups)
    if "travel" in mood:
        return "travel"
    if {"coffee", "food"} & mood:
        return "coffee"
    return "default"


def _bgm_score(track: dict, target: set[str]) -> int:
    haystack = " ".join(str(track.get(k, "")).lower() for k in ("title", "artist", "mood", "license_name"))
    return sum(1 for word in target if word in haystack)


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 video-vault-ai/0.1"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def _download(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 video-vault-ai/0.1", "Referer": "https://www.free-stock-music.com/"})
    with urllib.request.urlopen(req, timeout=60) as response, out.open("wb") as f:
        shutil.copyfileobj(response, f)


def _first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.S | re.I)
    return match.group(1) if match else ""


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def audio_duration(path: Path, cfg: dict) -> float:
    try:
        return float(metadata(path, cfg)["duration_seconds"])
    except Exception:
        return 0.0


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find unique filename for {path}")
