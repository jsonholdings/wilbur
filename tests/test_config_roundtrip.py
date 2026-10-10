"""Round-trip tests for wilbur.config.Config: defaults and save/load.

The config location is pointed at tmp_path with monkeypatch (config.py builds
CONFIG_PATH from Path.home() at import time, so the module attribute is what
save()/load() read), so the owner's real ~/.config/wilbur is never touched.
"""
import json

import wilbur.config as config_mod
from wilbur.config import Config


def test_default_base_url():
    assert Config().base_url == "http://localhost:11434"


def test_save_load_roundtrip(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)

    cfg = Config()
    cfg.model = "test-model:1b"
    cfg.temperature = 0.42
    cfg.save()

    # save() wrote to the tmp location, not the real home dir.
    assert config_path.exists()
    on_disk = json.loads(config_path.read_text())
    assert on_disk["model"] == "test-model:1b"

    loaded = Config.load()
    assert loaded.model == "test-model:1b"
    assert loaded.temperature == 0.42
    # Untouched fields keep their defaults through the round trip.
    assert loaded.base_url == "http://localhost:11434"
