from video_vault.config import load_config, save_default_config


def test_config_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    save_default_config(str(path))
    cfg = load_config(str(path))
    assert cfg["library_root"] == "D:/VideoLibrary"
    assert cfg["ai"]["provider"] == "mock"


def test_config_uses_yaml_types_and_deep_default_merge(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """frame_interval_seconds: 0.08
enabled: true
items: [one, two]
nested:
  value: null
""",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg["frame_interval_seconds"] == 0.08
    assert cfg["frame_interval_seconds"] != "0.08"
    assert cfg["enabled"] is True
    assert cfg["items"] == ["one", "two"]
    assert cfg["nested"]["value"] is None
    assert cfg["ai"]["provider"] == "mock"
