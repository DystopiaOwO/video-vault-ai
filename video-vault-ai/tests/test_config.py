from video_vault.config import load_config, save_default_config


def test_config_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    save_default_config(str(path))
    cfg = load_config(str(path))
    assert cfg["library_root"] == "D:/VideoLibrary"
    assert cfg["ai"]["provider"] == "mock"
