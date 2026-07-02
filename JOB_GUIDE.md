# Daily Job Guide

Automated daily pipeline + Telegram handoff for human Reddit posting.

For manual `app.py` runs and all CLI scenarios, see [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md). For project overview, see [README.md](README.md).

---

## What the daemon does

The daemon (`python -m job.daemon`) runs three things in one process:

1. **Daily workflow** at **00:01 Los Angeles time** — equivalent to `python app.py {day} 3`
2. **Telegram handoff** immediately after the workflow finishes — all generated comments sent at once
3. **Button listener** (background thread) — handles **Regenerate** / **Done** on Telegram messages 24/7

```
00:01 LA  →  scrape + rank + generate comments (all groups for today)
     ↓
immediately  →  send all comments to Telegram (spaced by TELEGRAM_SEND_DELAY_SECS)
     ↓
24/7  →  listen for Regenerate / Done button presses
```

The team posts on Reddit whenever they want. They tap **Done** in Telegram after publishing.

### Day mapping (automatic)

| Calendar day (LA) | Mara day | Equivalent command |
|-------------------|----------|-------------------|
| Saturday | 1 | `python app.py 1 3` |
| Sunday | 2 | `python app.py 2 3` |
| Monday | 3 | REST — workflow skipped, logged |
| Tuesday | 4 | `python app.py 4 3` |
| Wednesday | 5 | `python app.py 5 3` |
| Thursday | 6 | `python app.py 6 3` |
| Friday | 7 | `python app.py 7 3` |

---

## Telegram setup

### 1. Create bot and add to group

1. Create a bot via [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN` in `.env`
2. Add the bot to your handoff Telegram group
3. Set `REDDIT_USERNAME` so messages show which account to post from

### 2. Register the group

The bot auto-discovers groups when it sees join events or messages. If the bot was added before the daemon started, register once:

```bash
# Find chat ID: add @userinfobot or @RawDataBot to the group
python resend_telegram.py --register-chat -1001234567890

# List registered groups
python resend_telegram.py --list-chats
```

**Optional:** set `TELEGRAM_CHAT_ID` in `.env` to send only to that one group (useful for testing).

### 3. Telegram message format

**Before Done:**

```
Day: Saturday (day 1)
Group: A — Group title

Reddit account: u/your_username

Comment:
[copyable comment text]

Post URL:
[copyable url]

[Regenerate] [Done]
```

**After Done** (message edited, buttons removed):

```
...
Status: ✅ Published
Posted by: @username
```

- **Regenerate** — calls the LLM again, updates the same Telegram message
- **Done** — records who clicked in MongoDB, marks comment as `published`

---

## Environment variables

### Required for the daemon

```env
APIFY_API_TOKEN=...
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4
LLM_TEMPERATURE=0.7          # optional

TELEGRAM_BOT_TOKEN=...
REDDIT_USERNAME=your_reddit_username

# MongoDB (server profile)
MONGODB_SSH_ENABLED=false
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DB=RedditBot
```

### Optional tuning

```env
TELEGRAM_CHAT_ID=-1001234567890    # pin to one group
TELEGRAM_SEND_DELAY_SECS=1         # delay between Telegram messages

JOB_TOP_N=3                        # posts per group (default 3)
JOB_WORKFLOW_HOUR=0                # workflow trigger hour (LA, default 0)
JOB_WORKFLOW_MINUTE=1              # workflow trigger minute (default 1)
JOB_POLL_SECS=30                   # scheduler poll interval
JOB_LOG_DIR=/path/to/logs          # default: ../reddit-bot-logs
```

### Gemini instead of OpenAI

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

---

## Running the daemon

### Foreground (testing)

```bash
python -m job.daemon
```

Starts workflow scheduler, Telegram sender, and button listener in one process.

### One-off commands

```bash
# Run one scheduler tick (workflow if due, or retry pending Telegram)
python -m job.daemon --tick

# Force today's workflow now (ignores idempotency)
python -m job.daemon --force-workflow

# Force Telegram send for today's completed workflow
python -m job.daemon --force-telegram
```

### Button listener only

If you only need Regenerate/Done handling without the scheduler:

```bash
python telegram_bot.py
```

---

## Install as macOS service

```bash
chmod +x scripts/install_job.sh
./scripts/install_job.sh
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.redditbot.job
rm ~/Library/LaunchAgents/com.redditbot.job.plist
```

---

## Retry unsent Telegram messages

If comments were generated but never reached Telegram:

```bash
python resend_telegram.py --dry-run
python resend_telegram.py --run-key 20260626_070107
python resend_telegram.py --day 1
python resend_telegram.py --group A
```

The daemon also retries automatically on each tick if workflow completed but Telegram send did not finish.

---

## Logs

One log file per calendar day (LA timezone):

```
../reddit-bot-logs/
  2026-06-23.log
  2026-06-24.log
  daemon.stdout.log
  daemon.stderr.log
```

Each daily log records:

- Workflow start/finish and duration
- Mara day number and `run_key`
- Telegram send results (sent / failed)
- Published count (when team taps **Done**)
- Errors with stack traces

---

## MongoDB `job_runs` collection

One document per calendar day tracks daemon state:

```javascript
db.job_runs.find({ calendar_date: "2026-06-23" })
```

Key fields:

| Field | Description |
|-------|-------------|
| `workflow_status` | `pending` / `running` / `completed` / `skipped` / `failed` |
| `workflow_run_key` | Pipeline `run_key` for that day |
| `telegram_status` | `pending` / `running` / `sent` / `partial` / `failed` / `skipped` |
| `telegram_comments_sent` | Count delivered to Telegram |
| `telegram_comments_published` | Count marked Done by team |

---

## Manual vs automated

| Task | Manual | Automated daemon |
|------|--------|------------------|
| Run pipeline | `python app.py 5 3` | Same at 00:01 LA on Wednesday |
| Send to Telegram | `python app.py 5 --resume <KEY> --send-telegram` | Immediately after workflow |
| Regenerate / Done | `python telegram_bot.py` | Built into daemon (background thread) |
| Retry failed sends | `python resend_telegram.py` | Auto-retry on each tick |

The manual CLI remains available for ad-hoc runs, partial phases, and debugging. See [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md).

---

## Server setup checklist

When running on the same machine as MongoDB (`/home/oriele/reddit-bot`):

```bash
# 1. .env with MONGODB_SSH_ENABLED=false and local URI
# 2. Verify DB
python check_db_access.py

# 3. Test workflow manually once
python app.py 7 3 --group C

# 4. Register Telegram group
python resend_telegram.py --register-chat -1001234567890

# 5. Start daemon
python -m job.daemon
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No Telegram groups found" | Register group: `python resend_telegram.py --register-chat <ID>` |
| Workflow ran but no Telegram | `python -m job.daemon --force-telegram` or `python resend_telegram.py` |
| MongoDB connection fails | Run `python check_db_access.py`, check SSH key / `MONGODB_URI` |
| Monday shows "skipped" | Expected — Monday is REST in Mara schedule |
| Regenerate not working | Ensure daemon or `telegram_bot.py` is running |
| Duplicate workflow same day | Daemon skips if `workflow_status` is already `completed` — use `--force-workflow` to override |
