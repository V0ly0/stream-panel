# Stream Panel 3.0

Web panel + 24/7 supervisor for the **stream** part of *discord-badge-maximizer* (`stream.py`, **unmodified**).
Version 3.0 merges *stream-panel v2* (technical base, security, tests) with *discord-stream-host* (schedule, goal, backup, keep-alive).

## Features

- **Setup from the browser**: token, voice channel, alt account, mode; **"Test configuration"** (token, channel, alt, ffmpeg) without starting the stream.
- **Start / Stop / Restart**, automatic start on boot, automatic restart with progressive back-off (5 s → 5 min, jitter), immediate stop on a configuration error instead of looping.
- **Hours goal** with automatic stop (shortcuts for the 10 tiers) and an estimated completion date.
- **Schedule**: time window (including windows crossing midnight), weekdays, timezone. Outside the window the stream pauses and resumes by itself.
- **Statistics**: viewers (and peak), session / day / total, 7-day rate and coverage, estimated date of the next tier, memory and load, 14 / 30 / 90-day chart.
- **Session history** (duration, counted time, peak, end reason) and CSV export.
- **Backup**: JSON export / import of the statistics (the export of the old *discord-stream-host* is accepted), manual total adjustment, reset.
- **Webhook notifications** (Discord, ntfy…): config error, repeated restarts, stalled stream or no viewer, tier, goal, start (optional).
- **Log** with filters and download, also written to `data/panel.log` (rotating); tokens masked everywhere.
- **Security**: hashed password (scrypt) changeable in the UI, revocable sessions, per-IP lockout, anti-CSRF origin check, nonce-based CSP, webhooks refused towards private networks, data files in `0600`, non-root container without privileges.
- Optional **keep-alive** (self-ping of `/healthz`), light/dark theme, dynamic tab title, clean shutdown on `docker stop`.
- Panel in **pure Python standard library**; ready-to-use Docker; 33 tests.

> ⚠️ **Account risk.** Automating a user account and faking a stream violates Discord's Terms of Service and can get the account suspended. Preferably use a secondary account. The token gives full access to the account: never share it.

## Quick start (Docker)

```bash
echo "PANEL_PASSWORD=choose-a-long-password" > .env
docker compose up -d --build
```

The panel listens on `127.0.0.1:7860` (see "Secure access"). In the **Settings** tab:

1. paste the **token** of the streaming account and the **voice channel ID** (or its link);
2. paste the **token of an alt account**: without a viewer in the channel, the counter stays paused;
3. **Save**, then **Test configuration**, then **Start** (Dashboard).
4. Optional: **Goal and schedule**.

Changing the token, channel or mode while the stream is running requires a **Restart** (the panel tells you). Goal, schedule and timezone apply immediately.

Without Docker: `pip install -r requirements.txt && python panel.py` (Python 3.10+, ffmpeg only for video mode).

Discord prerequisites: the account must be able to *View / Connect / Video* in the channel, and "Use data to personalize my experience" must be enabled.

## Modes

| Mode | What is sent | CPU |
|---|---|---|
| **AFK** (default) | silent audio, no video | ~0 % |
| **Video** | `assets/video.mp4` looped through ffmpeg (H.264 480p) | high |

AFK mode relies on the original engine's "silence" fallback; its real effect on the badge on Discord's side could not be tested.

## Goal and schedule

- **Goal**: when reached, the stream stops and a notification is sent. Set `0` to remove it. Starting while the goal is already reached is refused.
- **Schedule**: "22:00 → 06:00" belongs to the day the window starts on (ticking *Fri* covers Friday 22:00 until Saturday 06:00). Start = end means "the whole day". "Stop" also cancels a scheduled resume.
- **Timezone**: used for the schedule and for splitting statistics into days. Days already recorded keep their original date.

## "Free 24/7" hosting

The engine keeps an *outgoing* connection to Discord, with no incoming request. A host that puts the service to sleep after ~15 min without an HTTP visit would therefore stop it.

| Option | 24/7? | Notes |
|---|---|---|
| **Oracle Cloud "Always Free"** (VM) | Yes | Credit card required at sign-up. ARM offer reduced to 2 vCPU / 12 GB since 15 June 2026, plenty in AFK mode. `dave.py` wheels available for aarch64. |
| **PC / Raspberry Pi / mini-PC** | Yes | Cost: electricity. The most discreet option (see "Datacenter IP"). |
| **Render (free plan)** | Only thanks to keep-alive | `render.yaml` provided. The panel pings itself every 9 min (disable with `KEEPALIVE=0`). **Temporary** disk: export your stats before a redeploy. Working around the sleep mode goes against the spirit of the offer: your call. |

Free offers change fast: check the current terms.

**Datacenter IP.** Logging into an account from a cloud host's IP, very different from your usual one, easily triggers verifications or account restrictions. A Raspberry Pi at home avoids that problem.

## Secure access (important)

The panel handles Discord tokens: never expose it over plain HTTP on the Internet.

- **Simplest:** SSH tunnel, `ssh -L 7860:localhost:7860 user@your-server`, then `http://localhost:7860`.
- **Access from anywhere:** Cloudflare Tunnel, Tailscale, or an HTTPS reverse proxy (Caddy). Behind a trusted proxy, set `TRUST_PROXY=1` (`Secure` cookie, real client IP).
- **Password:** `PANEL_PASSWORD` (not editable in the UI); otherwise a password is generated **once**, shown in `docker compose logs`, stored hashed and changeable in Settings → Security. Lost it: `docker compose run --rm stream-panel python panel.py --reset-password`.
- 8 failures within 10 min lock out the login of the IP concerned; "Log out other devices" revokes the other sessions.
- Tokens are never returned by the API and are masked in the log. They are stored in clear text in the `/data` volume (`session.json`): protect that folder.

## Environment variables (all optional)

`PANEL_PASSWORD`, `DISCORD_TOKEN`, `ALT_TOKEN`, `CHANNEL_ID`, `STREAM_MODE` (`afk`/`video`), `AUTOSTART` (`1`/`0`), `NOTIFY_WEBHOOK`, `TRUST_PROXY`, `KEEPALIVE_URL`, `KEEPALIVE` (`0` to turn off), `PORT`, `BIND`, `DATA_DIR`. Example in `.env.example`.

## Good to know

- The "counted hours" are a **local estimate** (real time with at least one viewer). Discord updates the badge within 24 to 48 h.
- The date estimate uses your actual rate over the last 7 days: it moves if the stream is interrupted.
- "Hours already accumulated" (Settings → Statistics) realigns the bar if you already had hours before the panel.
- Docker does not restart an "unhealthy" container by itself: the "stalled" webhook warns you, or use `autoheal`.
- If another device is already connected to voice with this account, the original engine uses that channel.

## Migrating from *discord-stream-host*

In the old panel: Settings → Statistics → **Export**. In this one: Settings → Statistics → **Import**. Totals, sessions and days are carried over; token, channel and schedule must be re-entered (for security reasons, the export never contains tokens).

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use a fake engine (no call to Discord): authentication, sessions, anti-CSRF, CSP, start/stop cycle, fatal error, token masking, progress/ETA, schedule (including overnight windows), goal with automatic stop, timezones, export/import. The engine's real behaviour against Discord cannot be tested here.

## Files

`panel.py` · `ui.html` · `stream.py` (original engine, MIT) · `Dockerfile` · `docker-compose.yml` · `render.yaml` · `tests/`

## Credits

`stream.py` engine: *discord-badge-maximizer* v1.3.0 (MIT, see `LICENSE`), credited author: sonuworks.
