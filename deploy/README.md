# AutoTrader — supervision (launchd), file logging, heartbeat, backups

Operational (V8c) artifacts that turn the paper-trading loop from "runs in a
terminal you have to babysit" into a supervised, self-restarting, logged, and
backed-up macOS service. Nothing here changes the trading logic — every order
still goes through the same risk core / entry gate / router as running
`python -m autotrader.main` by hand (RUNBOOK.md §4).

Files in this directory:

| File | Purpose |
|---|---|
| `com.autotrader.trader.plist` | launchd agent: the continuous trading loop (`autotrader.main`). `KeepAlive` + `RunAtLoad` — launchd restarts it if it dies, and starts it at login/boot. |
| `com.autotrader.webhook.plist` | launchd agent: the webhook signal ingress (`autotrader.signals.webhook`, Phase 2c-W). Same restart/boot behavior. Only needed if you use the webhook ingress instead of / alongside the file-drop inbox. |
| `com.autotrader.backup.plist` | launchd agent: nightly backup, calendar-triggered at 17:30 local (after the 16:30 ET EOD report and 16:15 ET EOD cancel-orders commit). Not `KeepAlive`/`RunAtLoad` — it's a one-shot job, not a service. |
| `backup_autotrader.sh` | The backup script itself: `VACUUM INTO`s the SQLite projection, copies the trade-audit journal(s), prunes anything older than 30 days. |

> **Labels are `com.autotrader.*`.** A separate, independent process — the SNP
> trading bot, its own repo — already owns `com.bot.trading` on this Mac. Do
> not reuse or collide with that label, and do not point any of these units
> at that bot's files.

---

## 1. Install

