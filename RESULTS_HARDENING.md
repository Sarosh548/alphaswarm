# AlphaSwarm — Breakout Bot Kind + Hardening / Deploy Readiness

Built 2026-11-19. Paper trading only, as always: no real-money code
was added, no exchange keys exist anywhere, no OS schedules/crons
were created (the backup and bot schedulers are in-app threads).
`~/workspace/alpha-paper-forward/` and all other project folders were
only read from, never written.

## JOB 1 — Breakout as a platform bot kind

**Signal source (copied, logic verbatim):**
- `engine/breakout.py` — Donchian-20 fresh-cross breakout on closed
  1h bars, ATR top-decile volatility guard (percentile ≥ 0.90 of the
  trailing 200 ATR14 values blocks; < 50 defined values fails
  closed), copied from the proven breakout study / forward runner.
- `engine/htf.py` — the study's own fail-closed 4h trend labels
  (EMA50 vs EMA200 on fully-closed 4h candles only; "unknown" blocks).
- `engine/engine_breakout.py` — the live walk, mirroring the forward
  runner: fetch → exit watcher → signal per symbol → the platform's
  deterministic risk policy (≤ configured risk % per trade, max 2
  open, daily-loss halt, stale-data refusal, kill switch) → paper
  fill at the signal bar's close. One trade per signal bar: a re-run
  inside the same hour cannot duplicate a trade
  (`Journal.trade_exists_for_signal`, added to the platform journal).
- Plain-language reasons for every breakout trade, e.g. *"Why this
  trade: the price closed at $X, ABOVE the highest high of the last
  20 hours ($Y) — a Donchian breakout… The 4-hour trend is UP…
  Volatility check passed… Risk Manager approved it: risking $…
  Stop loss $… (1.5x the average hourly range), first target $…,
  final target $…. Paper trade — no real money."*

**Wiring:** `botrunner` routes kind `breakout` to the breakout cycle
(screener universe mode works for it exactly as for technical bots,
fail-closed); the create-bot API accepts kind `"breakout"`; the admin
create-bot form offers it; bot detail / live trades / comparison /
kill switches work for it unchanged. The agent-based "test position"
button is refused for breakout bots (it would show the wrong agents'
votes) with a plain explanation.

**Stated compromise (exits):** the study's entry geometry is kept
exactly — stop = 1.5 × ATR14, final target = 2R, 0.1% fee per side.
But exits run through the platform execution engine, so the TP1
partial booking at 1R (50%), the break-even stop move and the
trailing stop also apply to breakout trades, as they do for every
other platform bot. That means this bot's live results are *the
study's entries under platform execution*, not a bar-for-bar
replication of the study's exits. The pure study record continues
untouched in the frozen forward runner
(`~/workspace/alpha-paper-forward/breakout/`), which remains the
honest +12.60%-after-fees shared-account record this candidate's
reputation rests on.

**Demo bot (demo instance only, port 8001):** bot #5
**"Breakout — Leading Candidate"** created as admin, one live cycle
run, `cycle_minutes` set to 60, status **running** — it now builds a
forward record alongside bot #4 during the week.

Live cycle outcome (2026-11-19, ~21:35 PKT): **no trade — honestly.**
All three symbols were inside their Donchian channels with the 4h
trend down:

| Symbol | Price context | Signal | 4h trend |
|---|---|---|---|
| BTCUSDT | inside channel | `inside` | down |
| ETHUSDT | inside channel | `inside` | down |
| SOLUSDT | 73.57 | `inside` | down |

Equity $10,000.00, 0 open / 0 closed. A breakout bot that opens
nothing on a no-breakout hour is behaving correctly; the mocked-cycle
tests below prove the open path end to end.

## JOB 2 — Hardening + deploy readiness

**Backups (`backup.py`, new):** an in-app thread takes a backup at
startup and then daily — platform DB + every bot journal copied with
SQLite's backup API (consistent snapshots of live databases) plus
the news/alert logs and a manifest, into
`<data_dir>/backups/YYYY-MM-DD/`; only the newest 7 dated folders are
kept. Admin endpoints: `POST /api/admin/backup` (create now) and
`GET /api/admin/backup/download` (zip of the latest), plus a
*Backups* card in the Admin tab.

