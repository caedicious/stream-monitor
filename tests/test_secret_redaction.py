"""The Twitch client secret and tokens never reach the logs the desktop serves.

The app token request used to carry client_id and client_secret in the URL
query, and requests puts the request URL in HTTPError and ConnectionError
text, so a failed request (a bad secret, or a start before the network was
up) wrote the secret into stream_monitor.log, which the desktop serves at
/debug.log and /debug.log.json and users attach to issues. Now:
- all three token requests (the monitor, the tray's Test Connection, the
  setup wizard's) send the credentials in the form body;
- a token failure is logged as its exception type and HTTP status only;
- a filter on the app's log handler redacts client_secret, access_token and
  refresh_token values (URL, form, key: value, JSON and repr shapes), bearer
  and OAuth tokens, and the live secret and token by value wherever they
  appear; ordinary words such as "Bearer of the Curse" are kept;
- parse_debug_log and the /debug.log download redact lines written before;
- existing stream_monitor.log* files are scrubbed once at startup, line by
  line, flushed to disk before the replace;
- activity events are redacted before serializing, so lines stay valid JSON;
- the tray tooltip, notifications and the Test Connection label are redacted
  too (the owner streams, so the screen is public);
- printing a Config never shows the client secret.
"""
import http.client
import io
import json
import logging
import os
import re
import threading
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import setup_wizard as sw
import stream_monitor_tray as sm


@pytest.fixture(autouse=True)
def no_registered_secrets(monkeypatch):
    """Each test starts with no live secret values registered."""
    monkeypatch.setattr(sm, "_secret_values", [])

ROOT = Path(__file__).resolve().parent.parent
SECRET = "s3cretValue0123456789abcdefghij"
TOKEN = "tok0123456789abcdefghijklmnopqr"
OLD_URL = (f"https://id.twitch.tv/oauth2/token?client_id=abc123&client_secret={SECRET}"
           f"&grant_type=client_credentials")
OLD_LINE = f"2026-09-01 07:00:00 [ERROR] OAuth token request failed: 400 Client Error: Bad Request for url: {OLD_URL}"


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"access_token": TOKEN}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Bad Request for url: {OLD_URL}",
                                     response=self)

    def json(self):
        return self._body


