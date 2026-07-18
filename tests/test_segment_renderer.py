from video_vault.render_types import MediaProbeResult, RenderProfile, RenderSegment, RenderSettings
from video_vault.segment_renderer import build_segment_command


def test_command_uses_exact_trim_speed_and_normalized_audio():
    segment = RenderSegment("s1", "clip.mp4", 1500, 5500, speed=2.0)
    probe = MediaProbeResult("clip.mp4", 10000, 1280, 720, has_audio=True, audio_track_count=1)
    command = build_segment_command(segment, RenderSettings(), RenderProfile(), output="out.mp4", probe=probe, ffmpeg_path="ffmpeg", encoder="libx264")
    joined = " ".join(command)
    assert "trim=start=1.500000:end=5.500000" in joined
    assert "setpts=PTS-STARTPTS" in joined
    assert "atrim=start=1.500000:end=5.500000" in joined
    assert "atempo=2" in joined
    assert "aresample=48000" in joined
    assert "-pix_fmt yuv420p" in joined


def test_no_audio_uses_stereo_silence():
    segment = RenderSegment("s1", "clip.mp4", 0, 2000)
    probe = MediaProbeResult("clip.mp4", 2000, 1920, 1080, has_audio=False)
    command = build_segment_command(segment, RenderSettings(), RenderProfile(), output="out.mp4", probe=probe)
    joined = " ".join(command)
    assert "anullsrc=r=48000:cl=stereo:d=2.000000" in joined
    assert "-ac 2" in joined


def test_lower_original_audio_role_is_encoded_in_filter():
    segment = RenderSegment("s1", "clip.mp4", 0, 2000, audio_role="lower_original")
    probe = MediaProbeResult("clip.mp4", 2000, 1920, 1080, has_audio=True, audio_track_count=1)
    command = build_segment_command(segment, RenderSettings(), RenderProfile(), output="out.mp4", probe=probe)
    assert "volume=0.35" in " ".join(command)
