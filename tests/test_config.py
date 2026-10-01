"""Tests for the Config dataclass."""
import json

import pytest

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


# ---------------------------------------------------------------------------
# Slot mode fields (1.12.0, plan 3.1)
# ---------------------------------------------------------------------------

SLOT_DEFAULTS = {"slot_mode": False, "keep_open_slots": 2, "cycle_slots": 1,
                 "slot_minutes": 30, "auto_save_streaks": False}


def _slot_fields(config):
    return {name: getattr(config, name) for name in SLOT_DEFAULTS}


def test_r01_o10_new_fields_default_off():
    config = sm.Config()
    assert _slot_fields(config) == SLOT_DEFAULTS
    assert all(name in sm.asdict(config) for name in SLOT_DEFAULTS)


@pytest.mark.parametrize("given, expected", [
    ({"cycle_slots": 0}, {"cycle_slots": 1, "keep_open_slots": 2}),
    ({"keep_open_slots": 3, "cycle_slots": 1}, {"keep_open_slots": 2, "cycle_slots": 1}),
    ({"keep_open_slots": 2, "cycle_slots": 2}, {"keep_open_slots": 1, "cycle_slots": 2}),
    ({"keep_open_slots": 1, "cycle_slots": 3}, {"keep_open_slots": 0, "cycle_slots": 3}),
    ({"keep_open_slots": -1, "cycle_slots": 4}, {"keep_open_slots": 0, "cycle_slots": 3}),
    ({"keep_open_slots": 0, "cycle_slots": 3}, {"keep_open_slots": 0, "cycle_slots": 3}),
    ({"slot_minutes": 4}, {"slot_minutes": 5}),
    ({"slot_minutes": 121}, {"slot_minutes": 120}),
    ({"slot_minutes": 5}, {"slot_minutes": 5}),
    ({"slot_minutes": 120}, {"slot_minutes": 120}),
    ({"keep_open_slots": "1", "cycle_slots": "2", "slot_minutes": "45"},
     {"keep_open_slots": 1, "cycle_slots": 2, "slot_minutes": 45}),
    ({"slot_minutes": 12.9}, {"slot_minutes": 12}),
    ({"keep_open_slots": "two", "cycle_slots": None, "slot_minutes": [30]},
     {"keep_open_slots": 2, "cycle_slots": 1, "slot_minutes": 30}),
    ({"keep_open_slots": True, "cycle_slots": False, "slot_minutes": True},
     {"keep_open_slots": 2, "cycle_slots": 1, "slot_minutes": 30}),
    ({"slot_minutes": float("inf")}, {"slot_minutes": 30}),
    ({"slot_mode": "yes", "auto_save_streaks": 1}, {"slot_mode": False, "auto_save_streaks": False}),
    ({"slot_mode": True, "auto_save_streaks": True}, {"slot_mode": True, "auto_save_streaks": True}),
])
def test_r03_c01_clamps(given, expected):
    config = sm.Config(**given)
    for name, value in expected.items():
        assert getattr(config, name) == value, name
        assert type(getattr(config, name)) is type(value), name
    # The effective ranges always hold (plan 3.1).
    assert 0 <= config.keep_open_slots <= 2
    assert 1 <= config.cycle_slots <= 3
    assert config.keep_open_slots + config.cycle_slots <= 3
    assert 5 <= config.slot_minutes <= 120


def test_c01_old_config_loads_with_defaults(tmp_config_dir):
    # A config.json written by 1.11 has none of the new keys.
    sm.CONFIG_FILE.write_text(json.dumps({
        "client_id": "cid", "client_secret": "csec", "streamers": ["x"],
        "pinned_streamers": ["x"], "check_interval": 60, "vod_fallback": True,
    }))
    loaded = sm.Config.load()
    assert loaded.streamers == ["x"] and loaded.vod_fallback is True
    assert _slot_fields(loaded) == SLOT_DEFAULTS

    # Saved and loaded again, the fields round-trip; values out of range
    # in the file are clamped on load.
    loaded.slot_mode = True
    loaded.save()
    assert sm.Config.load().slot_mode is True
    data = json.loads(sm.CONFIG_FILE.read_text())
    data.update({"keep_open_slots": 2, "cycle_slots": 3, "slot_minutes": 500})
    sm.CONFIG_FILE.write_text(json.dumps(data))
    clamped = sm.Config.load()
    assert (clamped.keep_open_slots, clamped.cycle_slots, clamped.slot_minutes) == (0, 3, 120)