class _RecordingSession:
    """Stands in for requests.Session: records the post and answers with the
    given response, or raises the given exception."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _assert_no_secret(text):
    assert SECRET not in text
    assert TOKEN not in text


# ---------------------------------------------------------------------------
# The token requests
# ---------------------------------------------------------------------------

def test_the_monitor_sends_the_credentials_in_the_body(monitor):
    monitor.config.client_secret = SECRET
    session = _RecordingSession(_FakeResponse())
    monitor._http = session
    assert monitor._get_oauth_token() is True
    (url, kwargs), = session.calls
    assert url == sm.TwitchMonitor.TOKEN_URL
    assert "?" not in url
    assert "params" not in kwargs
    assert kwargs["data"] == {"client_id": "test_client", "client_secret": SECRET,
                              "grant_type": "client_credentials"}


@pytest.mark.parametrize("error", [
    "http",
    requests.ConnectionError(f"HTTPSConnectionPool(host='id.twitch.tv', port=443): Max retries exceeded "
                             f"with url: /oauth2/token?client_id=abc123&client_secret={SECRET} "
                             f"(Caused by NameResolutionError)"),
    requests.Timeout(f"Read timed out. (url: {OLD_URL})"),
])
def test_a_failed_token_request_logs_only_the_type_and_status(monitor, caplog, monkeypatch, error):
    """Even if an exception's text still carried the secret (as it did when
    the credentials were in the URL), neither the log nor the tray status
    gets that text."""
    monitor.config.client_secret = SECRET
    result = _FakeResponse(status_code=400) if error == "http" else error
    monitor._http = _RecordingSession(result)
    # Switch the safety net off (the filter looks redact_secrets up by
    # name), so this checks what _get_oauth_token itself logs.
    monkeypatch.setattr(sm, "redact_secrets", lambda text: text)
    caplog.set_level(logging.DEBUG, logger="StreamMonitor")
    assert monitor._get_oauth_token() is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    _assert_no_secret(logged)
    _assert_no_secret("\n".join(monitor._status_calls))
    failure = [r.getMessage() for r in caplog.records if "OAuth token request failed" in r.getMessage()]
    if error == "http":
        assert failure == ["OAuth token request failed: HTTPError (HTTP 400)"]
        assert monitor._status_calls[-1] == "Auth error: HTTPError (HTTP 400)"
    else:
        assert failure == [f"OAuth token request failed: {type(error).__name__}"]


def test_the_setup_wizard_sends_the_credentials_in_the_body(monkeypatch):
    calls = []
    monkeypatch.setattr(sw.requests, "post", lambda url, **kw: calls.append((url, kw)) or _FakeResponse())
    label = SimpleNamespace(config=lambda **kw: None)
    fake = SimpleNamespace(
        client_id_entry=SimpleNamespace(get=lambda: "abc123"),
        client_secret_entry=SimpleNamespace(get=lambda: SECRET),
        test_result_label=label,
        root=SimpleNamespace(update=lambda: None),
        client_id="", client_secret="",
    )
    sw.SetupWizard.test_credentials(fake)
    (url, kwargs), = calls
    assert "?" not in url and SECRET not in url
    assert "params" not in kwargs
    assert kwargs["data"]["client_secret"] == SECRET


def test_no_token_request_puts_credentials_in_the_url():
    """Covers all three call sites, including the tray's Test Connection
    closure, which is built inside a Tk dialog: every post to the token
    endpoint passes a data= body and no params=."""
    sites = 0
    for name in ("stream_monitor_tray.py", "setup_wizard.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        for m in re.finditer(r'\.post\(\s*(self\.TOKEN_URL|"https://id\.twitch\.tv/oauth2/token")', source):
            call = source[m.start():source.index("timeout=", m.start())]
            assert "data=" in call and "params=" not in call, f"{name}: {call!r}"
            sites += 1
    assert sites == 3
    # The tray dialog's error label shows the type and status only.
    tray = (ROOT / "stream_monitor_tray.py").read_text(encoding="utf-8")
    assert 'text=f"Connection error: {_token_failure_summary(e)}"' in tray
    assert 'text=f"Connection error: {e}"' not in tray


def test_the_monitor_registers_the_secret_and_the_token(monitor):
    monitor.config.client_secret = SECRET
    monitor._http = _RecordingSession(_FakeResponse())
    assert monitor._get_oauth_token() is True
    assert sm.redact_secrets(f"token {TOKEN} for secret {SECRET}") == "token REDACTED for secret REDACTED"


# ---------------------------------------------------------------------------
# The log filter
# ---------------------------------------------------------------------------

def _format_with_filter(emit):
    logger = logging.getLogger("test.secret_redaction")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(sm.SecretRedactingFilter())
    logger.addHandler(handler)
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_the_filter_redacts_every_kind_of_secret():
    def emit(logger):
        logger.error("OAuth token request failed: %s", f"400 Client Error for url: {OLD_URL}")
        logger.info("refresh with access_token=%s&refresh_token=%s", TOKEN, TOKEN)
        logger.info("headers: %s", {"Client-ID": "abc123", "Authorization": f"Bearer {TOKEN}"})
        logger.info('body: {"access_token": "%s", "expires_in": 5000}', TOKEN)
        try:
            raise requests.HTTPError(f"Bad Request for url: {OLD_URL}")
        except requests.HTTPError:
            logger.exception("unexpected failure")
    out = _format_with_filter(emit)
    _assert_no_secret(out)
    assert "client_secret=REDACTED" in out
    assert "access_token=REDACTED" in out and "refresh_token=REDACTED" in out
    assert "Bearer REDACTED" in out
    assert '"access_token": "REDACTED"' in out
    assert "client_id=abc123" in out and "'Client-ID': 'abc123'" in out  # not secret, kept
    assert "Traceback" in out  # the traceback is kept, only redacted


def test_the_apps_log_handler_has_the_filter():
    assert any(isinstance(f, sm.SecretRedactingFilter)
               for h in sm.log.handlers for f in h.filters)


@pytest.mark.parametrize("text", [
    f"client_secret: {SECRET}",
    f"client_secret='{SECRET}'",
    f'client_secret = "{SECRET}"',
    f"Config(client_id='abc123', client_secret='{SECRET}')",
    f"client_secret%3D{SECRET}%26grant_type%3Dclient_credentials",
    '{\\"access_token\\": \\"' + TOKEN + '\\"}',
    f"Authorization: OAuth {TOKEN}",
])
def test_other_shapes_of_a_secret_are_redacted(text):
    out = sm.redact_secrets(text)
    _assert_no_secret(out)
    assert "REDACTED" in out


@pytest.mark.parametrize("text", [
    "Bearer of the Curse run, Dark Souls 2",
    "Ring Bearer marathon",
    "the Flag Bearer returns",
    "OAuth token request failed: HTTPError (HTTP 400)",
    "OAuth response OK but no access_token in body",
])
def test_ordinary_words_are_kept(text):
    assert sm.redact_secrets(text) == text


def test_live_secret_values_are_redacted_wherever_they_appear():
    sm.register_secret(SECRET)
    sm.register_secret("short")  # too short to register: would match ordinary text
    assert sm.redact_secrets(f"retrying with {SECRET} now") == "retrying with REDACTED now"
    assert sm.redact_secrets(f"url encoded: {SECRET}%3D") == "url encoded: REDACTED%3D"
    assert sm.redact_secrets("a short word") == "a short word"
    for i in range(20):
        sm.register_secret(f"{i:02d}" + "x" * 20)
    assert len(sm._secret_values) == sm._SECRET_VALUES_MAX


def test_printing_a_config_never_shows_the_secret():
    config = sm.Config(client_id="abc123", client_secret=SECRET, streamers=["alice"])
    assert SECRET not in repr(config) and SECRET not in str(config)
    assert sm.asdict(config)["client_secret"] == SECRET  # still saved to config.json


def test_on_screen_outputs_are_redacted():
    notes = []
    fake = SimpleNamespace(status=None, icon=SimpleNamespace(title=None, notify=lambda m, t: notes.append((t, m))))
    sm.StreamMonitorApp.update_status(fake, f"Auth error: {OLD_URL}")
    _assert_no_secret(fake.status)
    _assert_no_secret(fake.icon.title)
    sm.StreamMonitorApp.send_notification(fake, f"Bearer {TOKEN}", f"failed: {OLD_URL}")
    (title, message), = notes
    _assert_no_secret(title + message)


def test_redaction_is_idempotent_and_leaves_plain_lines_alone():
    once = sm.redact_secrets(OLD_URL)
    assert sm.redact_secrets(once) == once
    plain = "2026-09-28 06:42:11 [INFO] Monitoring 22 streamer(s): ['popacollaa', 'leaflit']"
    assert sm.redact_secrets(plain) == plain


def test_activity_events_are_redacted_too(tmp_config_dir, monkeypatch):
    activity = tmp_config_dir / "stream_activity.jsonl"
    monkeypatch.setattr(sm, "STREAM_ACTIVITY_FILE", activity)
    sm.log_activity("api_error", error=f"400 Client Error: Bad Request for url: {OLD_URL}")
    text = activity.read_text(encoding="utf-8")
    _assert_no_secret(text)
    assert json.loads(text)["error"].endswith("client_secret=REDACTED&grant_type=client_credentials")


def test_an_activity_line_stays_valid_json(tmp_config_dir, monkeypatch):
    """Redacting the serialized line could eat the backslash of an escaped
    quote and leave a line /activity.json silently drops; values are now
    redacted before serializing, including nested ones."""
    activity = tmp_config_dir / "stream_activity.jsonl"
    monkeypatch.setattr(sm, "STREAM_ACTIVITY_FILE", activity)
    sm.log_activity(
        "api_error",
        error=f'GET "https://example.test/x?access_token={TOKEN}" failed',
        detail={"access_token": TOKEN, "nested": [{"refresh_token": TOKEN, "note": "kept"}]},
        title="Bearer of the Curse run",
    )
    (event,) = sm.read_activity_log()
    _assert_no_secret(json.dumps(event))
    assert event["error"] == 'GET "https://example.test/x?access_token=REDACTED" failed'
    assert event["detail"] == {"access_token": "REDACTED", "nested": [{"refresh_token": "REDACTED", "note": "kept"}]}
    assert event["title"] == "Bearer of the Curse run"


# ---------------------------------------------------------------------------
# Serving an old log
# ---------------------------------------------------------------------------

@pytest.fixture
def old_log(tmp_config_dir):
    path = tmp_config_dir / "stream_monitor.log"
    path.write_text(
        "2026-09-01 06:59:59 [INFO] Requesting OAuth token\n"
        f"{OLD_LINE}\n"
        f"2026-09-01 07:00:01 [DEBUG] headers Authorization: Bearer {TOKEN}\n",
        encoding="utf-8",
    )
    return path


def test_parse_debug_log_redacts_an_old_line(old_log):
    entries = sm.parse_debug_log()
    _assert_no_secret(json.dumps(entries))
    failure = next(e for e in entries if e["level"] == "ERROR")
    assert failure["msg"].endswith("client_secret=REDACTED&grant_type=client_credentials")


@pytest.fixture
def port():
    server = HTTPServer(("127.0.0.1", 0), sm.ConfigRequestHandler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path", ["/debug.log", "/debug.log.json"])
def test_the_debug_log_routes_redact_an_old_line(old_log, port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
    finally:
        conn.close()
    assert resp.status == 200
    _assert_no_secret(body)
    assert "client_secret=REDACTED" in body
    assert "Requesting OAuth token" in body  # everything else is served as before


# ---------------------------------------------------------------------------
# The startup scrub
# ---------------------------------------------------------------------------

def test_the_startup_scrub_rewrites_only_files_with_a_secret(tmp_path):
    live = tmp_path / "stream_monitor.log"
    rot1 = tmp_path / "stream_monitor.log.1"
    rot3 = tmp_path / "stream_monitor.log.3"
    other = tmp_path / "other.log"
    live.write_bytes(f"{OLD_LINE}\n".encode("utf-8"))
    rot1.write_bytes(b"2026-09-01 06:00:00 [INFO] nothing secret here\n")
    # A byte that is not valid UTF-8 must survive the rewrite unchanged.
    rot3.write_bytes(b"\xff\xfe odd bytes\n" + f"Authorization: Bearer {TOKEN}\n".encode("utf-8"))
    other.write_bytes(f"{OLD_LINE}\n".encode("utf-8"))
    os.utime(rot1, (1_700_000_000, 1_700_000_000))

    rewritten = sm.scrub_secrets_from_logs(live)

    assert sorted(p.name for p in rewritten) == ["stream_monitor.log", "stream_monitor.log.3"]
    _assert_no_secret(live.read_text(encoding="utf-8"))
    assert "client_secret=REDACTED" in live.read_text(encoding="utf-8")
    assert rot3.read_bytes() == b"\xff\xfe odd bytes\nAuthorization: Bearer REDACTED\n"
    assert rot1.stat().st_mtime == 1_700_000_000  # untouched: no match
    assert other.read_bytes() == f"{OLD_LINE}\n".encode("utf-8")  # not a log rotation
    assert not [p.name for p in tmp_path.iterdir() if "scrub-tmp" in p.name]


def test_the_startup_scrub_never_merges_or_drops_lines(tmp_path):
    """Redaction works line by line: an unclosed quoted value stops at its
    own line break, and a file with no secret (CRLF endings, a line ending
    in the word bearer) is not rewritten at all."""
    live = tmp_path / "stream_monitor.log"
    live.write_bytes(b'x "access_token": "abc\nline two "quoted"\nline three\n')
    clean = tmp_path / "stream_monitor.log.1"
    clean_bytes = b"2026-09-01 06:00:00 [INFO] the flag bearer\r\n2026-09-01 06:00:01 [INFO] next\r\n"
    clean.write_bytes(clean_bytes)
    assert [p.name for p in sm.scrub_secrets_from_logs(live)] == ["stream_monitor.log"]
    assert live.read_bytes() == b'x "access_token": "REDACTED\nline two "quoted"\nline three\n'
    assert clean.read_bytes() == clean_bytes


def test_the_startup_scrub_flushes_before_it_replaces(tmp_path, monkeypatch):
    live = tmp_path / "stream_monitor.log"
    live.write_bytes(f"{OLD_LINE}\n".encode("utf-8"))
    order = []
    real_fsync, real_replace = sm.os.fsync, sm.os.replace
    monkeypatch.setattr(sm.os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(sm.os, "replace", lambda a, b: order.append("replace") or real_replace(a, b))
    assert sm.scrub_secrets_from_logs(live) == [live]
    assert order == ["fsync", "replace"]


def test_the_startup_scrub_skips_a_file_it_cannot_replace(tmp_path, monkeypatch):
    """Another instance may hold the live log open (Windows refuses the
    replace). The scrub leaves it, cleans up, and never raises; serving
    still redacts it."""
    live = tmp_path / "stream_monitor.log"
    live.write_bytes(f"{OLD_LINE}\n".encode("utf-8"))

    def refuse(src, dst):
        raise PermissionError("in use")
    monkeypatch.setattr(sm.os, "replace", refuse)
    assert sm.scrub_secrets_from_logs(live) == []
    assert SECRET in live.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["stream_monitor.log"]


def test_setup_logging_scrubs_before_it_opens_the_log(tmp_path, monkeypatch):
    live = tmp_path / "stream_monitor.log"
    live.write_bytes(f"{OLD_LINE}\n".encode("utf-8"))
    monkeypatch.setattr(sm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sm, "LOG_FILE", live)
    logger = logging.getLogger("StreamMonitor")
    before = list(logger.handlers)
    sm.setup_logging()
    added = [h for h in logger.handlers if h not in before]
    try:
        assert len(added) == 1
        assert any(isinstance(f, sm.SecretRedactingFilter) for f in added[0].filters)
        for h in added:
            h.flush()
        text = live.read_text(encoding="utf-8")
        _assert_no_secret(text)
        assert "Redacted secrets in 1 earlier log file(s): stream_monitor.log" in text
    finally:
        for h in added:
            logger.removeHandler(h)
            h.close()
