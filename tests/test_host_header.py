"""Every request whose Host is not this server's own address is refused.

DNS rebinding: a page at http://x.rebind.example:52832 whose name first
resolves to the attacker's server and then to 127.0.0.1 is same origin with
its own requests. The browser delivers them to the desktop with
"Host: x.rebind.example:52832" and lets the page read every answer without
any CORS header (the watch list, the activity history, the debug log), and
its JSON POSTs need no preflight: with mode "same-origin" and referrerPolicy
"no-referrer" Firefox sends "Origin: null", which the POST guard lets through
because the extensions may send it too. The page cannot change or remove the
Host header, so the handler refuses any request whose Host is not
127.0.0.1, localhost or [::1] with the bound port, before any method runs.
A request with no Host header at all is allowed: no browser sends one, and
any other local program could send an allowed Host anyway. Refusals are
logged within a budget (each kind once per hour, capped) so a page cannot
flood the log or use the budget up for good.
"""
import http.client
import json
import logging
import socket
import threading
import urllib.request
from http.server import HTTPServer

import pytest

import stream_monitor_tray as sm
from tests.test_get_cors import GET_ROUTES
from tests.test_post_origin import DETECTED_AT, NOW_EPOCH, REQUESTS

EXTENSION_ORIGIN = "moz-extension://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
# Host values a rebound page (or anything that is not this app) could arrive
# with. "{port}" is the test server's port, "{other}" a different one.
FOREIGN_HOSTS = [
    "x.rebind.example:{port}",
    "X.REBIND.EXAMPLE:{port}",
    "x.rebind.example",
    "127.0.0.1.nip.io:{port}",
    "localhost.:{port}",
    "127.0.0.1:{other}",
    "localhost:{other}",
    "127.0.0.1",
    "localhost",
    "",
    "localhost:{port}, x.rebind.example:{port}",
]
ALLOWED_HOSTS = [
    "127.0.0.1:{port}",
    "localhost:{port}",
    "LocalHost:{port}",
    "[::1]:{port}",
]


@pytest.fixture
def acks():
    return []


