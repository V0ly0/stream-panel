#!/usr/bin/env python3
"""
Stream Panel 3.0 — web interface + 24/7 supervisor around stream.py (unchanged).

- Config (token, channel, mode) through the web interface, password protected
- Auto-start on boot + automatic restart with backoff (+ jitter)
- Stats: viewers, counted time, total, tiers, history per day and per session,
  real rate and estimated date of the next tier
- Hours goal (automatic stop) and schedule (time window, weekdays, timezone)
- Webhook notifications (Discord or generic): fatal error, tier, goal, stall
- JSON export / import of the statistics (also accepts the legacy "discord-stream-host" format)
- "Test configuration" without starting the stream
- Security: hashed password (scrypt), revocable sessions, per-IP attempt limit,
  origin check, nonce-based CSP, data files in 0600
- No extra dependency (standard library only)

Environment variables (all optional):
  PORT            HTTP port (default 7860)        BIND  interface (default 0.0.0.0)
  DATA_DIR        data folder (default ./data)
  PANEL_PASSWORD  panel password (otherwise generated once and shown in the logs)
  DISCORD_TOKEN   main token           ALT_TOKEN   viewer account token
  CHANNEL_ID      voice channel        STREAM_MODE afk | video
  AUTOSTART       1 | 0                NOTIFY_WEBHOOK  https webhook URL
  TRUST_PROXY     1 if the panel sits behind a trusted reverse proxy
                  (uses X-Forwarded-For / -Proto / -Host)
  KEEPALIVE_URL   public URL to ping every 9 min (keep-alive; auto-detected on
                  Render via RENDER_EXTERNAL_URL, disable with KEEPALIVE=0)

Command: python panel.py --reset-password   generates a new password and exits.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

VERSION = "3.0.0"

os.umask(0o077)  # tokens and secrets: readable by the process user only

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", "7860"))
BIND = os.environ.get("BIND", "0.0.0.0")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"

import stream as S  # noqa: E402  (original engine, unmodified)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BOX_ONLY = set("─═╔╗╚╝║ ")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
LINE_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}\s+(\S)\s")
GLYPH_LEVEL = {"✓": "ok", "↑": "sent", "●": "live", "!": "warn", "✗": "err", "ℹ": "info", "▸": "info"}
FATAL_PATTERNS = (
    "authentication failed",
    "invalid token",
    "token is invalid",
    "channel not found",
    "invalid channel type",
    "invalid channel id",
    "missing permission",
    "server not found",
    "not in a server",
)

SESSION_TTL = 7 * 86400
HISTORY_MAX = 100
DAILY_KEEP = 400
STALL_AFTER = 20 * 60          # no connection/stream for X s → "stalled"
NO_VIEWER_AFTER = 30 * 60      # live without viewers for X s → alert
DEFAULT_NOTIFY = {"fatal": True, "tier": True, "goal": True, "stalled": True, "restarts": True, "started": False}
HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_SCHEDULE = {"enabled": False, "start": "08:00", "end": "23:00", "days": list(range(7))}
KEEPALIVE_EVERY = 540


def _now() -> float:
    return time.time()


def _load_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**default, **data}
    except Exception:
        pass
    return dict(default)


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    os.replace(tmp, path)


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}j {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")


def rss_mb() -> float:
    """Resident memory of the panel process (Linux), in MB; 0 if unknown."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS"):
                return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------- pure functions (tested)
def compute_tiers(total_h: float, tiers_def) -> tuple[list, dict | None, dict | None, float]:
    tiers = [{"tier": t, "name": n, "hours": h, "done": total_h >= h} for t, n, h in tiers_def]
    cur = max((t for t in tiers if t["done"]), key=lambda t: t["tier"], default=None)
    nxt = next((t for t in tiers if not t["done"]), None)
    pct = 100.0
    if nxt:
        base = cur["hours"] if cur else 0
        pct = max(0.0, min(100.0, (total_h - base) / (nxt["hours"] - base) * 100))
    return tiers, cur, nxt, pct


def get_tz(name: str | None):
    """Timezone from its IANA name; UTC if unknown or if tzdata is missing."""
    try:
        if ZoneInfo is not None and name:
            return ZoneInfo(name)
    except Exception:
        pass
    return timezone.utc


def valid_tz(name: str) -> bool:
    try:
        return ZoneInfo is not None and bool(name) and bool(ZoneInfo(name))
    except Exception:
        return False


