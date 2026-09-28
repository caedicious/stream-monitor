"""No GET answer lets a web page on another site read it.

GET /activity.json and /debug.log.json used to answer with
Access-Control-Allow-Origin: *, so any site the user visited could read the
activity history (which streamers they watch and when) and the debug log
(paths, errors) from 127.0.0.1:52832 wherever the browser lets public pages
reach loopback, which Firefox does. Their only reader is logs.html, served by
the desktop itself, so its reads are same origin and need no CORS header.

GET /config sent two headers, "moz-extension://*" and "*", which browsers
combine into one invalid value (Firefox: "Multiple CORS header
'Access-Control-Allow-Origin' not allowed"), so no cross-origin read of it
ever worked. Both extensions read it from their background scripts with the
required http://127.0.0.1/* host permission, which CORS does not apply to.

So no GET route sends the header at all, and the same-origin reads the
desktop's own pages make still work.
"""
import http.client
import inspect
import json
import re
import threading
from http.server import HTTPServer

import pytest

import stream_monitor_tray as sm

GET_ROUTES = [
    "/config",
    "/activity.json",
    "/debug.log.json",
    "/activity.jsonl",
    "/debug.log",
    "/logs",
    "/activity",
    "/about",
]
FOREIGN_ORIGINS = [
    "https://evil.example",
    "http://evil.example",
    "http://localhost:3000",
    "null",
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "moz-extension://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
]
CONFIG = {"streamers": ["probe_streamer"], "pinned_streamers": [], "version": sm.VERSION}
ACTIVITY_EVENT = {"ts": "2026-09-28T06:42:11", "event": "stream_live", "streamer": "probe_streamer"}
DEBUG_LINE = "2026-09-28 06:42:11 [INFO] probe debug line"


@pytest.fixture(autouse=True)
def isolated(tmp_config_dir, monkeypatch):
    """Seed an activity event and a debug log line in throwaway files, and
    keep /config's side effects (extension contact, saved streaks) local."""
    activity = tmp_config_dir / "stream_activity.jsonl"
    activity.write_text(json.dumps(ACTIVITY_EVENT) + "\n", encoding="utf-8")
    monkeypatch.setattr(sm, "STREAM_ACTIVITY_FILE", activity)
    (tmp_config_dir / "stream_monitor.log").write_text(DEBUG_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(sm, "_streak_state", sm._empty_streak_state())
    monkeypatch.setattr(sm, "_extension_last_seen_monotonic", None)
    monkeypatch.setattr(sm.ConfigRequestHandler, "config_data", dict(CONFIG))


@pytest.fixture(scope="module")
def port():
    # One server for the module: the handler reads module state on every
    # request, so each test's monkeypatching still applies.
    server = HTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
    ).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _get(port, path, origin=None):
    """One GET as (status, [(lowercase header name, value)], body bytes).
    The header list keeps duplicates, which a dict would hide."""
    headers = {} if origin is None else {"Origin": origin}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, [(name.lower(), value) for name, value in resp.getheaders()], body
    finally:
        conn.close()


def _cors_headers(headers):
    return [value for name, value in headers if name.startswith("access-control-")]


@pytest.mark.parametrize("path", GET_ROUTES)
@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
def test_no_get_route_lets_another_origin_read_it(port, path, origin):
    """The answer is still served (a non-browser client, or the browser
    itself, may ask), but without any CORS header the browser withholds it
    from a page on another origin."""
    status, headers, _ = _get(port, path, origin)
    assert status == 200
    assert _cors_headers(headers) == []


def test_every_get_route_is_covered_and_none_sets_a_cors_header():
    """Guards the list above: a GET route added later must be added to
    GET_ROUTES (so the header check runs on it), and do_GET itself must not
    send any Access-Control-* header on any branch."""
    source = inspect.getsource(sm.ConfigRequestHandler.do_GET)
    routes = set(re.findall(r'self\.path == "([^"]+)"', source))
    routes |= set(re.findall(r'self\.path\.startswith\("([^"]+)"\)', source))
    assert routes, "no routes found in do_GET; the pattern above needs updating"
    assert routes <= set(GET_ROUTES), f"GET routes missing from GET_ROUTES: {sorted(routes - set(GET_ROUTES))}"
    assert not re.search(r'send_header\(\s*["\']Access-Control-', source)


def test_the_json_endpoints_answer_a_web_page_without_a_cors_header(port):
    """The two endpoints from the report, checked by name so a regression
    points straight at them."""
    for path in ("/activity.json", "/debug.log.json"):
        status, headers, body = _get(port, path, "https://evil.example")
        assert status == 200
        assert "access-control-allow-origin" not in [name for name, _ in headers]
        assert json.loads(body)  # still served; only the browser withholds it


def test_logs_page_reads_its_json_from_its_own_origin(port):
    """logs.html fetches root-relative paths, so its reads go to the origin
    that served it (http://127.0.0.1:52832) and need no CORS header."""
    status, _, body = _get(port, "/logs")
    assert status == 200
    fetched = re.findall(r"""fetch\(\s*(["'`])(.*?)\1""", body.decode("utf-8"))
    paths = {url for _, url in fetched}
    assert paths == {"/activity.json", "/debug.log.json"}
    assert all(p.startswith("/") and not p.startswith("//") for p in paths)


@pytest.mark.parametrize("same_origin", [None, "http://127.0.0.1:{port}"])
def test_same_origin_reads_of_the_json_endpoints_still_work(port, same_origin):
    """A same-origin fetch sends no Origin on GET in current browsers; an
    explicit same-origin Origin must not change anything either."""
    origin = None if same_origin is None else same_origin.format(port=port)

    status, headers, body = _get(port, "/activity.json", origin)
    assert status == 200
    assert ("content-type", "application/json") in headers
    events = json.loads(body)
    assert any(e.get("event") == "stream_live" and e.get("streamer") == "probe_streamer" for e in events)

    status, headers, body = _get(port, "/debug.log.json", origin)
    assert status == 200
    assert ("content-type", "application/json") in headers
    entries = json.loads(body)
    assert any(e.get("msg") == "probe debug line" and e.get("level") == "INFO" for e in entries)


@pytest.mark.parametrize("origin", [
    None,
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "moz-extension://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
])
def test_config_still_answers_the_extensions(port, origin):
    """The background scripts read /config with the host permission, so the
    missing CORS header does not affect them: the answer is the same JSON."""
    status, headers, body = _get(port, "/config", origin)
    assert status == 200
    assert ("content-type", "application/json") in headers
    data = json.loads(body)
    assert data["streamers"] == CONFIG["streamers"]
    assert data["version"] == sm.VERSION
    assert _cors_headers(headers) == []
