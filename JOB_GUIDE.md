# Daily Job Guide

Automated daily pipeline + scheduled Telegram handoff for human Reddit posting.

## What the job does

| Time (Los Angeles) | Action |
|---|---|
| **00:01** | Run `python app.py {day} 3` for today's Mara schedule day |
| **8:00 AM – 4:00 PM** (hourly slots) | Send generated comments to Telegram at one picked slot |
| **24/7** | Listen for **Regenerate** / **Done** button presses |

### Day mapping (automatic)

| Calendar day | Mara day | Command equivalent |
|---|---|---|
| Saturday | 1 | `python app.py 1 3` |
| Sunday | 2 | `python app.py 2 3` |
| Monday | 3 | REST — workflow skipped, logged |
| Tuesday | 4 | `python app.py 4 3` |
| Wednesday | 5 | `python app.py 5 3` |
| Thursday | 6 | `python app.py 6 3` |
| Friday | 7 | `python app.py 7 3` |

### Telegram slot rules

- One **hourly slot** is picked per day between **8:00 AM and 4:00 PM** LA time.
- If yesterday had a successful Telegram send, **today's slot will not repeat yesterday's hour**.
- All comments for that day's run are sent when the slot arrives (spaced by `TELEGRAM_SEND_DELAY_SECS`).

### Telegram message format

```
Reddit account: u/your_username

Comment:
[copyable comment text]

Post URL:
[copyable url]

[Regenerate] [Done]
```

- **Regenerate** — new LLM comment, message updated in Telegram
- **Done** — marks comment as published on Reddit (team confirmed manual post)

## Setup on the server

When the bot runs on the **same machine as MongoDB** (`/home/oriele/reddit-bot`), disable SSH in `.env`:

```env
MONGODB_SSH_ENABLED=false
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DB=RedditBot
```

Then verify:

```bash
python check_db_access.py
python -m job.daemon --tick
```

## Setup (one time)

### 1. Environment (`.env`)

```env
REDDIT_USERNAME=your_reddit_username
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-5391717887

# Optional job tuning
JOB_TOP_N=3
JOB_WORKFLOW_HOUR=0
JOB_WORKFLOW_MINUTE=1
JOB_TELEGRAM_SLOT_START_HOUR=8
JOB_TELEGRAM_SLOT_END_HOUR=16
JOB_POLL_SECS=30
JOB_LOG_DIR=/path/to/logs   # default: ../reddit-bot-logs next to project
```

### 2. Run the daemon manually (testing)

```bash
# Foreground — workflow + telegram scheduler + button listener
python -m job.daemon

# One scheduler check now
python -m job.daemon --tick

# Force today's workflow (ignore idempotency)
python -m job.daemon --force-workflow

# Force send Telegram now (after workflow completed)
python -m job.daemon --force-telegram
```

### 3. Install as macOS service (runs every day automatically)

```bash
chmod +x scripts/install_job.sh
./scripts/install_job.sh
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.redditbot.job
rm ~/Library/LaunchAgents/com.redditbot.job.plist
```

## Logs

One file per calendar day (LA timezone):

```
../reddit-bot-logs/
  2026-06-23.log
  2026-06-24.log
  daemon.stdout.log
  daemon.stderr.log
```

Each daily log records:

- Workflow start/finish and duration
- Mara day number and run key
- Telegram slot selection (and yesterday's excluded slot)
- Scheduled vs actual Telegram send time
- Messages sent / failed
- Published count (when team taps **Done**)
- Errors with stack traces

## MongoDB `job_runs` collection

One document per calendar day tracks the full job state:

```javascript
db.job_runs.find({ calendar_date: "2026-06-23" })
```

Fields include `workflow_status`, `workflow_run_key`, `telegram_slot_hour`, `telegram_scheduled_at`, `telegram_status`, `telegram_comments_sent`, `telegram_comments_published`.

## Manual vs automated

| Manual | Automated job |
|---|---|
| `python app.py 5 3` | Same at 00:01 LA on Wednesday |
| `python app.py 5 3 --send-telegram` | Telegram at scheduled slot (not immediately) |
| `python telegram_bot.py` | Built into daemon (background thread) |

The manual CLI still works for ad-hoc runs and debugging.
