"""v1.10.1: the first-run wizard starts from the saved settings and Finish
keeps the fields it does not ask about. Also guards the installer's Start
menu "Settings" shortcut, which pointed at this wizard instead of the
settings editor since the first commit."""
import json
from pathlib import Path

import setup_wizard as sw

ROOT = Path(__file__).resolve().parent.parent


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sw, "CONFIG_FILE", tmp_path / "config.json")
    return tmp_path / "config.json"


def test_load_existing_settings_prefills_from_a_saved_config(tmp_path, monkeypatch):
    cfg = _redirect(tmp_path, monkeypatch)
    assert sw.load_existing_settings() == {}
    cfg.write_text("{not json", encoding="utf-8")
    assert sw.load_existing_settings() == {}
    cfg.write_text(json.dumps({
        "client_id": "id", "client_secret": "sec",
        "streamers": ["alice", " ", 3, "bob "], "check_interval": 30,
    }), encoding="utf-8")
    assert sw.load_existing_settings() == {
        "streamers": ["alice", "bob"], "client_id": "id", "client_secret": "sec",
    }


def test_load_existing_settings_tolerates_odd_types(tmp_path, monkeypatch):
    cfg = _redirect(tmp_path, monkeypatch)
    cfg.write_text(json.dumps({"client_id": 5, "streamers": "alice"}), encoding="utf-8")
    assert sw.load_existing_settings() == {"streamers": [], "client_id": "", "client_secret": ""}
    cfg.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert sw.load_existing_settings() == {}


def test_write_config_keeps_fields_the_wizard_does_not_ask_about(tmp_path, monkeypatch):
    cfg = _redirect(tmp_path, monkeypatch)
    cfg.write_text(json.dumps({
        "client_id": "old", "client_secret": "old", "streamers": ["alice"],
        "check_interval": 30, "pinned_streamers": ["alice"], "own_channel": "me",
        "install_id": "abc", "usage_ping": False,
    }), encoding="utf-8")
    sw.write_config(["alice", "bob"], "new", "newsec")
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["streamers"] == ["alice", "bob"]
    assert saved["client_id"] == "new" and saved["client_secret"] == "newsec"
    assert saved["check_interval"] == 30  # used to be reset to 60
    assert saved["pinned_streamers"] == ["alice"]
    assert saved["own_channel"] == "me"
    assert saved["install_id"] == "abc"
    assert saved["usage_ping"] is False
    # A first run, with no config at all, gets the default interval.
    cfg.unlink()
    sw.write_config(["carol"], "id", "sec")
    assert json.loads(cfg.read_text(encoding="utf-8")) == {
        "client_id": "id", "client_secret": "sec", "streamers": ["carol"], "check_interval": 60,
    }


def test_installer_settings_shortcut_targets_the_settings_editor():
    iss = (ROOT / "installer.iss").read_text(encoding="utf-8")
    line = next(l for l in iss.splitlines() if 'Settings"; Filename:' in l)
    assert "MyAppSettingsExeName" in line, line
    assert "MyAppSetupExeName" not in line, line
