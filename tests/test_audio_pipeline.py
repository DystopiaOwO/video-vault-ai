from video_vault.audio_pipeline import atempo_chain, build_audio_filtergraph, normalize_audio_role
from video_vault.render_types import BgmSettings, RenderSegment


def test_atempo_chain_supports_extremes_and_rejects_invalid_speed():
    assert atempo_chain(0.25).count("atempo=") == 2
    assert atempo_chain(4).count("atempo=") == 2
    try:
        atempo_chain(0.1)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid speed should fail")


def test_audio_graph_has_bgm_ducking_and_master_limiter():
    segments = [RenderSegment("a", "a.mp4", 0, 1000, audio_role="dialogue")]
    graph = build_audio_filtergraph(
        segments,
        bgm=BgmSettings(enabled=True, source_file="music.mp3", fade_in_ms=100, fade_out_ms=100),
        timeline_duration_ms=1000,
    )
    assert "afade=t=in" in graph
    assert "bgm_ducked" in graph
    assert "alimiter" in graph
    assert normalize_audio_role("mute") == "mute"
