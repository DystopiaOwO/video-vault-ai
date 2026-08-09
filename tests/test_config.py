from video_vault.config import load_config, save_default_config


def test_config_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    save_default_config(str(path))
    cfg = load_config(str(path))
    assert cfg["library_root"] == "D:/VideoLibrary"
    assert cfg["ai"]["provider"] == "mock"


def test_yaml_boolean_false_is_loaded_as_false(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "perception:\n"
        "  multi_frame:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg["perception"]["multi_frame"]["enabled"] is False