@pytest.fixture(autouse=True)
def isolated(tmp_config_dir, monkeypatch, acks):
    """Seed owner data a refused request must not reach, and keep every
    piece of state the routes touch local to the test."""
    activity = tmp_config_dir / "stream_activity.jsonl"
    activity.write_text(json.dumps({"event": "stream_live", "streamer": "alice"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sm, "STREAM_ACTIVITY_FILE", activity)
    (tmp_config_dir / "stream_monitor.log").write_text(
        "2026-09-28 06:42:11 [INFO] probe debug line\n", encoding="utf-8")
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_streak_event_seen", set())
    monkeypatch.setattr(sm, "_polled_streamers", frozenset({"alice"}))
    monkeypatch.setattr(sm, "_streak_clock", lambda: NOW_EPOCH)
    monkeypatch.setattr(sm, "_open_tabs_reports", {})
    monkeypatch.setattr(sm, "_tray_notifier", None)
    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", None)
    monkeypatch.setattr(sm, "_rescue_ack_handler", lambda offer_id: acks.append(offer_id) or True)
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", {"streamers": ["alice"], "version": sm.VERSION})
    monkeypatch.setattr(sm, "_refusal_log", sm._new_refusal_window(float("-inf")))


@pytest.fixture(scope="module")
def port():
    server = HTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
    ).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _host(template, port):
    return template.format(port=port, other=port + 1 if port < 65535 else port - 1)


def _send(port, method, path, host, body=None, headers=None):
    """One request to 127.0.0.1:<port> carrying exactly the given Host (the
    TCP connection is the same whatever Host says, as with rebinding).
    Returns (status, {lowercase header: value}, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {n.lower(): v for n, v in resp.getheaders()}, data
    finally:
        conn.close()


def _post(port, path, host, origin="null"):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    body = json.dumps(REQUESTS[path]).encode("utf-8")
    return _send(port, "POST", path, host, body, headers)[0]


def _no_state_changed(acks):
    assert sm._streak_state["saved"] == {}
    assert sm.extension_open_tabs_snapshot() == {}
    assert acks == []
    assert sm._extension_last_seen_monotonic is None


@pytest.mark.parametrize("path", GET_ROUTES)
@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_a_foreign_host_is_refused_on_every_get_route(port, acks, path, host):
    status, headers, body = _send(port, "GET", path, _host(host, port))
    assert status == 403
    assert body == b""
    assert not any(name.startswith("access-control-") for name in headers)
    _no_state_changed(acks)  # /config would have noted extension contact


@pytest.mark.parametrize("path", sorted(REQUESTS))
@pytest.mark.parametrize("origin", ["null", None, "http://x.rebind.example:52832"])
@pytest.mark.parametrize("host", ["x.rebind.example:{port}", "localhost.:{port}", "127.0.0.1:{other}"])
def test_a_rebound_post_is_refused_with_no_state_change(port, acks, path, origin, host):
    """The same-origin, no-referrer JSON POST a rebound page can send."""
    assert _post(port, path, _host(host, port), origin) == 403
    _no_state_changed(acks)


@pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE", "PATCH"])
def test_other_methods_are_refused_for_a_foreign_host(port, acks, method):
    """Refused before routing, so an unsupported method gets 403, not 501."""
    headers = {"Origin": EXTENSION_ORIGIN, "Access-Control-Request-Method": "POST"}
    status, resp_headers, _ = _send(port, method, "/streak_event", _host("x.rebind.example:{port}", port),
                                    headers=headers)
    assert status == 403
    assert "access-control-allow-origin" not in resp_headers
    _no_state_changed(acks)


@pytest.mark.parametrize("host", ALLOWED_HOSTS)
def test_this_apps_own_addresses_work_on_every_route(port, acks, host):
    host = _host(host, port)
    for path in GET_ROUTES:
        status, _, _ = _send(port, "GET", path, host)
        assert status == 200, path
    for path in sorted(REQUESTS):
        assert _post(port, path, host, origin="null") == 204, path
    assert sm.streak_saved_since_last_live("alice") == DETECTED_AT
    assert sm.extension_open_tabs_snapshot()["chrome"]["streamers"] == frozenset({"alice"})
    assert acks == ["rescue-1"]
    status, headers, _ = _send(port, "OPTIONS", "/streak_event", host,
                               headers={"Origin": EXTENSION_ORIGIN, "Access-Control-Request-Method": "POST"})
    assert status == 200
    assert headers["access-control-allow-origin"] == "*"


def test_the_json_endpoints_still_serve_the_logs_page(port):
    """logs.html fetches relative paths, so they carry the Host it was
    opened with: 127.0.0.1:52832 from the tray menu, or localhost."""
    for host in ("127.0.0.1:{port}", "localhost:{port}"):
        status, _, body = _send(port, "GET", "/activity.json", _host(host, port))
        assert status == 200
        assert any(e.get("streamer") == "alice" for e in json.loads(body))
        status, _, body = _send(port, "GET", "/debug.log.json", _host(host, port))
        assert status == 200
        assert any(e.get("msg") == "probe debug line" for e in json.loads(body))


def test_standard_clients_send_an_allowed_host(port):
    """urllib and http.client, like the extensions' fetch and XHR, the
    updater's curl and the tray's links, derive Host from the URL
    http://127.0.0.1:<port>, which is allowed."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["streamers"] == ["alice"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/activity.json")
        assert conn.getresponse().status == 200
    finally:
        conn.close()


def test_a_request_without_a_host_header_is_allowed(port):
    """HTTP/1.0 lets a client omit Host. No browser does, so this cannot be
    a rebinding page, and any other local program could send an allowed
    Host anyway, so refusing it would stop nothing."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /config HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.split(b"\r\n", 1)[0].split()[1] == b"200"
    assert json.loads(body)["streamers"] == ["alice"]


def test_two_host_headers_are_refused(port, acks):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", "/config", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Host", f"x.rebind.example:{port}")
        conn.endheaders()
        assert conn.getresponse().status == 403
    finally:
        conn.close()
    _no_state_changed(acks)


def _refusal_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "Refused" in r.getMessage()]


def test_each_refused_host_is_logged_once(port, caplog):
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    for _ in range(3):
        _send(port, "GET", "/config", _host("x.rebind.example:{port}", port))
    _send(port, "GET", "/activity.json", _host("y.rebind.example:{port}", port))
    warnings = _refusal_warnings(caplog)
    assert len(warnings) == 2
    assert "x.rebind.example" in warnings[0].getMessage()
    assert "y.rebind.example" in warnings[1].getMessage()


def test_refused_host_logging_is_capped(port, caplog, monkeypatch):
    """A page cycling through hostnames cannot flood the log."""
    monkeypatch.setattr(sm, "REFUSAL_LOG_LIMIT", 3)
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    for i in range(6):
        _send(port, "GET", "/config", _host(f"r{i}.rebind.example:{{port}}", port))
    warnings = _refusal_warnings(caplog)
    assert len(warnings) == 4  # three hosts, then one line saying the rest are counted
    assert "counting the rest instead of logging them" in warnings[-1].getMessage()
    assert len(sm._refusal_log["keys"]) == 3
    assert sm._refusal_log["suppressed"] == 3


def test_the_budget_starts_over_each_hour(port, caplog, monkeypatch):
    """One burst cannot hide later refusals for the life of the process: in
    the next window the count of what was not logged is reported, and new
    hosts are named again."""
    clock = [1000.0]
    monkeypatch.setattr(sm, "_refusal_clock", lambda: clock[0])
    monkeypatch.setattr(sm, "REFUSAL_LOG_LIMIT", 3)
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    for i in range(5):
        _send(port, "GET", "/config", _host(f"burst{i}.example:{{port}}", port))
    clock[0] += sm.REFUSAL_LOG_WINDOW_SECONDS
    _send(port, "GET", "/config", _host("x.rebind.example:{port}", port))
    messages = [r.getMessage() for r in _refusal_warnings(caplog)]
    assert messages[-2] == "Refused 2 more request(s) in the previous hour without logging each one"
    assert "x.rebind.example" in messages[-1]


def test_post_and_preflight_refusals_share_the_budget(port, caplog):
    """The Origin and Content-Type refusals a page can trigger from any site
    are logged once per kind too, not once per request."""
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    body = json.dumps(REQUESTS["/streak_event"]).encode("utf-8")
    here = _host("127.0.0.1:{port}", port)
    for _ in range(20):
        assert _send(port, "POST", "/streak_event", here, body,
                     {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
        assert _send(port, "POST", "/streak_event", here, body, {"Content-Type": "text/plain"})[0] == 415
        assert _send(port, "OPTIONS", "/streak_event", here,
                     headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})[0] == 403
    messages = [r.getMessage() for r in _refusal_warnings(caplog)]
    assert len(messages) == 3
    assert any("from a web page (Origin https://evil.example)" in m and m.startswith("Refused POST") for m in messages)
    assert any("is not application/json" in m for m in messages)
    assert any(m.startswith("Refused a preflight") for m in messages)


def test_refusal_log_values_are_bounded(port, caplog):
    """Method, path and Host are the sender's: logged and stored truncated."""
    caplog.set_level(logging.WARNING, logger="StreamMonitor")
    huge_host = "h" * 5000 + ".example"
    status, _, _ = _send(port, "X" * 3000, "/" + "p" * 3000, huge_host)
    assert status == 403
    (message,) = [r.getMessage() for r in _refusal_warnings(caplog)]
    assert len(message) < 400
    assert all(len(key) < 120 for key in sm._refusal_log["keys"])
