import pytest

from video_vault.segment_renderer import atempo_chain


@pytest.mark.parametrize("speed", [0.25, 0.5, 1.0, 1.25, 2.0, 4.0])
def test_atempo_chain_supports_declared_speed_range(speed):
    chain = atempo_chain(speed)
    assert chain.startswith("atempo=")
    assert all(part.startswith("atempo=") for part in chain.split(","))


def test_atempo_chain_rejects_out_of_range():
    with pytest.raises(ValueError):
        atempo_chain(0.2)
    with pytest.raises(ValueError):
        atempo_chain(4.1)
