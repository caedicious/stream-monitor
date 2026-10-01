"""Every POST route refuses web pages (v1.11.2).

A web page's POST to 127.0.0.1:52832 carries its http(s) Origin (browsers
send one on every cross-site POST, and a text/plain body skips the CORS
preflight), so the desktop answers 403 on every route before it touches any
state. A page can send "null" instead (a sandboxed frame, a no-referrer
request, an https page posting to http), so every route also needs
Content-Type application/json (415 otherwise): a page can only send that
after a preflight, and a page's preflight is refused. The extension's own
requests carry no Origin, an extension origin, or "null", send JSON, and
go through. Also here: POST /streak_event answers 400 to a malformed
report and 200 to an ignored one, and GET /config recomputes saved_streaks
for each answer, so a save that ran out drops off without waiting for a
poll.
"""
import http.client
import json
import threading
import urllib.request
from http.server import HTTPServer

import pytest

import stream_monitor_tray as sm

NOW_EPOCH = sm._iso_to_epoch("2026-09-28T08:00:00.000Z")
DETECTED_AT = "2026-09-28T04:00:00.000Z"
REQUESTS = {
    "/streak_event": {
        "status": "already_saved", "streamer": "alice", "count": 4,
        "detected_at": DETECTED_AT,
        "page_url": "https://www.twitch.tv/save-streak/alice?sm=1",
    },
    "/open_tabs": {"browser": "chrome", "streamers": ["alice"], "reason": "init"},
    "/rescue_ack": {"id": "rescue-1"},
}


@pytest.fixture
def acks():
    return []


@pytest.fixture(autouse=True)
def isolated(tmp_config_dir, monkeypatch, acks):
    """No streak, tab-report, rescue or extension-contact state leaks in or
    out of these tests."""
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset({"alice"}))
    monkeypatch.setattr(sm, "_streak_clock", lambda: NOW_EPOCH)
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    monkeypatch.setattr(sm, "_tray_notifier", None)
    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", None)
    monkeypatch.setattr(sm, "_rescue_ack_handler", lambda offer_id: acks.append(offer_id) or True)
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", {})


@pytest.fixture(scope="module")
def port():
    # One server for the module: the handler reads module state on every
    # request, so each test's monkeypatching still applies, and shutdown
    # waits out the poll interval only once.
    server = HTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
    ).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _send(port, method, path, body=None, headers=None):
    """One request, as (status, headers with lowercase names). http.client
    adds no Content-Type of its own; urllib would add a form type."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status, {name.lower(): value for name, value in resp.getheaders()}
    finally:
        conn.close()


def _post(port, path, body, origin=None, content_type="application/json"):
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if origin is not None:
        headers["Origin"] = origin
    return _send(port, "POST", path, json.dumps(body).encode("utf-8"), headers)[0]


def _config(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.mark.parametrize("path", sorted(REQUESTS))
@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://127.0.0.1:52832",
    "HTTPS://EVIL.EXAMPLE",
])
def test_a_web_page_is_refused_on_every_post_route(port, acks, path, origin):
    assert _post(port, path, REQUESTS[path], origin) == 403
    assert sm._streak_state["saved"] == {}
    assert sm.extension_open_tabs_snapshot() == {}
    assert acks == []


def test_an_unknown_route_is_refused_to_a_web_page_too(port):
    assert _post(port, "/nope", {}, "https://evil.example") == 403


@pytest.mark.parametrize("origin", [
    None,
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "moz-extension://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    "null",
])
def test_the_extension_gets_through_on_every_post_route(port, acks, origin):
    assert _post(port, "/streak_event", REQUESTS["/streak_event"], origin) == 200
    assert _post(port, "/open_tabs", REQUESTS["/open_tabs"], origin) == 204
    assert _post(port, "/rescue_ack", REQUESTS["/rescue_ack"], origin) == 204
    assert sm.streak_saved_since_last_live("alice") == DETECTED_AT
    assert sm.extension_open_tabs_snapshot()["chrome"]["streamers"] == frozenset({"alice"})
    assert acks == ["rescue-1"]


@pytest.mark.parametrize("path", sorted(REQUESTS))
@pytest.mark.parametrize("origin", [None, "null"])
@pytest.mark.parametrize("content_type", [
    None,
    "text/plain",
    "text/plain;charset=UTF-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
    "text/plain; x=application/json",
])
def test_a_post_without_a_json_content_type_is_refused(port, acks, path, origin, content_type):
    """What a page can send without a preflight (a form, a no-cors fetch, a
    beacon) never declares JSON, so a page that hides its origin still
    gets nothing through."""
    assert _post(port, path, REQUESTS[path], origin, content_type) == 415
    assert sm._streak_state["saved"] == {}
    assert sm.extension_open_tabs_snapshot() == {}
    assert acks == []


def test_a_json_content_type_with_parameters_is_accepted(port):
    status = _post(port, "/streak_event", REQUESTS["/streak_event"],
                   content_type="Application/JSON; charset=utf-8")
    assert status == 200
    assert sm.streak_saved_since_last_live("alice") == DETECTED_AT


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://127.0.0.1:52832",
    "null",
    "NULL",
])
def test_a_web_page_preflight_is_refused(port, origin):
    """Refusing the preflight stops a page's JSON POST before it is sent."""
    status, headers = _send(port, "OPTIONS", "/streak_event", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert status == 403
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize("origin", [
    None,
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "moz-extension://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
])
def test_an_extension_preflight_is_answered_as_before(port, origin):
    headers = {"Access-Control-Request-Method": "POST"}
    if origin is not None:
        headers["Origin"] = origin
    status, resp_headers = _send(port, "OPTIONS", "/streak_event", headers=headers)
    assert status == 200
    assert resp_headers["access-control-allow-origin"] == "*"


def test_streak_event_answers_400_to_malformed_and_200_to_ignored(port):
    bad_login = dict(REQUESTS["/streak_event"], streamer="not a login!")
    assert _post(port, "/streak_event", bad_login) == 400
    assert _post(port, "/streak_event", dict(REQUESTS["/streak_event"], count="four")) == 400
    assert sm._streak_state["saved"] == {}

    # Well formed but older than alice's last go-live: ignored, still 200.
    sm.record_stream_live("alice", "2026-09-28T05:00:00.000Z")
    assert _post(port, "/streak_event", REQUESTS["/streak_event"]) == 200
    assert sm._streak_state["saved"] == {}


def test_config_drops_a_save_that_ran_out_without_waiting_for_a_poll(port, monkeypatch):
    unpolled = dict(REQUESTS["/streak_event"], streamer="driveyabatty")
    assert _post(port, "/streak_event", unpolled) == 200
    assert _config(port)["saved_streaks"] == {"driveyabatty": DETECTED_AT}

    # A day after the detection, and nothing ever polled that login.
    expired = sm._iso_to_epoch(DETECTED_AT) + sm.SAVED_STREAK_UNPOLLED_TTL_SECONDS + 60
    monkeypatch.setattr(sm, "_streak_clock", lambda: expired)
    assert _config(port)["saved_streaks"] == {}