def day_key(ts: float, tz=timezone.utc) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def normalize_schedule(raw) -> dict:
    """Validate and clean a schedule; raises ValueError if inconsistent."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid schedule")
    start, end = str(raw.get("start", "")).strip(), str(raw.get("end", "")).strip()
    if not (HM_RE.match(start) and HM_RE.match(end)):
        raise ValueError("Invalid schedule times (HH:MM format)")
    try:
        days = sorted({int(d) for d in (raw.get("days") or []) if 0 <= int(d) <= 6})
    except (TypeError, ValueError):
        raise ValueError("Invalid schedule days") from None
    enabled = bool(raw.get("enabled"))
    if enabled and not days:
        raise ValueError("Pick at least one day for the schedule")
    pad = lambda v: "%02d:%02d" % tuple(int(x) for x in v.split(":"))  # noqa: E731
    return {"enabled": enabled, "start": pad(start), "end": pad(end), "days": days or list(range(7))}


def in_schedule(sched: dict | None, now: datetime) -> bool:
    """True if `now` (schedule local time) is inside the allowed window.

    A window that crosses midnight (e.g. 22:00 → 06:00) belongs to the day it starts on.
    Start == end means "the whole day".
    """
    if not sched or not sched.get("enabled"):
        return True
    days = set(sched.get("days") or range(7))
    a, b = HM_RE.match(str(sched.get("start", "00:00"))), HM_RE.match(str(sched.get("end", "23:59")))
    if not (a and b):
        return True
    start, end = int(a[1]) * 60 + int(a[2]), int(b[1]) * 60 + int(b[2])
    t = now.hour * 60 + now.minute
    today, yesterday = now.weekday(), (now.weekday() - 1) % 7
    if start == end:
        return today in days
    if start < end:
        return today in days and start <= t < end
    return (t >= start and today in days) or (t < end and yesterday in days)


def clean_stats(raw: dict) -> dict:
    """Clean imported statistics (v2/v3 format or legacy discord-stream-host format)."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid file")
    if isinstance(raw.get("stats"), dict):
        raw = raw["stats"]
    elif isinstance(raw.get("totals"), dict):  # legacy format: {seconds, sessions, daily}
        t = raw["totals"]
        raw = {"lifetime_seconds": t.get("seconds", 0), "sessions": t.get("sessions", 0), "daily": t.get("daily", {})}

    def num(v, lo, hi) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("Invalid numeric value in the file") from None
        if f != f:  # NaN
            raise ValueError("Invalid numeric value in the file")
        return max(lo, min(hi, f))

    daily_in = raw.get("daily", {})
    if not isinstance(daily_in, dict):
        raise ValueError("Invalid file")
    daily = {}
    for k, v in daily_in.items():
        if DAY_RE.match(str(k)):
            daily[str(k)] = num(v, 0.0, 86400.0)
    daily = {k: daily[k] for k in sorted(daily)[-DAILY_KEEP:]}
    history = []
    hist_in = raw.get("history", [])
    if not isinstance(hist_in, list):
        raise ValueError("Invalid file")
    for h in hist_in[-HISTORY_MAX:]:
        if not isinstance(h, dict):
            continue
        try:
            history.append({
                "start": int(num(h.get("start", 0), 0, 4e9)), "wall": int(num(h.get("wall", 0), 0, 1e8)),
                "counted": int(num(h.get("counted", 0), 0, 1e8)), "peak": int(num(h.get("peak", 0), 0, 1e6)),
                "mode": "video" if h.get("mode") == "video" else "afk", "reason": str(h.get("reason", ""))[:60],
            })
        except ValueError:
            continue
    return {
        "lifetime_seconds": num(raw.get("lifetime_seconds", 0), 0.0, 1e9),
        "sessions": int(num(raw.get("sessions", 0), 0, 1e7)),
        "daily": daily, "history": history,
        "baseline_hours": round(num(raw.get("baseline_hours", 0), 0.0, 100000.0), 2),
        "since": int(num(raw.get("since", _now()), 0, 4e9)),
    }