The plists reference an absolute repo path
(`/Users/acdc/Documents/AI/AutoTrader`) and `uv` at `/opt/homebrew/bin/uv`
(Homebrew's default Apple-Silicon prefix). **If you are installing on a
different machine, a different checkout path, or a different `uv` location,
edit `WorkingDirectory` and the `uv` path in each plist's `ProgramArguments`
before loading it.**

1. Copy the risk/secure config templates if you have not already (RUNBOOK.md
   §3) and fill in `config/risk.config` / `config/secure.config`. These are
   sourced by the plist's wrapper shell at process start — nothing secret or
   risk-related lives in the plists themselves.

   > **`FUTU_ACC_ID`:** RUNBOOK.md §3/§4 has you `export FUTU_ACC_ID=...` by
   > hand in an interactive shell. A launchd-supervised process has no such
   > shell, so add `FUTU_ACC_ID=<your SIMULATE acc_id>` as its own line in
   > `config/secure.config` (gitignored, sourced by the wrapper) instead —
   > otherwise `common.get_default_acc_id()` falls back to `0`, which is not
   > what you want running unattended.

2. Create the log directory (launchd will create the log *files* but not
   missing parent directories):

   ```bash
   mkdir -p ~/Library/Logs/autotrader
   ```

3. Make the backup script executable (already committed executable, but
   `chmod +x` again after copying/moving is harmless) and copy the plists
   into your per-user LaunchAgents directory:

   ```bash
   chmod +x deploy/backup_autotrader.sh
   mkdir -p ~/Library/LaunchAgents
   cp deploy/com.autotrader.trader.plist   ~/Library/LaunchAgents/
   cp deploy/com.autotrader.webhook.plist  ~/Library/LaunchAgents/   # only if you use the webhook ingress
   cp deploy/com.autotrader.backup.plist   ~/Library/LaunchAgents/
   ```

4. Load them (`-w` clears any previous "disabled" override — see Apple's
   launchctl docs; harmless on a first load):

   ```bash
   launchctl load -w ~/Library/LaunchAgents/com.autotrader.trader.plist
   launchctl load -w ~/Library/LaunchAgents/com.autotrader.webhook.plist   # optional
   launchctl load -w ~/Library/LaunchAgents/com.autotrader.backup.plist
   ```

   `RunAtLoad` is `true` on the trader/webhook units, so loading starts them
   immediately (in addition to at every future login/boot). The backup unit
   only runs at its scheduled time — load does not run it immediately.

---

## 2. Verify

```bash
# Confirm launchd has each job loaded and see its last exit status
# (0 = last run OK; a small non-zero "last exit status" alone right after a
# fresh RunAtLoad start is normal — check the actual PID/logs too):
launchctl list | grep com.autotrader

# Tail the rotating application log (written by autotrader.main itself when
# AUTOTRADER_LOG_DIR is set — independent of launchd's stdout/stderr capture):
tail -f ~/Library/Logs/autotrader/trader.log

# Tail launchd's own stdout/stderr capture for the trader:
tail -f ~/Library/Logs/autotrader/trader.stdout.log
tail -f ~/Library/Logs/autotrader/trader.stderr.log
```

`launchctl list | grep com.autotrader` should show three (or two, if you
skipped the webhook) rows with a PID in the first column for the
always-running units.

---

## 3. Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.autotrader.trader.plist
launchctl unload ~/Library/LaunchAgents/com.autotrader.webhook.plist
launchctl unload ~/Library/LaunchAgents/com.autotrader.backup.plist
rm ~/Library/LaunchAgents/com.autotrader.trader.plist \
   ~/Library/LaunchAgents/com.autotrader.webhook.plist \
   ~/Library/LaunchAgents/com.autotrader.backup.plist
```

Unloading a `KeepAlive` unit stops it and, unlike killing the process
directly, launchd will not restart it.

---

## 4. Dead-man heartbeat (optional)

Set `AUTOTRADER_HEARTBEAT_URL` (e.g. a healthchecks.io / cronitor / your own
"ping" endpoint) in `config/secure.config` — it's sourced by the plist's
wrapper shell like every other env var, so nothing needs to change in the
plist itself. When set, `autotrader.main` performs a 5-second-timeout GET
against that URL every `AUTOTRADER_HEARTBEAT_EVERY` loop iterations
(default `60`; at the default 5s loop interval that's roughly every 5
minutes — first ping fires on the very first iteration so a fresh start
pings immediately). A failed ping is logged as a warning and never kills the
trading loop (see `SessionRunner.run()` / `tests/test_heartbeat.py`).

This is a **dead-man's switch for the process being wedged or the machine
being down**, not a substitute for the Slack alerts (`AlertSink`) already
wired into halts / degraded loops / lifecycle-job failures — those two
mechanisms are complementary.

---

## 5. Power settings

A supervised trader is only as good as the machine staying awake and
connected. On the Mac running OpenD + the trader:

```bash
sudo pmset -a sleep 0 displaysleep 10
```

`sleep 0` disables system sleep entirely (the trader/OpenD keep running with
the lid closed / no interaction); `displaysleep 10` still lets the display
itself blank after 10 minutes to save power — it does not affect background
processes. If this machine runs on battery, keep it plugged in for any
supervised session: `-a` applies to both power sources, and running off
battery with sleep disabled will drain it while you're not watching.

---

## 6. Manual verification checklist

Run through this after installing, and again after any change to the plists
or the backup script. All four should be demonstrated in a real (unloaded
from a fresh terminal, not just "it compiled") session before relying on
this for anything beyond paper-trading supervision:

- [ ] **Kill the trader process → launchd restarts it.** Find the PID
      (`launchctl list | grep com.autotrader.trader`), `kill <pid>`, then
      re-run `launchctl list | grep com.autotrader.trader` a few seconds
      later and confirm a **new** PID appears (and `trader.stdout.log` shows
      a fresh startup banner: `TRADING_ENV=PAPER (paper-only v1)`).
- [ ] **Reboot → both agents come back.** Reboot the Mac, log in, and confirm
      (without manually starting anything) that `launchctl list | grep
      com.autotrader` shows the trader (and webhook, if installed) running,
      and their stdout logs show a fresh startup sequence.
- [ ] **Heartbeat URL shows pings.** With `AUTOTRADER_HEARTBEAT_URL` set,
      confirm the endpoint (e.g. healthchecks.io's "last ping" timestamp)
      updates roughly every `AUTOTRADER_HEARTBEAT_EVERY` iterations —
      cross-check against `trader.log`'s startup line logging the heartbeat
      URL and cadence.
- [ ] **Backup file appears after 17:30.** After the scheduled time (or after
      running `deploy/backup_autotrader.sh` by hand for a dry run), confirm
      `~/.autotrader_backups/` contains a fresh `autotrader-<date>.db` and
      the corresponding audit-journal copy, and that `sqlite3
      ~/.autotrader_backups/autotrader-<date>.db ".tables"` opens cleanly
      (a restore drill, not just a file-exists check).
