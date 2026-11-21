"""Panel tests, with a fake engine (no network call to Discord).

Lancer :  python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="sp-test-")
os.environ["DATA_DIR"] = TMP
os.environ.pop("PANEL_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---- faux moteur `stream` -------------------------------------------------
class FakeEngine:
    fail_with = None  # e.g. "authentication failed" to simulate a fatal error

    def __init__(self, state, session, console):
        self.running = True
        self.stream_connected = True
        self.voice_connected = True
        self.counted_stream_seconds = 0.0
        self.channel_name = "test"
        self._v = 1

    def get_viewer_count(self):
        return self._v

    def start_stream(self):
        if FakeEngine.fail_with:
            print(f"  12:00:00 ✗ auth · {FakeEngine.fail_with}")
            return
        while self.running:
            time.sleep(0.05)
            self.counted_stream_seconds += 30


class FakeState:
    token = ""
    viewer_token = ""

    @classmethod
    def load(cls):
        return cls()

    def save(self):
        pass

    has_token = property(lambda s: bool(s.token))
    has_viewer_token = property(lambda s: bool(s.viewer_token))


stub = types.ModuleType("stream")
stub.STREAM_TIERS = [(1, "Newcomer", 1), (2, "Fledgling", 5), (3, "Breakout", 20)]
stub.State = FakeState
stub.STATE_FILE = None
stub.extract_channel_id = lambda raw: (raw or "").strip().split("/")[-1]
stub.Session = types.SimpleNamespace(new=lambda: types.SimpleNamespace(super_props="x"))
stub.ensure_token = lambda state, props: None
stub.GatewayStreamEngine = FakeEngine
stub.Console = lambda: None
stub.VoiceConnection = types.SimpleNamespace(start_media_stream=lambda c, p: True)
stub._user_id_from_token = lambda t: "1"
stub.USERS_URL = "https://example.invalid/users"
stub._discord_api_get = lambda url, tok, props="": (200, {"id": "1", "username": "me"})
sys.modules["stream"] = stub

import panel  # noqa: E402


class PureFunctions(unittest.TestCase):
    def test_tiers(self):
        tiers, cur, nxt, pct = panel.compute_tiers(12.5, stub.STREAM_TIERS)
        self.assertEqual(cur["tier"], 2)
        self.assertEqual(nxt["tier"], 3)
        self.assertAlmostEqual(pct, 50.0)

    def test_tiers_max_and_zero(self):
        _, cur, nxt, pct = panel.compute_tiers(99, stub.STREAM_TIERS)
        self.assertEqual((cur["tier"], nxt, pct), (3, None, 100.0))
        _, cur, nxt, pct = panel.compute_tiers(0, stub.STREAM_TIERS)
        self.assertEqual((cur, nxt["tier"], pct), (None, 1, 0.0))

    def test_rate_and_eta(self):
        now = time.time()
        day = lambda i: time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        daily = {day(i): 12 * 3600.0 for i in range(7)}
        self.assertAlmostEqual(panel.compute_rate(daily, now), 12.0, delta=0.6)
        self.assertAlmostEqual(panel.compute_eta(24, 12.0), 2 * 86400)
        self.assertIsNone(panel.compute_eta(10, 0.0))
        self.assertEqual(panel.compute_eta(0, 0.0), 0.0)
        self.assertEqual(panel.compute_rate({}, now), 0.0)

    def test_parse_level(self):
        self.assertEqual(panel.parse_level("  12:00:00 ✗ auth · boom"), "err")
        self.assertEqual(panel.parse_level("  12:00:00 ! x · y"), "warn")
        self.assertEqual(panel.parse_level("plain"), "info")

    def test_webhook_rules(self):
        for bad in ("http://example.com/x", "https://127.0.0.1/x", "https://localhost/x", "ftp://a/b", "https://10.0.0.5/x"):
            with self.assertRaises(ValueError, msg=bad):
                panel.safe_webhook(bad)

    def test_token_masking(self):
        ctl = panel.Controller.__new__(panel.Controller)
        ctl.state = FakeState()
        fake = "A" * 24 + "." + "B" * 6 + "." + "C" * 30
        self.assertNotIn(fake, ctl._mask(f"token={fake}"))
        ctl.state.token = "secret-value-123"
        self.assertEqual(ctl._mask("x secret-value-123 y"), "x *** y")

    def test_clean_token(self):
        c = panel.Controller._clean_token
        self.assertEqual(c('  "' + "a" * 40 + '" '), "a" * 40)
        with self.assertRaises(ValueError):
            c("short")
        with self.assertRaises(ValueError):
            c("a" * 20 + " " + "b" * 20)


class ScheduleAndStats(unittest.TestCase):
    def dt(self, y, m, d, hh, mm):  # 2026-11-16 is a Monday
        return datetime(y, m, d, hh, mm, tzinfo=panel.get_tz("Europe/Paris"))

    def test_disabled_schedule_is_always_open(self):
        self.assertTrue(panel.in_schedule({"enabled": False}, self.dt(2026, 11, 16, 3, 0)))
        self.assertTrue(panel.in_schedule(None, self.dt(2026, 11, 16, 3, 0)))

    def test_daytime_window_and_days(self):
        s = {"enabled": True, "start": "08:00", "end": "23:00", "days": [0, 1, 2, 3, 4]}
        self.assertTrue(panel.in_schedule(s, self.dt(2026, 11, 16, 8, 0)))
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 16, 23, 0)))   # fin exclue
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 16, 7, 59)))
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 21, 12, 0)))   # samedi

    def test_overnight_window_belongs_to_start_day(self):
        s = {"enabled": True, "start": "22:00", "end": "06:00", "days": [4]}  # vendredi soir
        self.assertTrue(panel.in_schedule(s, self.dt(2026, 11, 20, 23, 0)))   # ven 23h
        self.assertTrue(panel.in_schedule(s, self.dt(2026, 11, 21, 5, 0)))    # sam 5h (suite de vendredi)
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 21, 23, 0)))  # sam 23h
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 20, 5, 0)))   # ven 5h (suite de jeudi)

    def test_equal_bounds_means_whole_day(self):
        s = {"enabled": True, "start": "00:00", "end": "00:00", "days": [0]}
        self.assertTrue(panel.in_schedule(s, self.dt(2026, 11, 16, 15, 0)))
        self.assertFalse(panel.in_schedule(s, self.dt(2026, 11, 17, 15, 0)))

    def test_normalize_schedule(self):
        ok = panel.normalize_schedule({"enabled": True, "start": "8:05", "end": "23:00", "days": [6, 0, 0, 9]})
        self.assertEqual((ok["start"], ok["days"]), ("08:05", [0, 6]))
        for bad in ({"start": "25:00", "end": "10:00"}, {"start": "08:00"}, {"enabled": True, "start": "08:00", "end": "09:00", "days": []}, "x"):
            with self.assertRaises(ValueError, msg=str(bad)):
                panel.normalize_schedule(bad)

    def test_timezone_helpers(self):
        self.assertTrue(panel.valid_tz("Europe/Paris"))
        self.assertFalse(panel.valid_tz("Mars/Olympus"))
        self.assertEqual(panel.get_tz("Mars/Olympus"), panel.timezone.utc)
        ts = datetime(2026, 11, 16, 23, 30, tzinfo=panel.timezone.utc).timestamp()
        self.assertEqual(panel.day_key(ts), "2026-11-16")
        self.assertEqual(panel.day_key(ts, panel.get_tz("Europe/Paris")), "2026-11-17")

    def test_clean_stats_current_and_legacy_formats(self):
        cur = panel.clean_stats({"stats": {"lifetime_seconds": 7200, "sessions": 3, "baseline_hours": 5,
                                           "daily": {"2026-11-01": 3600, "garbage": 5, "2026-11-02": 999999},
                                           "history": [{"start": 1, "wall": 2, "counted": 3, "peak": 1, "mode": "x", "reason": "r"}, 5]}})
        self.assertEqual(cur["lifetime_seconds"], 7200)
        self.assertEqual(set(cur["daily"]), {"2026-11-01", "2026-11-02"})
        self.assertEqual(cur["daily"]["2026-11-02"], 86400.0)  # capped
        self.assertEqual(len(cur["history"]), 1)
        legacy = panel.clean_stats({"version": "2.0", "totals": {"seconds": 36000, "sessions": 4, "daily": {"2026-10-30": 1800}}})
        self.assertEqual((legacy["lifetime_seconds"], legacy["sessions"]), (36000, 4))
        for bad in ("x", {"stats": {"daily": []}}, {"lifetime_seconds": "abc"}, {"lifetime_seconds": float("nan")}):
            with self.assertRaises(ValueError, msg=str(bad)):
                panel.clean_stats(bad)

    def test_keepalive_url(self):
        self.assertEqual(panel.keepalive_url({}), "")
        self.assertEqual(panel.keepalive_url({"RENDER_EXTERNAL_URL": "https://x.onrender.com/"}), "https://x.onrender.com")
        self.assertEqual(panel.keepalive_url({"KEEPALIVE_URL": "https://a.b", "RENDER_EXTERNAL_URL": "https://x.y"}), "https://a.b")
        self.assertEqual(panel.keepalive_url({"RENDER_EXTERNAL_URL": "https://x.y", "KEEPALIVE": "0"}), "")
        self.assertEqual(panel.keepalive_url({"KEEPALIVE_URL": "javascript:alert(1)"}), "")


class AuthTests(unittest.TestCase):
    def setUp(self):
        for f in Path(TMP).glob("auth.json"):
            f.unlink()
        self.auth = panel.Auth()

    def test_password_roundtrip_and_hashing(self):
        pw = "pw-test-12345"
        self.auth._store(pw)
        self.assertTrue(self.auth.verify(pw))
        self.assertFalse(self.auth.verify("nope"))
        self.assertNotIn(pw, Path(TMP, "auth.json").read_text())

    def test_change_password_revokes_other_sessions(self):
        self.auth._store("old-password")
        a, b = self.auth.create_session(), self.auth.create_session()
        self.auth.change_password("old-password", "new-password-1", keep_sid=a)
        self.assertTrue(self.auth.valid(a))
        self.assertFalse(self.auth.valid(b))
        self.assertTrue(self.auth.verify("new-password-1"))
        with self.assertRaises(ValueError):
            self.auth.change_password("wrong", "another-pass-1")
        with self.assertRaises(ValueError):
            self.auth.change_password("new-password-1", "short")

    def test_logout_revokes(self):
        sid = self.auth.create_session()
        self.assertTrue(self.auth.valid(sid))
        self.auth.revoke(sid)
        self.assertFalse(self.auth.valid(sid))
        self.assertFalse(self.auth.valid(""))

    def test_lockout_is_per_ip(self):
        for _ in range(8):
            self.auth.fail("1.1.1.1")
        self.assertTrue(self.auth.locked("1.1.1.1"))
        self.assertFalse(self.auth.locked("2.2.2.2"))
        self.auth.clear_fails("1.1.1.1")
        self.assertFalse(self.auth.locked("1.1.1.1"))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        panel.AUTH = panel.Auth()
        panel.AUTH._store("integration-pass")
        panel.CTL = panel.Controller()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        panel.CTL.stop(wait=True)
        cls.srv.shutdown()

    def setUp(self):  # isolation: each test starts from a stopped, unconfigured panel
        c = panel.CTL
        c.stop(wait=True)
        FakeEngine.fail_with = None
        c.state.token = c.state.viewer_token = ""
        c.cfg["channel_id"] = ""
        c.cfg.update({"target_hours": 0.0, "tz": "UTC", "schedule": dict(panel.DEFAULT_SCHEDULE)})
        c.intent, c.stop_reason, c.last_error = False, "", ""
        c.stats.update({"lifetime_seconds": 0.0, "sessions": 0, "daily": {}, "history": [], "baseline_hours": 0.0})

    def req(self, method, path, body=None, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-Requested-With": "panel", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def login(self):
        r, _ = self.req("POST", "/api/login", {"password": "integration-pass"})
        self.assertEqual(r.status, 200)
        return r.getheader("Set-Cookie").split(";")[0]

    def test_html_has_csp_nonce(self):
        r, data = self.req("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', data.decode())
        self.assertNotIn("__NONCE__", data.decode())
        self.assertIn("frame-ancestors 'none'", csp)

    def test_auth_required_and_csrf(self):
        r, _ = self.req("GET", "/api/status")
        self.assertEqual(r.status, 401)
        r, _ = self.req("POST", "/api/login", {"password": "x"}, headers={"X-Requested-With": "other"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/login", {"password": "integration-pass"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

    def test_login_wrong_password(self):
        r, _ = self.req("POST", "/api/login", {"password": "wrong"})
        self.assertEqual(r.status, 401)

    def test_logout_invalidates_cookie(self):
        ck = self.login()
        r, _ = self.req("GET", "/api/status", cookie=ck)
        self.assertEqual(r.status, 200)
        self.req("POST", "/api/logout", {}, cookie=ck)
        r, _ = self.req("GET", "/api/status", cookie=ck)
        self.assertEqual(r.status, 401)

    def test_config_validation_and_start_stop(self):
        ck = self.login()
        r, d = self.req("POST", "/api/config", {"token": "short"}, cookie=ck)
        self.assertEqual(r.status, 400)
        r, d = self.req("POST", "/api/config", {"webhook": "http://insecure.example/x"}, cookie=ck)
        self.assertEqual(r.status, 400)
        r, d = self.req("POST", "/api/start", {}, cookie=ck)
        self.assertEqual(r.status, 400)  # incomplete config
        tok = "t" * 60
        r, d = self.req("POST", "/api/config", {"token": tok, "alt_token": tok, "channel_id": "123456789012345678"}, cookie=ck)
        self.assertEqual(r.status, 400)  # alt == principal
        r, d = self.req("POST", "/api/config", {"token": tok, "alt_token": "a" * 60, "channel_id": "https://discord.com/channels/1/123456789012345678"}, cookie=ck)
        self.assertEqual(r.status, 200, d)
        r, d = self.req("GET", "/api/status", cookie=ck)
        st = json.loads(d)
        self.assertTrue(st["config"]["has_token"] and st["config"]["has_alt"])
        self.assertNotIn(tok, d.decode())  # tokens never leave the server
        self.assertEqual(st["config"]["channel_id"], "123456789012345678")

        FakeEngine.fail_with = None
        r, _ = self.req("POST", "/api/start", {}, cookie=ck)
        self.assertEqual(r.status, 200)
        deadline = time.time() + 5
        while time.time() < deadline and json.loads(self.req("GET", "/api/status", cookie=ck)[1])["phase"] != "live":
            time.sleep(0.1)
        st = json.loads(self.req("GET", "/api/status", cookie=ck)[1])
        self.assertEqual(st["phase"], "live")
        time.sleep(0.3)  # let the fake engine count
        panel.CTL._sample()
        self.assertGreater(panel.CTL.stats["lifetime_seconds"], 0)

        r, _ = self.req("POST", "/api/stop", {}, cookie=ck)
        self.assertEqual(r.status, 200)
        deadline = time.time() + 10
        while time.time() < deadline and panel.CTL._alive():
            time.sleep(0.1)
        st = json.loads(self.req("GET", "/api/status", cookie=ck)[1])
        self.assertEqual(st["phase"], "stopped")
        hist = json.loads(self.req("GET", "/api/history", cookie=ck)[1])
        self.assertEqual(len(hist["daily"]), 90)
        self.assertTrue(hist["sessions"] and hist["sessions"][0]["reason"] == "stopped by user")
        r, csv_data = self.req("GET", "/api/export.csv", cookie=ck)
        self.assertTrue(csv_data.decode().startswith("date,"))

    def test_fatal_error_stops_supervisor(self):
        ck = self.login()
        tok = "t" * 60
        self.req("POST", "/api/config", {"token": tok, "channel_id": "123456789012345678"}, cookie=ck)
        FakeEngine.fail_with = "authentication failed: token invalide"
        try:
            self.req("POST", "/api/start", {}, cookie=ck)
            deadline = time.time() + 8
            while time.time() < deadline and panel.CTL.want:
                time.sleep(0.1)
            st = json.loads(self.req("GET", "/api/status", cookie=ck)[1])
            self.assertEqual(st["phase"], "error")
            self.assertIn("authentication failed", st["error"])
        finally:
            FakeEngine.fail_with = None

    def test_logs_levels_and_masking(self):
        ck = self.login()
        panel.CTL._push("  12:00:00 ✗ x · " + "A" * 24 + "." + "B" * 6 + "." + "C" * 30)
        j = json.loads(self.req("GET", "/api/logs?since=0", cookie=ck)[1])
        last = j["entries"][-1]
        self.assertEqual(last["l"], "err")
        self.assertIn("***", last["m"])

    def test_healthz(self):
        r, d = self.req("GET", "/healthz")
        self.assertEqual(r.status, 200)

    def test_baseline_and_reset(self):
        ck = self.login()
        r, _ = self.req("POST", "/api/stats/baseline", {"hours": 6}, cookie=ck)
        self.assertEqual(r.status, 200)
        st = json.loads(self.req("GET", "/api/status", cookie=ck)[1])
        self.assertGreaterEqual(st["lifetime_hours"], 6)
        self.assertEqual(st["tier_current"]["tier"], 2)
        r, _ = self.req("POST", "/api/stats/reset", {}, cookie=ck)
        self.assertEqual(r.status, 400)  # confirmation obligatoire
        r, _ = self.req("POST", "/api/stats/reset", {"confirm": True}, cookie=ck)
        self.assertEqual(r.status, 200)
        st = json.loads(self.req("GET", "/api/status", cookie=ck)[1])
        self.assertEqual(st["lifetime_hours"], 0)

    def _configure(self, ck, **extra):
        body = {"token": "t" * 60, "alt_token": "a" * 60, "channel_id": "123456789012345678", **extra}
        r, d = self.req("POST", "/api/config", body, cookie=ck)
        self.assertEqual(r.status, 200, d)

    def _status(self, ck):
        return json.loads(self.req("GET", "/api/status", cookie=ck)[1])

    def _wait(self, cond, timeout=8):
        end = time.time() + timeout
        while time.time() < end and not cond():
            time.sleep(0.1)
        return cond()

    def test_target_tz_schedule_validation(self):
        ck = self.login()
        for bad in ({"target_hours": "abc"}, {"target_hours": -1}, {"tz": "Mars/Olympus"},
                    {"schedule": {"enabled": True, "start": "99:00", "end": "10:00", "days": [1]}},
                    {"schedule": {"enabled": True, "start": "08:00", "end": "10:00", "days": []}}):
            r, _ = self.req("POST", "/api/config", bad, cookie=ck)
            self.assertEqual(r.status, 400, bad)
        r, d = self.req("POST", "/api/config", {"target_hours": 20, "tz": "Europe/Paris",
                                                "schedule": {"enabled": True, "start": "08:00", "end": "23:00", "days": [0, 1, 2]}}, cookie=ck)
        self.assertEqual(r.status, 200, d)
        st = self._status(ck)
        self.assertEqual((st["target_hours"], st["tz"], st["schedule"]["days"]), (20, "Europe/Paris", [0, 1, 2]))
        self.assertGreater(st["rate_per_day"] + 1, 0)

    def test_goal_reached_stops_stream(self):
        ck = self.login()
        self._configure(ck, target_hours=0.01)  # 36 s; the fake engine counts 30 s every 50 ms
        r, _ = self.req("POST", "/api/start", {}, cookie=ck)
        self.assertEqual(r.status, 200)
        self.assertTrue(self._wait(lambda: panel.CTL._sample() or not panel.CTL.want))
        self.assertTrue(self._wait(lambda: not panel.CTL._alive()))
        st = self._status(ck)
        self.assertEqual(st["phase"], "done")
        self.assertTrue(st["goal_reached"])
        self.assertFalse(panel.CTL.intent)
        r, d = self.req("POST", "/api/start", {}, cookie=ck)
        self.assertEqual(r.status, 400)  # goal already reached
        self.assertIn("Goal", json.loads(d)["error"])
        self.assertEqual(panel.CTL.stats["history"][-1]["reason"], "goal reached")

    def test_schedule_pauses_and_resumes(self):
        ck = self.login()
        today = datetime.now(panel.get_tz("UTC")).weekday()
        other = [d for d in range(7) if d != today]
        self._configure(ck, schedule={"enabled": True, "start": "00:00", "end": "23:59", "days": other})
        r, _ = self.req("POST", "/api/start", {}, cookie=ck)
        self.assertEqual(r.status, 200)
        st = self._status(ck)
        self.assertEqual(st["phase"], "scheduled")  # hors plage : rien ne tourne
        self.assertFalse(st["in_window"])
        self.assertFalse(panel.CTL._alive())
        # the window opens
        self.req("POST", "/api/config", {"schedule": {"enabled": False, "start": "00:00", "end": "23:59", "days": other}}, cookie=ck)
        panel.CTL._schedule_tick()
        self.assertTrue(self._wait(lambda: self._status(ck)["phase"] == "live"))
        # the window closes: paused, but the intent is kept
        self.req("POST", "/api/config", {"schedule": {"enabled": True, "start": "00:00", "end": "23:59", "days": other}}, cookie=ck)
        panel.CTL._schedule_tick()
        self.assertTrue(self._wait(lambda: not panel.CTL._alive()))
        self.assertEqual(self._status(ck)["phase"], "scheduled")
        self.assertTrue(panel.CTL.intent)
        self.assertEqual(panel.CTL.stats["history"][-1]["reason"], "outside schedule")
        # manual stop: the intent is dropped
        self.req("POST", "/api/stop", {}, cookie=ck)
        self.assertEqual(self._status(ck)["phase"], "stopped")
        panel.CTL._schedule_tick()
        self.assertFalse(panel.CTL._alive())

    def test_stats_export_import_roundtrip(self):
        ck = self.login()
        panel.CTL.stats.update({"lifetime_seconds": 7200.0, "sessions": 2, "daily": {"2026-11-01": 7200.0}})
        r, d = self.req("GET", "/api/stats/export", cookie=ck)
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        dump = json.loads(d)
        self.req("POST", "/api/stats/reset", {"confirm": True}, cookie=ck)
        self.assertEqual(self._status(ck)["lifetime_hours"], 0)
        r, d = self.req("POST", "/api/stats/import", {"data": dump}, cookie=ck)
        self.assertEqual(r.status, 200, d)
        self.assertEqual(self._status(ck)["lifetime_hours"], 2.0)
        r, _ = self.req("POST", "/api/stats/import", {"data": "nope"}, cookie=ck)
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/stats/import", {"data": {"totals": {"seconds": 18000, "daily": {}}}}, cookie=ck)  # ancien format
        self.assertEqual(r.status, 200)
        self.assertEqual(self._status(ck)["lifetime_hours"], 5.0)

    def test_check_config(self):
        ck = self.login()
        self.req("POST", "/api/config", {"token": "t" * 60, "alt_token": "a" * 60, "channel_id": "123456789012345678"}, cookie=ck)
        r, d = self.req("POST", "/api/check", {}, cookie=ck)
        self.assertEqual(r.status, 200)
        ids = {c["id"] for c in json.loads(d)["checks"]}
        self.assertTrue({"token", "channel", "alt"} <= ids)


if __name__ == "__main__":
    unittest.main()