def keepalive_url(env=None) -> str:
    """URL to ping to avoid sleep mode (empty = disabled)."""
    env = os.environ if env is None else env
    if env.get("KEEPALIVE", "") == "0":
        return ""
    url = (env.get("KEEPALIVE_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def compute_rate(daily: dict, now: float, tz=timezone.utc) -> float:
    """Counted hours per day over the last 7 days (shorter window if history is short)."""
    if not daily:
        return 0.0
    today = datetime.fromtimestamp(now, tz)
    keys = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    total = sum(float(daily.get(k, 0.0)) for k in keys)
    try:
        first = min(daily)
        first_ts = datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=tz).timestamp()
    except ValueError:
        return 0.0
    window = min(7 * 86400.0, max(6 * 3600.0, now - first_ts))
    return total / 3600.0 / (window / 86400.0)


def compute_eta(remaining_h: float, rate_h_per_day: float) -> float | None:
    if remaining_h <= 0:
        return 0.0
    if rate_h_per_day < 0.05:
        return None
    return remaining_h / rate_h_per_day * 86400.0


def parse_level(line: str) -> str:
    m = LINE_RE.match(line)
    if m:
        return GLYPH_LEVEL.get(m.group(1), "info")
    return "info"


def safe_webhook(url: str) -> str:
    """Validate a webhook URL: https only, never towards a local/private network."""
    u = urlparse(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("The webhook URL must start with https://")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError("Webhook domain name not found")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Webhook refused: the address points to a private network")
    return url.strip()


# ---------------------------------------------------------------- journal
class LogTee:
    """Duplicate stdout: original console + ring buffer for the interface."""

    def __init__(self, orig, push) -> None:
        self._orig, self._push, self._buf = orig, push, ""
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
        except Exception:
            pass
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = ANSI.sub("", line.split("\r")[-1]).rstrip()
                if line.strip() and not set(line.strip()) <= BOX_ONLY:
                    self._push(line)
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:
            pass

    def reconfigure(self, **_kw) -> None:  # called by Console.__init__
        pass

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._orig, name)


def _make_file_logger() -> logging.Logger:
    lg = logging.getLogger("stream-panel")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        try:
            h = logging.handlers.RotatingFileHandler(DATA_DIR / "panel.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())
    return lg


# ---------------------------------------------------------------- controller
class Controller:
    def __init__(self) -> None:
        S.STATE_FILE = DATA_DIR / "session.json"  # the engine stores token + totals there
        self.lock = threading.RLock()
        self.boot = _now()
        self._seq = 0
        self.logs: deque[tuple[int, float, str, str]] = deque(maxlen=1000)
        self.filelog = _make_file_logger()

        self.cfg = _load_json(
            DATA_DIR / "panel.json",
            {"channel_id": "", "mode": "afk", "autostart": True, "webhook": "", "notify": dict(DEFAULT_NOTIFY),
             "target_hours": 0.0, "tz": "UTC", "schedule": dict(DEFAULT_SCHEDULE)},
        )
        self.cfg["notify"] = {**DEFAULT_NOTIFY, **(self.cfg.get("notify") or {})}
        try:
            self.cfg["schedule"] = normalize_schedule({**DEFAULT_SCHEDULE, **(self.cfg.get("schedule") or {})})
        except ValueError:
            self.cfg["schedule"] = dict(DEFAULT_SCHEDULE)
        if not valid_tz(str(self.cfg.get("tz", ""))):
            self.cfg["tz"] = "UTC"
        self.state = S.State.load()
        self._apply_env()

        self.stats = _load_json(
            DATA_DIR / "stats.json",
            {"lifetime_seconds": 0.0, "sessions": 0, "daily": {}, "since": int(_now()), "baseline_hours": 0.0, "history": []},
        )
        if "tier_notified" not in self.stats:  # no retroactive notification
            self.stats["tier_notified"] = self._tier_count()
        self._stats_dirty = False

        self.engine: S.GatewayStreamEngine | None = None
        self.thread: threading.Thread | None = None
        self.want = False            # the engine must be running now
        self.intent = False          # the user wants a stream (may be paused because of the schedule)
        self.stop_reason = ""
        self.last_error = ""
        self.restarts = 0
        self.run_started = 0.0
        self.session_peak = 0
        self._super_props = ""
        self._applied = ""
        self._bad_since = 0.0
        self._novw_since = 0.0
        self._last_alert: dict[str, float] = {}

        sys.stdout = LogTee(sys.stdout, self._push)  # type: ignore[assignment]
        S.input = self._fake_input  # type: ignore[attr-defined]  # no more blocking prompts
        self._patch_media_mode()
        threading.Thread(target=self._sample_loop, daemon=True).start()

    # ---------- config ----------
    def _apply_env(self) -> None:
        e = os.environ
        if e.get("DISCORD_TOKEN"):
            self.state.token = e["DISCORD_TOKEN"].strip()
        if e.get("ALT_TOKEN"):
            self.state.viewer_token = e["ALT_TOKEN"].strip()
        if e.get("CHANNEL_ID"):
            self.cfg["channel_id"] = S.extract_channel_id(e["CHANNEL_ID"])
        if e.get("STREAM_MODE") in ("afk", "video"):
            self.cfg["mode"] = e["STREAM_MODE"]
        if e.get("AUTOSTART") in ("0", "1"):
            self.cfg["autostart"] = e["AUTOSTART"] == "1"
        if e.get("NOTIFY_WEBHOOK"):
            self.cfg["webhook"] = e["NOTIFY_WEBHOOK"].strip()

    @staticmethod
    def _clean_token(raw: str) -> str:
        tok = str(raw or "").strip().strip('"').strip("'")
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        if tok and (re.search(r"\s", tok) or len(tok) < 30):
            raise ValueError("This token has the wrong shape (pasted in full, with no space or line break?)")
        return tok

    def save_config(self, body: dict) -> None:
        with self.lock:
            tok = self._clean_token(body.get("token", ""))
            alt = self._clean_token(body.get("alt_token", ""))
            new_main = tok or self.state.token
            new_alt = "" if body.get("clear_alt") else (alt or self.state.viewer_token)
            if new_main and new_alt and new_main == new_alt:
                raise ValueError("The alt token must belong to a different account than the one that streams")
            ch = str(body.get("channel_id", "")).strip()
            cid = ""
            if ch:
                cid = S.extract_channel_id(ch)
                if not cid.isdigit():
                    raise ValueError("Invalid channel ID (digits only, or the channel link)")
            hook = None
            if "webhook" in body:
                hook = str(body["webhook"]).strip()
                if hook:
                    hook = safe_webhook(hook)
            target = None
            if "target_hours" in body:
                try:
                    target = float(body["target_hours"] or 0)
                except (TypeError, ValueError):
                    raise ValueError("Invalid hours goal") from None
                if not (0 <= target <= 100000):
                    raise ValueError("Invalid hours goal (0 = no goal)")
            tzname = None
            if "tz" in body:
                tzname = str(body["tz"]).strip() or "UTC"
                if not valid_tz(tzname):
                    raise ValueError("Unknown timezone (e.g. Europe/Paris, UTC)")
            sched = None
            if "schedule" in body:
                sched = normalize_schedule(body["schedule"])
            # everything is valid: apply
            if target is not None:
                self.cfg["target_hours"] = round(target, 2)
            if tzname is not None:
                self.cfg["tz"] = tzname
            if sched is not None:
                self.cfg["schedule"] = sched
            self.state.token, self.state.viewer_token = new_main, new_alt
            if cid:
                self.cfg["channel_id"] = cid
            if body.get("mode") in ("afk", "video"):
                self.cfg["mode"] = body["mode"]
            if "autostart" in body:
                self.cfg["autostart"] = bool(body["autostart"])
            if hook is not None:
                self.cfg["webhook"] = hook
            if isinstance(body.get("notify"), dict):
                for k in DEFAULT_NOTIFY:
                    if k in body["notify"]:
                        self.cfg["notify"][k] = bool(body["notify"][k])
            self.state.save()
            _save_json(DATA_DIR / "panel.json", self.cfg)
            try:
                (DATA_DIR / "session.json").chmod(0o600)
            except Exception:
                pass

    def set_baseline(self, hours: float) -> None:
        if not (0 <= hours <= 100000):
            raise ValueError("Invalid hours value")
        with self.lock:
            self.stats["baseline_hours"] = round(hours, 2)
            self.stats["tier_notified"] = self._tier_count()
            _save_json(DATA_DIR / "stats.json", self.stats)

    def reset_stats(self) -> None:
        with self.lock:
            self.stats.update({"lifetime_seconds": 0.0, "sessions": 0, "daily": {}, "history": [], "baseline_hours": 0.0, "tier_notified": 0})
            _save_json(DATA_DIR / "stats.json", self.stats)
            eng = self.engine
            if eng is not None:
                eng._panel_last = float(getattr(eng, "counted_stream_seconds", 0.0))  # type: ignore[attr-defined]

    # ---------- timezone, schedule, goal ----------
    @property
    def tz(self):
        return get_tz(self.cfg.get("tz"))

    def _in_window(self) -> bool:
        return in_schedule(self.cfg.get("schedule"), datetime.now(self.tz))

    def _target(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("target_hours") or 0))
        except (TypeError, ValueError):
            return 0.0

    def _goal_reached(self) -> bool:
        t = self._target()
        return t > 0 and self._total_hours() >= t

    def export_stats(self) -> dict:
        with self.lock:
            return {"app": "stream-panel", "version": VERSION, "exported": int(_now()),
                    "stats": json.loads(json.dumps(self.stats))}

    def import_stats(self, payload: dict) -> None:
        new = clean_stats(payload)
        with self.lock:
            self.stats.update(new)
            self.stats["tier_notified"] = self._tier_count()
            _save_json(DATA_DIR / "stats.json", self.stats)
            eng = self.engine
            if eng is not None:  # do not recount what the engine already produced
                eng._panel_last = float(getattr(eng, "counted_stream_seconds", 0.0))  # type: ignore[attr-defined]
        self.log("statistics imported")

    def _config_fingerprint(self) -> str:
        raw = "|".join([self.state.token, self.state.viewer_token, str(self.cfg.get("channel_id", "")), str(self.cfg.get("mode", ""))])
        return hashlib.sha256(raw.encode()).hexdigest()

    def secrets_list(self) -> list[str]:
        return [t for t in (self.state.token, self.state.viewer_token) if t]

    def _mask(self, line: str) -> str:
        for s in self.secrets_list():
            line = line.replace(s, "***")
        return TOKEN_RE.sub("***", line)

    # ---------- logs ----------
    def _push(self, line: str) -> None:
        line = self._mask(line)
        lvl = parse_level(line)
        with self.lock:
            self._seq += 1
            self.logs.append((self._seq, _now(), lvl, line))
        try:
            self.filelog.info(line)
        except Exception:
            pass

    def log(self, text: str, lvl: str = "info") -> None:
        self._push(f"{time.strftime('%H:%M:%S')} {'✗' if lvl == 'err' else '▸'} panel · {text}")

    def get_logs(self, since: int = 0) -> dict:
        with self.lock:
            rows = [r for r in self.logs if r[0] > since]
            seq = self._seq
        return {"seq": seq, "entries": [{"n": n, "t": t, "l": lv, "m": self._mask(m)} for n, t, lv, m in rows]}

    def logs_text(self) -> str:
        with self.lock:
            rows = list(self.logs)
        return "\n".join(f"{datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')} {self._mask(m)}" for _, t, _, m in rows) + "\n"

    # ---------- notifications ----------
    def notify(self, kind: str, text: str, force: bool = False) -> None:
        hook = str(self.cfg.get("webhook", "")).strip()
        if not hook or not (force or self.cfg["notify"].get(kind, False)):
            return
        threading.Thread(target=self._send_hook, args=(hook, text), daemon=True).start()

    def _send_hook(self, hook: str, text: str) -> None:
        try:
            safe_webhook(hook)
            payload = json.dumps({"content": text, "text": text, "username": "Stream Panel"}).encode()
            req = urllib.request.Request(hook, data=payload, method="POST", headers={"Content-Type": "application/json", "User-Agent": f"StreamPanel/{VERSION}"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            self.log(f"notification not sent: {exc}")

    def test_notify(self) -> None:
        hook = str(self.cfg.get("webhook", "")).strip()
        if not hook:
            raise ValueError("No webhook configured")
        safe_webhook(hook)
        payload = json.dumps({"content": "✅ Stream Panel: test notification", "text": "Stream Panel: test notification", "username": "Stream Panel"}).encode()
        req = urllib.request.Request(hook, data=payload, method="POST", headers={"Content-Type": "application/json", "User-Agent": f"StreamPanel/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            raise RuntimeError(f"Send failed: {exc}") from exc

    def _alert_once(self, key: str, text: str, kind: str, every: float = 6 * 3600) -> None:
        if _now() - self._last_alert.get(key, 0) >= every:
            self._last_alert[key] = _now()
            self.notify(kind, text)

    # ---------- patches moteur ----------
    def _fake_input(self, prompt: str = "") -> str:
        p = ANSI.sub("", str(prompt)).lower()
        if "voice channel id" in p:
            return self.cfg.get("channel_id", "")
        if "try entering" in p:
            return "n"
        return ""

    def _patch_media_mode(self) -> None:
        orig = S.VoiceConnection.start_media_stream
        ctl = self

        def patched(conn, video_path):
            if ctl.cfg.get("mode") != "video":
                return False  # the engine then falls back to the silence pump (0 % CPU)
            return orig(conn, video_path)

        S.VoiceConnection.start_media_stream = patched  # type: ignore[assignment]

    # ---------- lifecycle ----------
    def ready_error(self) -> str:
        if not self.state.has_token:
            return "Missing Discord token"
        if not str(self.cfg.get("channel_id", "")).isdigit():
            return "Missing voice channel ID"
        return ""

    def _alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self.lock:
            if self.want:
                return
            if self._alive():
                raise ValueError("Stop in progress, try again in a few seconds")
            err = self.ready_error()
            if err:
                raise ValueError(err)
            if self._goal_reached():
                raise ValueError(f"Goal of {self._target():g} h already reached: raise it or set 0 to remove it")
            self.intent, self.last_error = True, ""
            if not self._in_window():
                self.log("outside schedule: the stream will start when the window opens")
                return
            self._launch()

    def _launch(self) -> None:
        """Start the supervision thread (lock already held)."""
        self.want, self.restarts = True, 0
        self._bad_since = 0.0
        self.thread = threading.Thread(target=self._supervise, daemon=True)
        self.thread.start()
        self.notify("started", "▶️ Stream Panel: stream started")

    def stop(self, wait: bool = False, keep_intent: bool = False, reason: str = "") -> None:
        with self.lock:
            was = self.want
            if reason:
                self.stop_reason = reason
            self.want = False
            if not keep_intent:
                self.intent = False
            eng, th = self.engine, self.thread
            if eng:
                eng.running = False
        if wait and th and th is not threading.current_thread():
            th.join(timeout=25)
        if was:
            self.log(f"stopped: {reason}" if reason else "stop requested")

    def _schedule_tick(self) -> None:
        """Pause outside the schedule window, resume when the window opens."""
        with self.lock:
            if not self.intent:
                return
            inw, want, alive = self._in_window(), self.want, self._alive()
        if want and not inw:
            self.stop(keep_intent=True, reason="outside schedule")
        elif inw and not want and not alive:
            with self.lock:
                if self.intent and not self.want and not self._alive() and not self._goal_reached():
                    self.log("schedule window open: resuming the stream")
                    self._launch()

    def _goal_stop(self) -> None:
        t = self._target()
        self.log(f"goal of {t:g} h reached: automatic stop")
        self.notify("goal", f"🎯 Stream Panel: goal of {t:g} h reached, stream stopped.")
        self.stop(reason="goal reached")

    def restart(self) -> None:
        def job() -> None:
            self.stop(wait=True)
            try:
                self.start()
                self.log("restart done")
            except Exception as exc:
                self.log(f"restart failed: {exc}", "err")

        threading.Thread(target=job, daemon=True).start()

    def _fatal_since(self, mark: int) -> str:
        with self.lock:
            lines = [m for n, _, _, m in self.logs if n > mark]
        for ln in reversed(lines):
            low = ln.lower()
            if any(p in low for p in FATAL_PATTERNS):
                return ANSI.sub("", ln.split("·", 1)[-1]).strip()[:300]
        return ""

    def _supervise(self) -> None:
        backoff = 5
        while self.want:
            with self.lock:
                mark = self._seq
            began = _now()
            eng_ref = None
            try:
                eng_ref = self._run_once()
            except Exception as exc:  # the engine must never kill the panel
                self.log(f"engine error: {exc!r}", "err")
                self.last_error = str(exc)[:300]
            reason = self.stop_reason or "stopped by user"
            fatal = self._fatal_since(mark) if self.want else ""
            if self.want:
                reason = "configuration error" if fatal else "session interrupted"
            else:
                self.stop_reason = ""
            self._record_session(eng_ref, began, reason)
            self.engine = None
            if not self.want:
                break
            if fatal:
                self.last_error, self.want, self.intent = fatal, False, False
                self.log(f"permanent stop (config needs fixing): {fatal}", "err")
                self.notify("fatal", f"⛔ Stream Panel stopped: {fatal}")
                break
            if _now() - began > 300:
                backoff = 5
            self.restarts += 1
            if self.restarts in (3, 10, 30):
                self.notify("restarts", f"⚠️ Stream Panel: {self.restarts} consecutive restarts. Last cause: {self.last_error or 'connection interrupted'}")
            wait = min(300, backoff) + random.uniform(0, min(5, backoff / 2))
            self.log(f"session ended — restarting in {int(wait)}s (restart #{self.restarts})")
            end = _now() + wait
            while self.want and _now() < end:
                time.sleep(0.5)
            backoff = min(backoff * 2, 300)

    def _run_once(self):
        self.run_started = _now()
        self.session_peak = 0
        self._applied = self._config_fingerprint()
        sess = S.Session.new()
        self._super_props = sess.super_props
        try:
            S.ensure_token(self.state, sess.super_props)  # needed by the engine, non-blocking otherwise
        except Exception as exc:
            self.log(f"token check: {exc}", "err")
            if "401" in str(exc):
                self._push("authentication failed: invalid token (HTTP 401)")
                return None
        eng = S.GatewayStreamEngine(self.state, sess, S.Console())
        self.engine = eng
        try:
            eng.start_stream()  # blocks while the session runs (no stdin/msvcrt → headless loop)
        finally:
            self._account(eng, final=True)
        return eng

    # ---------- statistiques ----------
    def _total_hours(self) -> float:
        return float(self.stats.get("baseline_hours", 0.0)) + self.stats["lifetime_seconds"] / 3600.0

    def _tier_count(self) -> int:
        total = self._total_hours()
        return sum(1 for _, _, h in S.STREAM_TIERS if total >= h)

    def _account(self, eng, final: bool = False) -> None:
        with self.lock:
            cur = float(getattr(eng, "counted_stream_seconds", 0.0))
            last = float(getattr(eng, "_panel_last", 0.0))
            delta = max(0.0, cur - last)
            eng._panel_last = cur  # type: ignore[attr-defined]
            if delta > 0:
                day = day_key(_now(), self.tz)
                self.stats["lifetime_seconds"] += delta
                self.stats["daily"][day] = self.stats["daily"].get(day, 0.0) + delta
                for k in sorted(self.stats["daily"])[:-DAILY_KEEP]:
                    del self.stats["daily"][k]
                self._stats_dirty = True
                n = self._tier_count()
                if n > self.stats.get("tier_notified", 0):
                    self.stats["tier_notified"] = n
                    t = S.STREAM_TIERS[n - 1]
                    self.notify("tier", f"🏆 Tier {t[0]} '{t[1]}' reached ({t[2]} h counted)!")
                if self.want and self._goal_reached():
                    self.want = False  # avoids a double trigger; stop() cleans up the rest
                    threading.Thread(target=self._goal_stop, daemon=True).start()
            if final and cur >= 10:
                self.stats["sessions"] += 1
                self._stats_dirty = True
            if self._stats_dirty:
                _save_json(DATA_DIR / "stats.json", self.stats)
                self._stats_dirty = False

    def _record_session(self, eng, began: float, reason: str) -> None:
        wall = _now() - began
        counted = float(getattr(eng, "counted_stream_seconds", 0.0)) if eng is not None else 0.0
        if wall < 60 and counted < 10:
            return  # aborted attempt: no history entry
        with self.lock:
            hist = self.stats.setdefault("history", [])
            hist.append({
                "start": int(began), "wall": int(wall), "counted": int(counted),
                "peak": int(self.session_peak), "mode": self.cfg.get("mode", "afk"), "reason": reason,
            })
            del hist[:-HISTORY_MAX]
            _save_json(DATA_DIR / "stats.json", self.stats)

    def _sample_loop(self) -> None:
        while True:
            time.sleep(15)
            try:
                self._sample()
            except Exception:
                pass

    def _sample(self) -> None:
        eng = self.engine
        if eng is not None:
            self._account(eng)
        self._schedule_tick()
        phase, viewers = self._phase_and_viewers()
        self.session_peak = max(self.session_peak, viewers)
        # "stalled" state: we want to stream but are neither live nor waiting for a viewer
        if self.want and phase not in ("live", "waiting"):
            self._bad_since = self._bad_since or _now()
            if _now() - self._bad_since > STALL_AFTER:
                self._alert_once("stall", f"⚠️ Stream Panel: no active stream for {fmt_dur(_now() - self._bad_since)} (state: {phase}).", "stalled")
        else:
            self._bad_since = 0.0
        if phase == "waiting":
            self._novw_since = self._novw_since or _now()
            if _now() - self._novw_since > NO_VIEWER_AFTER:
                self._alert_once("noviewer", "⚠️ Stream Panel: live but no viewer for over 30 min — the counter is paused. Check the alt account.", "stalled")
        else:
            self._novw_since = 0.0

    def healthy(self) -> bool:
        return not (self.want and self._bad_since and _now() - self._bad_since > STALL_AFTER)

    # ---------- state for the interface ----------
    def _phase_and_viewers(self) -> tuple[str, int]:
        eng = self.engine
        viewers, live, voice = 0, False, False
        if eng is not None:
            try:
                viewers = eng.get_viewer_count()
                live, voice = bool(eng.stream_connected), bool(eng.voice_connected)
            except Exception:
                pass
        if not self.want:
            if self._alive():
                phase = "stopping"
            elif self.last_error:
                phase = "error"
            elif self.intent:
                phase = "scheduled"
            elif self._goal_reached():
                phase = "done"
            else:
                phase = "stopped"
        elif eng is None:
            phase = "retrying"
        elif live:
            phase = "live" if viewers > 0 else "waiting"
        elif voice:
            phase = "starting"
        else:
            phase = "connecting"
        return phase, viewers

    def status(self) -> dict:
        eng = self.engine
        counted, channel = 0.0, ""
        if eng is not None:
            try:
                counted = float(eng.counted_stream_seconds)
                channel = eng.channel_name or ""
            except Exception:
                pass
        phase, viewers = self._phase_and_viewers()

        with self.lock:
            total_h = self._total_hours()
            daily = dict(self.stats["daily"])
            sessions = self.stats["sessions"]
            baseline = float(self.stats.get("baseline_hours", 0.0))
        tiers, cur, nxt, pct = compute_tiers(total_h, S.STREAM_TIERS)
        rate = compute_rate(daily, _now(), self.tz)
        eta = compute_eta(nxt["hours"] - total_h, rate) if nxt else None
        tz = self.tz
        today = day_key(_now(), tz)
        target = self._target()
        t_eta = compute_eta(target - total_h, rate) if target > 0 else None

        load = None
        try:
            load = round(os.getloadavg()[0], 2)
        except Exception:
            pass
        running = self.want or self._alive()
        return {
            "version": VERSION,
            "phase": phase,
            "error": self.last_error,
            "viewers": viewers,
            "peak": self.session_peak,
            "alt_enabled": self.state.has_viewer_token,
            "session_counted": counted,
            "session_wall": (_now() - self.run_started) if (eng is not None and self.run_started) else 0,
            "channel_name": channel,
            "restarts": self.restarts,
            "panel_uptime": _now() - self.boot,
            "lifetime_hours": round(total_h, 2),
            "baseline_hours": baseline,
            "today_hours": round(daily.get(today, 0.0) / 3600.0, 2),
            "rate_per_day": round(rate, 2),
            "eta_seconds": eta,
            "eta_date": day_key(_now() + eta, tz) if eta else None,
            "coverage": round(min(100.0, rate / 24.0 * 100.0), 1),
            "target_hours": target,
            "goal_reached": self._goal_reached(),
            "target_eta_seconds": t_eta,
            "target_eta_date": day_key(_now() + t_eta, tz) if t_eta else None,
            "tz": self.cfg.get("tz", "UTC"),
            "local_time": datetime.now(tz).strftime("%H:%M"),
            "schedule": self.cfg.get("schedule"),
            "in_window": self._in_window(),
            "rss_mb": rss_mb(),
            "keepalive": bool(KEEPALIVE),
            "sessions": sessions,
            "tiers": tiers,
            "tier_current": cur,
            "tier_next": nxt,
            "tier_pct": round(pct, 1),
            "load": load,
            "config_pending": bool(running and self._applied and self._applied != self._config_fingerprint()),
            "config": {
                "has_token": self.state.has_token,
                "has_alt": self.state.has_viewer_token,
                "channel_id": self.cfg.get("channel_id", ""),
                "mode": self.cfg.get("mode", "afk"),
                "autostart": bool(self.cfg.get("autostart", True)),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "webhook_set": bool(self.cfg.get("webhook")),
                "notify": self.cfg["notify"],
                "password_from_env": bool(os.environ.get("PANEL_PASSWORD", "").strip()),
            },
            "ready_error": self.ready_error(),
        }

    def history(self) -> dict:
        today = datetime.now(self.tz)
        with self.lock:
            dmap = dict(self.stats["daily"])
            sessions = list(self.stats.get("history", []))[::-1]
        daily = []
        for i in range(89, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            daily.append({"date": d, "hours": round(dmap.get(d, 0.0) / 3600.0, 2)})
        return {"daily": daily, "sessions": sessions}

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "hours_counted"])
        with self.lock:
            for k in sorted(self.stats["daily"]):
                w.writerow([k, round(self.stats["daily"][k] / 3600.0, 3)])
        return buf.getvalue()

    # ---------- appels Discord ----------
    def _props(self) -> str:
        if not self._super_props:
            self._super_props = S.Session.new().super_props
        return self._super_props

    def profile(self) -> dict:
        if not self.state.has_token:
            raise ValueError("Missing Discord token")
        uid = S._user_id_from_token(self.state.token)
        if not uid:
            raise RuntimeError("cannot read the account ID from the token")
        url = f"{S.USERS_URL}/{uid}/profile?with_mutual_guilds=false&with_mutual_friends_count=false"
        status, data = S._discord_api_get(url, self.state.token, self._props())
        if status != 200:
            raise RuntimeError(f"Discord answered HTTP {status}: {data.get('message', 'error')}")
        user = data.get("user", {})
        return {
            "id": uid,
            "username": user.get("username", "?"),
            "display": user.get("global_name") or user.get("username", "?"),
            "badges": [{"id": b.get("id", "?"), "description": b.get("description", "")} for b in data.get("badges", [])],
        }

    def check_config(self) -> list[dict]:
        """Check the configuration without starting the stream."""
        out: list[dict] = []

        def add(i: str, ok: bool | None, label: str, detail: str = "") -> None:
            out.append({"id": i, "ok": ok, "label": label, "detail": detail})

        props = self._props()
        main_id = ""
        if not self.state.has_token:
            add("token", False, "Main token", "missing")
        else:
            st, d = S._discord_api_get("https://discord.com/api/v9/users/@me", self.state.token, props)
            if st == 200:
                main_id = str(d.get("id", ""))
                add("token", True, "Main token", f"account @{d.get('username', '?')}")
            elif st == 401:
                add("token", False, "Main token", "rejected by Discord (invalid or expired)")
            else:
                add("token", False, "Main token", f"Discord answered HTTP {st}: {d.get('message', '')}")

        cid = str(self.cfg.get("channel_id", ""))
        if not cid.isdigit():
            add("channel", False, "Voice channel", "missing ID")
        elif self.state.has_token:
            st, d = S._discord_api_get(f"https://discord.com/api/v9/channels/{cid}", self.state.token, props)
            if st == 200:
                kind = d.get("type")
                if kind in (2, 13):
                    add("channel", True, "Voice channel", f"#{d.get('name', '?')} ({'stage' if kind == 13 else 'voice'})")
                else:
                    add("channel", False, "Voice channel", "this channel is not a voice channel")
            elif st in (403, 404):
                add("channel", False, "Voice channel", "not found or not accessible for this account")
            else:
                add("channel", False, "Voice channel", f"HTTP {st} : {d.get('message', '')}")

        if not self.state.has_viewer_token:
            add("alt", None, "Viewer account (alt)", "not configured: without a viewer the counter stays paused")
        else:
            st, d = S._discord_api_get("https://discord.com/api/v9/users/@me", self.state.viewer_token, props)
            if st == 200:
                if main_id and str(d.get("id", "")) == main_id:
                    add("alt", False, "Viewer account (alt)", "this is the same account as the main one")
                else:
                    add("alt", True, "Viewer account (alt)", f"account @{d.get('username', '?')}")
            elif st == 401:
                add("alt", False, "Viewer account (alt)", "token rejected by Discord")
            else:
                add("alt", False, "Viewer account (alt)", f"HTTP {st} : {d.get('message', '')}")

        if self.cfg.get("mode") == "video":
            has = bool(shutil.which("ffmpeg"))
            vid = (ROOT / "assets" / "video.mp4").exists()
            add("ffmpeg", has and vid, "Video mode", "ffmpeg and assets/video.mp4 present" if has and vid else ("ffmpeg not found" if not has else "assets/video.mp4 not found"))
        if self.cfg.get("webhook"):
            try:
                safe_webhook(self.cfg["webhook"])
                add("webhook", True, "Notification webhook", "valid URL (use 'Send a test')")
            except ValueError as exc:
                add("webhook", False, "Notification webhook", str(exc))
        return out


# ---------------------------------------------------------------- panel authentication
class Auth:
    SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)

    def __init__(self, reset: bool = False) -> None:
        self.file = DATA_DIR / "auth.json"
        self.sess_file = DATA_DIR / "sessions.json"
        self.env_pw = os.environ.get("PANEL_PASSWORD", "").strip()
        self.lock = threading.Lock()
        self.fails: dict[str, deque[float]] = {}
        self.sessions: dict[str, dict] = {}
        self._load_sessions()
        self._init_password(reset)

    # --- password ---
    def _hash(self, pw: str, salt: bytes) -> str:
        return hashlib.scrypt(pw.encode(), salt=salt, **self.SCRYPT).hex()

    def _store(self, pw: str) -> None:
        salt = secrets.token_bytes(16)
        _save_json(self.file, {"salt": salt.hex(), "hash": self._hash(pw, salt)})

    def _init_password(self, reset: bool) -> None:
        legacy = DATA_DIR / "panel_password.txt"
        if self.env_pw:
            return
        if reset or not self.file.exists():
            if not reset and legacy.exists():  # migration from v1: hash the old password
                self._store(legacy.read_text().strip())
                legacy.unlink(missing_ok=True)
                return
            pw = secrets.token_urlsafe(12)
            self._store(pw)
            legacy.unlink(missing_ok=True)
            self.sessions.clear()
            self._save_sessions()
            bar = "=" * 60
            sys.__stderr__.write(f"\n{bar}\n  PANEL PASSWORD (shown only once): {pw}\n  Change it in Settings, or set PANEL_PASSWORD.\n{bar}\n\n")

    def verify(self, pw: str) -> bool:
        if self.env_pw:
            return hmac.compare_digest(pw.encode(), self.env_pw.encode())
        try:
            d = json.loads(self.file.read_text())
            return hmac.compare_digest(self._hash(pw, bytes.fromhex(d["salt"])), d["hash"])
        except Exception:
            return False

    def change_password(self, old: str, new: str, keep_sid: str = "") -> None:
        if self.env_pw:
            raise ValueError("The password is set by PANEL_PASSWORD: change the environment variable instead")
        if not self.verify(old):
            raise ValueError("Current password is incorrect")
        if len(new) < 8:
            raise ValueError("The new password must be at least 8 characters long")
        self._store(new)
        self.revoke_all(keep_sid)

    # --- anti brute-force (per IP) ---
    def locked(self, ip: str) -> bool:
        now = _now()
        with self.lock:
            q = self.fails.get(ip)
            if not q:
                return False
            return sum(1 for t in q if now - t < 600) >= 8

    def fail(self, ip: str) -> None:
        with self.lock:
            if len(self.fails) > 2000:
                self.fails.clear()
            self.fails.setdefault(ip, deque(maxlen=20)).append(_now())

    def clear_fails(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)

    # --- revocable sessions ---
    @staticmethod
    def _h(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def _load_sessions(self) -> None:
        try:
            data = json.loads(self.sess_file.read_text())
            now = _now()
            self.sessions = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("exp", 0) > now}
        except Exception:
            self.sessions = {}

    def _save_sessions(self) -> None:
        try:
            _save_json(self.sess_file, self.sessions)
        except Exception:
            pass

    def create_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self.lock:
            now = _now()
            self.sessions = {k: v for k, v in self.sessions.items() if v["exp"] > now}
            self.sessions[self._h(sid)] = {"exp": now + SESSION_TTL, "created": now}
            self._save_sessions()
        return sid

    def valid(self, sid: str) -> bool:
        if not sid:
            return False
        with self.lock:
            s = self.sessions.get(self._h(sid))
            return bool(s and s["exp"] > _now())

    def revoke(self, sid: str) -> None:
        with self.lock:
            self.sessions.pop(self._h(sid), None)
            self._save_sessions()

    def revoke_all(self, keep_sid: str = "") -> None:
        with self.lock:
            keep = self._h(keep_sid) if keep_sid else None
            self.sessions = {k: v for k, v in self.sessions.items() if k == keep}
            self._save_sessions()


CTL: Controller
AUTH: Auth
UI_HTML = (ROOT / "ui.html").read_text(encoding="utf-8") if (ROOT / "ui.html").exists() else "<h1>ui.html missing</h1>"


class Handler(BaseHTTPRequestHandler):
    server_version = "StreamPanel"
    protocol_version = "HTTP/1.1"
    timeout = 30  # avoids lingering connections (slowloris)

    def log_message(self, *_a) -> None:
        pass

    # --- helpers ---
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), extra=extra)

    def _ip(self) -> str:
        if TRUST_PROXY:
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")
            if xff and xff[-1].strip():
                return xff[-1].strip()
        return self.client_address[0]

    def _sid(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sp_session":
                return v
        return ""

    def _authed(self) -> bool:
        return AUTH.valid(self._sid())

    def _read_body(self, limit: int = 16384) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    def _secure(self) -> bool:
        return TRUST_PROXY and self.headers.get("X-Forwarded-Proto") == "https"

    def _cookie(self, sid: str, max_age: int) -> str:
        return f"sp_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}" + ("; Secure" if self._secure() else "")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("X-Forwarded-Host") if TRUST_PROXY else None) or self.headers.get("Host") or ""
        return urlparse(origin).netloc == host

    # --- routes ---
    def do_GET(self) -> None:
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; "
                   "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return self._send(200, UI_HTML.replace("__NONCE__", nonce).encode(), "text/html", {"Content-Security-Policy": csp})
        if path == "/healthz":
            ok = CTL.healthy()
            return self._json(200 if ok else 503, {"ok": ok})
        if path == "/api/session":
            return self._json(200, {"authed": self._authed(), "version": VERSION})
        if not self._authed():
            return self._json(401, {"error": "not authenticated"})
        try:
            if path == "/api/status":
                return self._json(200, CTL.status())
            if path == "/api/logs":
                since = int((qs.get("since") or ["0"])[0])
                return self._json(200, CTL.get_logs(since))
            if path == "/api/logs/download":
                return self._send(200, CTL.logs_text().encode(), "text/plain", {"Content-Disposition": 'attachment; filename="stream-panel.log"'})
            if path == "/api/history":
                return self._json(200, CTL.history())
            if path == "/api/stats/export":
                name = "stream-panel-stats-%s.json" % time.strftime("%Y%m%d")
                return self._send(200, json.dumps(CTL.export_stats(), indent=1).encode(), "application/json",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/export.csv":
                return self._send(200, CTL.export_csv().encode(), "text/csv", {"Content-Disposition": 'attachment; filename="stream-stats.csv"'})
            if path == "/api/profile":
                return self._json(200, CTL.profile())
        except Exception as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            raw = self._read_body(524288 if path == "/api/stats/import" else 16384)  # always read: keeps the keep-alive connection healthy
        except Exception as exc:
            return self._json(413, {"error": str(exc)})
        if self.headers.get("X-Requested-With") != "panel" or not self._origin_ok():  # anti-CSRF
            return self._json(403, {"error": "request refused"})
        try:
            body = json.loads(raw.decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError("Invalid JSON")
            ip = self._ip()
            if path == "/api/login":
                if AUTH.locked(ip):
                    return self._json(429, {"error": "Too many attempts, try again in a few minutes"})
                if AUTH.verify(str(body.get("password", ""))):
                    AUTH.clear_fails(ip)
                    return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(AUTH.create_session(), SESSION_TTL)})
                AUTH.fail(ip)
                time.sleep(0.4)  # slows down rapid-fire attempts
                return self._json(401, {"error": "Incorrect password"})
            if path == "/api/logout":
                AUTH.revoke(self._sid())
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
            if not self._authed():
                return self._json(401, {"error": "not authenticated"})
            if path == "/api/config":
                CTL.save_config(body)
            elif path == "/api/start":
                CTL.start()
            elif path == "/api/stop":
                CTL.stop()
            elif path == "/api/restart":
                CTL.restart()
            elif path == "/api/check":
                return self._json(200, {"checks": CTL.check_config()})
            elif path == "/api/notify/test":
                CTL.test_notify()
            elif path == "/api/stats/baseline":
                CTL.set_baseline(float(body.get("hours", 0)))
            elif path == "/api/stats/import":
                CTL.import_stats(body.get("data"))
            elif path == "/api/stats/reset":
                if body.get("confirm") is not True:
                    raise ValueError("confirmation requise")
                CTL.reset_stats()
            elif path == "/api/password":
                if AUTH.locked(ip):
                    return self._json(429, {"error": "Too many attempts, try again in a few minutes"})
                try:
                    AUTH.change_password(str(body.get("old", "")), str(body.get("new", "")), self._sid())
                except ValueError as exc:
                    if "incorrect" in str(exc):
                        AUTH.fail(ip)
                    raise
            elif path == "/api/logout_all":
                AUTH.revoke_all(self._sid())
            else:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"ok": True})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


