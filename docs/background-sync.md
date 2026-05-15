# Background Gmail Sync

By default, Compass only checks Gmail for application-status emails when you
explicitly trigger `/gmail/sync` in the web UI. For most users that's enough.

If you want Compass's funnel to stay fresh **even when the app is closed**,
install the OS-native background sync below.

---

## How it works

```
                ┌─ macOS  → launchd      ─┐
Schedule        ├─ Linux  → systemd timer ┤ → calls tools/gmail_sync_cli.py
(every 4 h)     └─ Windows → Task Scheduler ┘     ↓
                                            run_sync() — same code path
                                            as the /gmail/sync route
                                                  ↓
                                            updates SQLite, emits
                                            ReflectionEvents
                                                  ↓
                                            next time you open Compass,
                                            funnel is already current
```

**The Flask app does NOT need to be running.** The background job calls
Compass's classification engine directly via the standalone CLI.

---

## Prerequisites

Before installing the background sync, complete the one-time setup once:

1. Place a Google Cloud OAuth client secret at:
   ```
   data/credentials/gmail_client_secret.json
   ```
   (Follow [Google's Gmail API quickstart](https://developers.google.com/gmail/api/quickstart/python)
   to generate one — it's free.)

2. Open Compass and complete the OAuth flow once via the web UI:
   ```
   http://localhost:7000/gmail/setup
   ```
   This generates `data/credentials/gmail_token.json` which the background
   sync will reuse.

3. Make sure `LLM_API_KEY` is configured in `.env` (the background sync needs
   an LLM to classify emails).

---

## Install (pick your OS)

### macOS — launchd

```bash
cd /path/to/compass-intl
bash tools/launchd/install.sh
```

Output should end with:
```
✅ Installed and loaded com.compass.gmail-sync
   Next sync: in 4 hours (or whenever Mac wakes if asleep)
```

**Trigger an immediate test sync:**
```bash
launchctl kickstart -k gui/$(id -u)/com.compass.gmail-sync
tail -20 data/logs/gmail-sync.out.log
```

**Uninstall:**
```bash
bash tools/launchd/uninstall.sh
```

### Linux — systemd user timer

```bash
cd /path/to/compass-intl
bash tools/systemd/install.sh
```

Verify the timer is active:
```bash
systemctl --user list-timers | grep compass
```

**Trigger immediate test:**
```bash
systemctl --user start compass-gmail-sync.service
journalctl --user -u compass-gmail-sync.service -n 50
```

**Uninstall:**
```bash
bash tools/systemd/uninstall.sh
```

### Windows — Task Scheduler

Open Command Prompt (no admin needed) and:
```cmd
cd C:\path\to\compass-intl
tools\windows-task-scheduler\install.bat
```

**Trigger immediate test:**
```cmd
schtasks /Run /TN "CompassGmailSync"
type data\logs\gmail-sync.out.log
```

**Uninstall:**
```cmd
tools\windows-task-scheduler\uninstall.bat
```

---

## Tuning the schedule

The default 4-hour interval suits most users. To change it:

| OS | Edit | Default | Variable |
|---|---|---|---|
| macOS | `~/Library/LaunchAgents/com.compass.gmail-sync.plist` | 14400 sec | `<StartInterval>` |
| Linux | `~/.config/systemd/user/compass-gmail-sync.timer` | 4h | `OnUnitActiveSec=` |
| Windows | Task Scheduler → CompassGmailSync → Triggers | 4h | Repeat interval |

After editing, reload:
```bash
# macOS
launchctl unload ~/Library/LaunchAgents/com.compass.gmail-sync.plist
launchctl load -w ~/Library/LaunchAgents/com.compass.gmail-sync.plist

# Linux
systemctl --user daemon-reload && systemctl --user restart compass-gmail-sync.timer
```

(Windows: edits via the Task Scheduler GUI apply immediately.)

---

## Troubleshooting

### Sync runs but no Gmail data appears in Compass

Check the logs:
```bash
tail -50 data/logs/gmail-sync.err.log
```

Common causes:
- **OAuth token expired** — re-run `/gmail/setup` in the web UI
- **`LLM_API_KEY` not set** — check `.env`
- **Wrong `data_dir`** — the CLI reads `COMPASS_DATA_DIR` from `.env`; make sure
  it matches the directory that contains your `credentials/` folder

### "Database is locked" errors

The Flask app and the background job both write to the same SQLite database.
SQLite WAL mode plus the 30-second busy_timeout in `compass/extensions.py`
prevents this in practice, but if you see lock errors:
- Stop the Flask app while the sync runs (close `compass.run`), or
- Migrate to PostgreSQL (set `COMPASS_DB_URL=postgresql://...` in `.env`)

### Job didn't fire when expected (laptop closed / asleep)

- **macOS**: launchd's `StartInterval` *skips* fires when sleeping. The next
  fire happens when you wake the laptop (or after the interval if it's
  been awake).
- **Linux**: `Persistent=true` in the timer catches missed fires on next boot.
- **Windows**: `<StartWhenAvailable>true` in the task XML catches missed fires.

---

## Removing the background sync

Each OS has a matching `uninstall.{sh,bat}` next to its `install`. Run it and
the schedule entry is gone. The standalone CLI script remains — you can still
trigger manual syncs.

---

## Comparison with similar projects

| Tool | Background Gmail sync? |
|---|---|
| CareerSync | ❌ Stateless — only syncs when you open the app |
| JustAJobApp / jobseeker-analytics | ❌ Backend on-demand |
| gmail-job-application-tracker | ❌ Manual run only |
| **Compass** (this project) | ✅ All 3 OS supported, native scheduler |

Background sync is Compass's "you don't have to remember" differentiator. It
matches the reflection-system philosophy: the tool runs quietly in the
background so you don't optimize *for* the tool.
