from pathlib import Path

from video_vault.render_types import ColorSettings, RenderProfile, RenderSegment, RenderSettings
from video_vault.segment_cache import SegmentCache


def _inputs(source: Path):
    segment = RenderSegment("s1", str(source), 1000, 3000)
    settings = RenderSettings(color=ColorSettings(mode="none"))
    return segment, settings, RenderProfile()


def test_cache_key_changes_when_source_changes(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"one")
    segment, settings, profile = _inputs(source)
    cache = SegmentCache(tmp_path / "cache")
    first = cache.key(segment, settings, profile, encoder="libx264")
    source.write_bytes(b"two-and-more")
    second = cache.key(segment, settings, profile, encoder="libx264")
    assert first != second


def test_corrupt_metadata_is_cache_miss(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    segment, settings, profile = _inputs(source)
    cache = SegmentCache(tmp_path / "cache")
    entry = cache.entry(cache.key(segment, settings, profile, encoder="libx264"))
    entry.media_path.parent.mkdir()
    entry.media_path.write_bytes(b"media")
    entry.metadata_path.write_text("not json", encoding="utf-8")
    assert cache.get(segment, settings, profile, encoder="libx264") is None
