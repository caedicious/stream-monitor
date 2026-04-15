"""Tests for the Config dataclass."""
import json

import stream_monitor_tray as sm


def test_config_defaults():
    c = sm.Config()
    assert c.client_id == ""
    assert c.client_secret == ""
    assert c.streamers == []
    assert c.check_interval == 60
    assert c.paused is False
    assert c.auto_paused is False if hasattr(c, "auto_paused") else True
    assert c.im_live_pause is False
    assert c.vod_fallback is False


def test_is_valid_requires_all_fields():
    assert not sm.Config().is_valid()
    assert not sm.Config(client_id="x", client_secret="y").is_valid()  # no streamers
    assert not sm.Config(streamers=["a"]).is_valid()  # no creds
    assert sm.Config(client_id="x", client_secret="y", streamers=["a"]).is_valid()


def test_save_and_load_roundtrip(tmp_config_dir):
    original = sm.Config(
        client_id="cid",
        client_secret="csec",
        streamers=["alice", "bob"],
        check_interval=45,
        own_channel="me",
        im_live_pause=True,
        vod_fallback=True,
    )
    original.save()

    loaded = sm.Config.load()
    assert loaded.client_id == "cid"
    assert loaded.client_secret == "csec"
    assert loaded.streamers == ["alice", "bob"]
    assert loaded.check_interval == 45
    assert loaded.own_channel == "me"
    assert loaded.im_live_pause is True
    assert loaded.vod_fallback is True


def test_load_missing_file_returns_defaults(tmp_config_dir):
    # tmp_config_dir is empty, so load should fall back to defaults
    loaded = sm.Config.load()
    assert loaded.client_id == ""
    assert loaded.streamers == []


def test_load_filters_unknown_keys(tmp_config_dir):
    # Config files from older/newer versions may contain extra fields.
    # Config.load should silently drop them instead of raising TypeError.
    sm.CONFIG_FILE.write_text(json.dumps({
        "client_id": "cid",
        "client_secret": "csec",
        "streamers": ["x"],
        "some_removed_field": "hello",
        "future_feature_flag": True,
    }))
    loaded = sm.Config.load()
    assert loaded.client_id == "cid"
    assert loaded.streamers == ["x"]
    assert not hasattr(loaded, "some_removed_field")


def test_load_invalid_json_returns_defaults(tmp_config_dir):
    sm.CONFIG_FILE.write_text("{ not valid json")
    loaded = sm.Config.load()
    # Should not raise; returns defaults
    assert loaded.client_id == ""