KEEPALIVE = keepalive_url()


def _keepalive_loop(url: str) -> None:
    """Avoid sleep mode on hosts that shut down after X minutes without an HTTP visit."""
    while True:
        time.sleep(KEEPALIVE_EVERY)
        try:
            urllib.request.urlopen(url + "/healthz", timeout=15).read()
        except Exception:
            pass  # a 503 or a network error still counts as a visit


def main() -> None:
    global CTL, AUTH
    AUTH = Auth(reset="--reset-password" in sys.argv)
    if "--reset-password" in sys.argv:
        return
    CTL = Controller()
    if KEEPALIVE:
        threading.Thread(target=_keepalive_loop, args=(KEEPALIVE,), daemon=True).start()
        CTL.log(f"keep-alive active: pinging {KEEPALIVE}/healthz every {KEEPALIVE_EVERY // 60} min")
    if CTL.cfg.get("autostart") and not CTL.ready_error():
        CTL.log("automatic start")
        try:
            CTL.start()
        except Exception as exc:
            CTL.log(f"autostart failed: {exc}", "err")
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True

    def _term(*_a) -> None:  # docker stop / systemd: clean shutdown of the stream and stats
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _term)
        except Exception:
            pass
    sys.__stderr__.write(f"Stream Panel {VERSION} ready on http://{BIND}:{PORT}\n")
    try:
        srv.serve_forever()
    finally:
        CTL.stop(wait=True)
        try:
            with CTL.lock:
                _save_json(DATA_DIR / "stats.json", CTL.stats)
        except Exception:
            pass


if __name__ == "__main__":
    main()