*Proof:* after the restart both instances wrote
`backups/2026-11-19/` on their own — main: platform.db + logs
(3 files; main has no bots yet); demo: 7 files including all bot
journals. A second admin-triggered demo backup after the breakout
bot was created contains all 5 bot journals
(`bots/1..5/journal.db`). Retention pruning is unit-tested
(10 dated folders → newest 7 kept).

**Terms + Privacy (`static/index.html`):** two new SPA pages
(Terms of Service, Privacy Policy) in plain language: paper-trading
only, no financial advice, **no profit guarantee**, TEST-MODE billing
(no real charges, no full card numbers ever stored), data collected
(email, name, hashed password, paper activity, card last-4 in test
records only), what is never collected (exchange keys, wallet keys,
real payment details), session-cookie-only, backups/retention,
account deletion route, and a clearly-marked placeholder contact
(`support@alphaswarm.example`) to replace before launch. A footer
with both links sits under every app screen (Home and Billing
included).

**Stability:** the scheduler was refactored into `scheduler_tick()`
with a per-bot try/except — one bot's exception can no longer skip
or kill the other bots in the same pass (run_cycle already converts
engine errors into summaries; this is the second line of defence).
Proven by a test that injects a raising cycle for bot #1 and asserts
bot #2 still runs and paused bot #3 is skipped. HTTP access logs now
go to `<data_dir>/logs/access.log` under a RotatingFileHandler
(1 MB × 3 files ≈ 4 MB hard cap) instead of growing forever.

**Deploy package (`deploy/`):**
- `alphaswarm.service` — systemd unit (dedicated `alphaswarm` user,
  data dir env, restart-on-failure, journal logging, basic sandboxing).
- `Caddyfile` — reverse proxy to 127.0.0.1:8000 with automatic
  Let's Encrypt HTTPS for a placeholder domain and capped Caddy logs.
- `DEPLOY_RUNBOOK.md` — blank Ubuntu VPS → live site, following
  DEPLOY_PLAN.md: firewall (app port never public), code copy,
  systemd, DNS, Caddy, **admin-account-first bootstrap before going
  public**, go-live checks, day-to-day ops, backup restore steps,
  and the explicit non-goals (no live trading, no real billing until
  a merchant of record is connected).

## Restarts & data preservation

Both running instances were restarted onto the new code (main :8000,
demo :8001; both HTTP 200). A before/after snapshot of the demo
instance — all 4 bots (ids, kinds, statuses, cycle settings,
published flags), both users, every trade, and per-bot cycle counts —
was compared after the restart: **identical.** Bot #4 (All Coins,
running, 60-min screener cycle, open UNI short) was not altered in
any way; the only demo change afterwards is the new bot #5 above,
created through the normal admin API.

## Tests

Full acceptance suite: **91/91 checks passed** (was 57/57 for the
platform + all-coins builds; +34 new). New coverage:

- *Breakout signal (7):* fresh cross fires long; geometry
  SL = entry − 1.5×ATR / TP = entry + 2R; inside → no proposal;
  trend-down and trend-unknown both block (fail closed); ATR top
  decile blocks (vol guard); undefined ATR blocks.
- *HTF labels (3):* rising → up, falling → down, short history →
  unknown.
- *Breakout engine, mocked (6):* cycle opens exactly one trade with
  the study geometry (entry 103, SL 100, TP 109, TP1 106), stores
  the plain-language reason, charges the 0.1% entry fee; same signal
  bar re-run opens no duplicate; 4h-trend-down cycle opens nothing.
- *Hardening (8):* backup folder + platform DB; every bot journal
  included; zip download bytes; 7-day retention; scheduler fault
  isolation; legal pages present; legal basics stated; log rotation
  + in-app backup wiring present.
- *HTTP (10):* breakout bot create / unknown kind still 400 /
  detail loads / live cycle runs ("Cycle done…") / comparison covers
  it / agent test-position refused; backup create (admin) /
  backup create forbidden for non-admin / backup zip download /
  served SPA carries Terms + Privacy.

## Limitations / notes

- The demo breakout bot has no live trade yet — breakouts are
  events, and its first hour had none. Its forward record starts
  from zero, like every honest bot here.
- Platform breakout ≠ pure study exits (see the compromise above);
  judge it on its own forward record, not on the study's numbers.
- Backups protect against data loss on the same machine only; the
  runbook's production step should also copy them off-server (left
  as an ops note in the runbook flow — download endpoint makes this
  one click for the admin).
- Terms/Privacy are plain-language drafts, not lawyer-reviewed;
  the runbook flags replacing the placeholder contact before launch.
