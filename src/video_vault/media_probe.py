"""Small, safe ffprobe adapter used by single-segment rendering."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Mapping

from .render_errors import MediaProbeError
from .source_fingerprint import resolve_source_fingerprint, source_stat


@dataclass(frozen=True)
class MediaProbe:
    source_file: Path
    duration_seconds: float
    has_video: bool
    has_audio: bool
    width: int
    height: int
    fps: float
    fps_num: int
    fps_den: int
    pixel_format: str
    video_codec: str
    audio_codec: str
    sample_rate: int
    channels: int
    color_primaries: str = ""
    color_transfer: str = ""
    color_matrix: str = ""
    color_range: str = ""
    frame_count: int = 0
    video_start_seconds: float = 0.0
    video_end_seconds: float = 0.0
    audio_start_seconds: float = 0.0
    audio_end_seconds: float = 0.0
    video_stream_index: int = -1
    audio_stream_index: int = -1
    # ``width``/``height`` remain the visible coded dimensions for backwards
    # compatibility.  The fields below make the source display transform
    # explicit so renderers do not confuse coded geometry with what a normal
    # player displays.
    coded_width: int = 0
    coded_height: int = 0
    sample_aspect_ratio: str = "1:1"
    display_aspect_ratio: str = ""
    display_ratio: float = 0.0
    display_width: int = 0
    display_height: int = 0
    rotation_degrees: int = 0
    display_matrix: str = ""
    display_geometry_source: str = ""


MediaProbeResult = MediaProbe


def probe_media(ffprobe_path: str, path: Path, mode: str = "deep") -> MediaProbe:
    """Probe media metadata.

    ``deep`` is the historical contract and asks ffprobe to count frames and
    packets.  ``fast`` intentionally reads stream/container metadata only;
    callers that need complete decode, frame counts, or packet continuity must
    continue using the deep contract.
    """
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise MediaProbeError(f"source file does not exist: {source}")
    normalized_mode = str(mode or "deep").strip().lower()
    if normalized_mode not in {"fast", "deep"}:
        raise MediaProbeError(f"unsupported media probe mode: {mode!r}")
    command = [
        str(ffprobe_path),
        "-v",
        "error",
    ]
    if normalized_mode == "deep":
        command.extend(["-count_frames", "-count_packets"])
    command.extend([
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    ])
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as exc:
        raise MediaProbeError(f"unable to start ffprobe: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffprobe error").strip()
        raise MediaProbeError(f"ffprobe failed for {source}: {detail}")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"ffprobe returned invalid JSON for {source}") from exc
    return _parse(source, raw, include_frame_count=normalized_mode == "deep")


def probe_media_metadata(ffprobe_path: str, path: Path) -> MediaProbe:
    """Read header/stream metadata without frame or packet counting."""

    return probe_media(ffprobe_path, path, "fast")


class SourceProbeRegistry:
    """Job-scoped, identity-checked, single-flight source metadata probes."""

    def __init__(self, ffprobe_path: str, *, approved_fingerprints: Mapping[str, Mapping[str, Any]] | None = None):
        self.ffprobe_path = str(ffprobe_path)
        self.approved_fingerprints = {
            str(Path(path).expanduser().resolve()): dict(value)
            for path, value in (approved_fingerprints or {}).items()
        }
        self._condition = threading.Condition()
        self._probes: dict[tuple[Any, ...], MediaProbe] = {}
        self._inflight: dict[tuple[Any, ...], threading.Event] = {}
        self._errors: dict[tuple[Any, ...], BaseException] = {}
        self._source_keys: set[tuple[Any, ...]] = set()
        self._source_key_by_path: dict[str, tuple[Any, ...]] = {}
        self._fingerprints: dict[str, dict[str, Any]] = {}
        self._source_probe_calls = 0
        self._source_probe_cache_hits = 0
        self._source_probe_latency_ms = 0.0

    def probe(self, path: Path) -> MediaProbe:
        source = Path(path).expanduser().resolve()
        before = self._validated_stat(source)
        key = self._identity_key(source, before)
        while True:
            with self._condition:
                previous_key = self._source_key_by_path.get(str(source))
                if previous_key is not None and previous_key != key:
                    raise MediaProbeError(f"source identity changed during render job: {source}")
                cached = self._probes.get(key)
                if cached is not None:
                    self._source_probe_cache_hits += 1
                    self._assert_unchanged(source, before)
                    return cached
                waiter = self._inflight.get(key)
                if waiter is None:
                    self._errors.pop(key, None)
                    waiter = threading.Event()
                    self._inflight[key] = waiter
                    owner = True
                else:
                    owner = False
            if owner:
                break
            waiter.wait()
            with self._condition:
                error = self._errors.pop(key, None)
            if error is not None:
                raise error

        started = time.perf_counter()
        try:
            result = probe_media_metadata(self.ffprobe_path, source)
            self._assert_unchanged(source, before)
            with self._condition:
                self._probes[key] = result
                self._source_keys.add(key)
                self._source_key_by_path[str(source)] = key
                self._source_probe_calls += 1
                self._source_probe_latency_ms += (time.perf_counter() - started) * 1000.0
            return result
        except BaseException as exc:
            with self._condition:
                self._errors[key] = exc
            raise
        finally:
            with self._condition:
                event = self._inflight.pop(key, None)
                if event is not None:
                    event.set()

    def audit(self) -> dict[str, Any]:
        with self._condition:
            return {
                "unique_source_count": len(self._source_keys),
                "source_probe_calls": self._source_probe_calls,
                "source_probe_cache_hits": self._source_probe_cache_hits,
                "source_probe_mode": "fast_metadata",
                "source_probe_latency_ms": round(self._source_probe_latency_ms, 3),
            }

    def fingerprint(self, path: Path) -> dict[str, Any]:
        """Return approved source evidence without rereading source bytes."""

        source = Path(path).expanduser().resolve()
        current = self._validated_stat(source)
        source_key = str(source)
        with self._condition:
            cached = self._fingerprints.get(source_key)
            if cached is not None and self._stat_version(cached["stat"]) == self._stat_version(current):
                return dict(cached["fingerprint"])
        approved = self.approved_fingerprints.get(source_key)
        if approved and str(approved.get("sha256") or ""):
            fingerprint = {
                "path": source_key,
                "size": int(current["size"]),
                "mtime_ns": int(current["mtime_ns"]),
                "sha256": str(approved["sha256"]),
            }
        else:
            fingerprint = resolve_source_fingerprint(source, approved)
        after = self._validated_stat(source)
        if self._stat_version(current) != self._stat_version(after):
            raise MediaProbeError(f"source changed while resolving cache identity: {source}")
        with self._condition:
            self._fingerprints[source_key] = {"stat": dict(after), "fingerprint": dict(fingerprint)}
        return dict(fingerprint)

    def _validated_stat(self, source: Path) -> dict[str, Any]:
        try:
            current = source_stat(source)
        except OSError as exc:
            raise MediaProbeError(f"source identity unavailable: {source}: {exc}") from exc
        approved = self.approved_fingerprints.get(str(source))
        if approved:
            self._assert_approved_stat(source, current, approved)
        return current

    def _assert_unchanged(self, source: Path, before: Mapping[str, Any]) -> None:
        try:
            after = source_stat(source)
        except OSError as exc:
            raise MediaProbeError(f"source identity unavailable after probe: {source}: {exc}") from exc
        if self._stat_version(before) != self._stat_version(after):
            raise MediaProbeError(f"source changed during fast probe: {source}")
        approved = self.approved_fingerprints.get(str(source))
        if approved:
            self._assert_approved_stat(source, after, approved)

    @staticmethod
    def _assert_approved_stat(source: Path, current: Mapping[str, Any], approved: Mapping[str, Any]) -> None:
        approved_path = str(approved.get("canonical_path") or approved.get("path") or "")
        if approved_path and Path(approved_path).expanduser().resolve() != source:
            raise MediaProbeError(f"approved source path mismatch: {source}")
        try:
            if int(approved.get("size")) != int(current["size"]):
                raise MediaProbeError(f"approved source size mismatch: {source}")
            if int(approved.get("mtime_ns")) != int(current["mtime_ns"]):
                raise MediaProbeError(f"approved source mtime mismatch: {source}")
        except (TypeError, ValueError, KeyError) as exc:
            raise MediaProbeError(f"approved source fingerprint evidence is invalid: {source}") from exc
        approved_identity = approved.get("source_identity")
        if approved_identity and dict(approved_identity) != dict(current.get("source_identity") or {}):
            raise MediaProbeError(f"approved source identity mismatch: {source}")

    @staticmethod
    def _stat_version(value: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            int(value.get("size", -1)),
            int(value.get("mtime_ns", -1)),
            tuple(sorted(dict(value.get("source_identity") or {}).items())),
        )

    @staticmethod
    def _identity_key(source: Path, stat: Mapping[str, Any]) -> tuple[Any, ...]:
        identity = dict(stat.get("source_identity") or {})
        return (
            str(source),
            int(stat.get("size", -1)),
            int(stat.get("mtime_ns", -1)),
            tuple(sorted(identity.items())),
        )


def _parse(source: Path, raw: dict[str, Any], *, include_frame_count: bool = True) -> MediaProbe:
    streams = list(raw.get("streams") or [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise MediaProbeError(f"no video stream found: {source}")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    fps_num, fps_den = _video_frame_rate(video)
    duration = _number((raw.get("format") or {}).get("duration")) or _number(video.get("duration")) or 0.0
    geometry = _display_geometry(video)
    return MediaProbe(
        source_file=source,
        duration_seconds=duration,
        has_video=True,
        has_audio=audio is not None,
        width=_integer(video.get("width")),
        height=_integer(video.get("height")),
        fps=fps_num / fps_den if fps_den else 0.0,
        fps_num=fps_num,
        fps_den=fps_den,
        pixel_format=str(video.get("pix_fmt") or ""),
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or "") if audio else "",
        sample_rate=_integer(audio.get("sample_rate")) if audio else 0,
        channels=_integer(audio.get("channels")) if audio else 0,
        color_primaries=str(video.get("color_primaries") or ""),
        color_transfer=str(video.get("color_transfer") or video.get("color_trc") or ""),
        color_matrix=str(video.get("color_space") or ""),
        color_range=str(video.get("color_range") or ""),
        frame_count=_integer(video.get("nb_read_frames") or video.get("nb_frames")) if include_frame_count else 0,
        video_start_seconds=_number(video.get("start_time")) or 0.0,
        video_end_seconds=(_number(video.get("start_time")) or 0.0) + (_number(video.get("duration")) or duration),
        audio_start_seconds=_number(audio.get("start_time")) if audio else 0.0,
        audio_end_seconds=((_number(audio.get("start_time")) or 0.0) + (_number(audio.get("duration")) or duration)) if audio else 0.0,
        video_stream_index=_integer(video.get("index")),
        audio_stream_index=_integer(audio.get("index")) if audio else -1,
        coded_width=_integer(video.get("coded_width") or video.get("width")),
        coded_height=_integer(video.get("coded_height") or video.get("height")),
        sample_aspect_ratio=geometry["sample_aspect_ratio"],
        display_aspect_ratio=geometry["display_aspect_ratio"],
        display_ratio=geometry["display_ratio"],
        display_width=geometry["display_width"],
        display_height=geometry["display_height"],
        rotation_degrees=geometry["rotation_degrees"],
        display_matrix=geometry["display_matrix"],
        display_geometry_source=geometry["display_geometry_source"],
    )


def _display_geometry(video: Mapping[str, Any]) -> dict[str, Any]:
    width = _integer(video.get("width"))
    height = _integer(video.get("height"))
    if width <= 0 or height <= 0:
        # Keep the historical parser permissive for mocked/header-incomplete
        # probes.  Render-time callers still reject invalid dimensions through
        # their existing media/QC contracts; unknown display geometry must not
        # be mistaken for a verified transform.
        return {
            "sample_aspect_ratio": "1:1",
            "display_aspect_ratio": "",
            "display_ratio": 0.0,
            "display_width": width,
            "display_height": height,
            "rotation_degrees": 0,
            "display_matrix": "",
            "display_geometry_source": "unknown",
        }

    sar = _parse_ratio(video.get("sample_aspect_ratio"), default=Fraction(1, 1))
    rotation, matrix = _display_rotation(video)
    coded_ratio = Fraction(width * sar.numerator, height * sar.denominator)
    displayed = (Fraction(1, 1) / coded_ratio) if abs(rotation) % 180 == 90 else coded_ratio
    displayed = displayed.limit_denominator(100000)
    if abs(rotation) % 180 == 90:
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height
    return {
        "sample_aspect_ratio": f"{sar.numerator}:{sar.denominator}",
        "display_aspect_ratio": f"{displayed.numerator}:{displayed.denominator}",
        "display_ratio": float(displayed),
        "display_width": display_width,
        "display_height": display_height,
        "rotation_degrees": rotation,
        "display_matrix": matrix,
        "display_geometry_source": "display_matrix" if matrix else "sample_aspect_ratio",
    }


def _display_rotation(video: Mapping[str, Any]) -> tuple[int, str]:
    side_data = video.get("side_data_list") or []
    for item in side_data:
        if not isinstance(item, Mapping) or str(item.get("side_data_type") or "").lower() != "display matrix":
            continue
        rotation = _integer(item.get("rotation"))
        matrix = str(item.get("displaymatrix") or "")
        return _normalize_rotation(rotation), matrix
    tags = video.get("tags") if isinstance(video.get("tags"), Mapping) else {}
    return _normalize_rotation(_integer(tags.get("rotate"))), ""


def _normalize_rotation(value: int) -> int:
    normalized = int(value) % 360
    return normalized - 360 if normalized > 180 else normalized


def _parse_ratio(value: Any, *, default: Fraction | None = None) -> Fraction:
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "0:0", "0/0"}:
        if default is not None:
            return default
        raise MediaProbeError(f"invalid aspect ratio: {value!r}")
    separator = ":" if ":" in text else "/"
    try:
        numerator, denominator = (int(part) for part in text.split(separator, 1))
        if numerator <= 0 or denominator <= 0:
            raise ValueError
        return Fraction(numerator, denominator)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise MediaProbeError(f"invalid aspect ratio: {value!r}")


def _fraction(value: Any) -> tuple[int, int]:
    if value is None or str(value).strip().upper() in {"", "N/A", "0/0"}:
        raise MediaProbeError(f"invalid frame rate: {value!r}")
    try:
        numerator, denominator = str(value).strip().split("/", 1)
        num = int(numerator)
        den = int(denominator)
        if num <= 0 or den <= 0:
            raise ValueError
        return num, den
    except (TypeError, ValueError):
        raise MediaProbeError(f"invalid frame rate: {value!r}")


def _video_frame_rate(video: Mapping[str, Any]) -> tuple[int, int]:
    failures: list[str] = []
    for field in ("avg_frame_rate", "r_frame_rate"):
        value = video.get(field)
        try:
            return _fraction(value)
        except MediaProbeError as exc:
            failures.append(f"{field}={value!r} ({exc})")
    raise MediaProbeError("video has no valid frame rate; tried " + "; ".join(failures))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "MediaProbe",
    "MediaProbeError",
    "MediaProbeResult",
    "SourceProbeRegistry",
    "probe_media",
    "probe_media_metadata",
]
