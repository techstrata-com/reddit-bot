# Reddit Bot Pipeline

Automated Reddit engagement pipeline for **Mara's weekly schedule**. Each run scrapes subreddits, ranks the best posts per group, generates AI comments, stores everything in **MongoDB**, and hands comments off to a **Telegram group** for manual posting on Reddit.

## Big picture

```
Mara weekly schedule (JSON)
        │
        ▼
  app.py or job daemon — pick today's groups
        │
        ▼
  For each group (A, B, C, …):
    Phase 1  Scrape subreddits (Apify)     → raw_posts
    Phase 2  Rank top N across group       → selected_posts
    Phase 3  Generate 1 LLM comment/post   → comments
    Phase 4  Send to Telegram (optional)   → comments (publish_status)
        │
        ▼
  Team posts on Reddit manually, taps Done in Telegram
```

**Example:** Friday (day 7) runs groups **C** and **H** with `top_n=3` → up to **6 comments** in MongoDB, then 6 Telegram handoff messages.

### Two ways to run

| Mode | When to use | Details |
|------|-------------|---------|
| **Manual CLI** (`app.py`) | Ad-hoc runs, debugging, partial phases, resume | [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md) |
| **Daily daemon** (`job.daemon`) | Hands-off production on a server | [JOB_GUIDE.md](JOB_GUIDE.md) |

## Weekly schedule

Mara days map to calendar weekdays (Los Angeles timezone for the daemon):

| Day # | Weekday | Groups | Notes |
|-------|---------|--------|-------|
| 1 | Saturday | A, H | |
| 2 | Sunday | B, I | |
| 3 | Monday | — | REST (pipeline exits) |
| 4 | Tuesday | C, E | |
| 5 | Wednesday | D, F | |
| 6 | Thursday | D, I | |
| 7 | Friday | C, H | |

Schedule data lives in `mara_schedule_structured_with_urls.json` (subreddit URLs, names, rules).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy and fill in `.env` (see [Environment variables](#environment-variables) below).

Verify MongoDB connectivity:

```bash
python check_db_access.py
```

### MongoDB profiles

**On the server** (MongoDB runs locally — no SSH):

```env
MONGODB_SSH_ENABLED=false
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DB=RedditBot
```

**On your Mac** (remote MongoDB via SSH tunnel):

```env
MONGODB_SSH_ENABLED=true
MONGODB_SSH_HOST=mdstudio.oriele.ai
MONGODB_SSH_USER=oriele
MONGODB_SSH_KEY_PATH=/Users/you/.ssh/id_ed25519
MONGODB_SSH_REMOTE_HOST=127.0.0.1
MONGODB_SSH_REMOTE_PORT=27017
MONGODB_DB=RedditBot
```

### Telegram setup (one time)

1. Create a bot via [@BotFather](https://t.me/BotFather) → set `TELEGRAM_BOT_TOKEN`.
2. Add the bot to your handoff Telegram group.
3. Register the group (pick one):
   - Start the daemon or `python telegram_bot.py` — the bot auto-registers groups it joins.
   - Or run once: `python resend_telegram.py --register-chat -1001234567890`
   - Or set `TELEGRAM_CHAT_ID` in `.env` to pin a single group.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APIFY_API_TOKEN` | Yes (scrape) | Apify API token for Reddit scraping |
| `LLM_PROVIDER` | No | `openai` (default) or `gemini` |
| `OPENAI_API_KEY` | If OpenAI | OpenAI API key |
| `OPENAI_MODEL` | No | Default `gpt-5.4` |
| `GEMINI_API_KEY` | If Gemini | Google Gemini API key |
| `GEMINI_MODEL` | No | Default `gemini-2.5-flash` |
| `LLM_TEMPERATURE` | No | e.g. `0.7` — omit to use provider default |
| `MONGODB_*` | Yes | See MongoDB profiles above |
| `TELEGRAM_BOT_TOKEN` | Phase 4 | Telegram bot token |
| `TELEGRAM_CHAT_ID` | No | Optional — send only to this group |
| `REDDIT_USERNAME` | No | Shown in Telegram handoff messages |
| `TELEGRAM_SEND_DELAY_SECS` | No | Delay between messages (default `1`) |
| `MARA_SCHEDULE_PATH` | No | Override path to schedule JSON |
| `JOB_TOP_N` | No | Daemon: posts per group (default `3`) |
| `JOB_WORKFLOW_HOUR` / `JOB_WORKFLOW_MINUTE` | No | Daemon: workflow trigger (default `00:01` LA) |
| `JOB_POLL_SECS` | No | Daemon poll interval (default `30`) |
| `JOB_LOG_DIR` | No | Log directory (default `../reddit-bot-logs`) |

## Quick start — manual runs

```bash
# Full pipeline for Friday, top 3 per group
python3 app.py 7 3

# Monday — exits immediately (REST day)
python3 app.py 3

# Scrape + rank only (no LLM)
python3 app.py 7 3 --skip-comments

# Full pipeline + send to Telegram in one command
python3 app.py 7 3 --send-telegram

# Resume: generate comments from a previous run
python3 app.py 1 --resume 20260617_090906 --group A --comments-only

# Resume: send existing comments to Telegram (dry-run first)
python3 app.py 1 --resume 20260608_122706 --send-telegram --dry-run
python3 app.py 1 --resume 20260608_122706 --send-telegram
```

Every new run prints a `run_key` (e.g. `20260608_122706`). Use it with `--resume` to continue later.

See [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md) for every CLI scenario.

## Quick start — automated daily job

Runs the full pipeline at **00:01 Los Angeles time**, then sends all comments to Telegram immediately. Listens for **Regenerate** / **Done** buttons 24/7.

```bash
python -m job.daemon              # foreground (workflow + Telegram + button listener)
./scripts/install_job.sh          # install as macOS launchd service
```

See [JOB_GUIDE.md](JOB_GUIDE.md) for daemon flags, logging, and troubleshooting.

## MongoDB collections

| Collection | What it stores |
|------------|----------------|
| `raw_posts` | Every scraped post (group, subreddit, run, full post data) |
| `selected_posts` | Top N ranked posts per group |
| `comments` | Generated comments + Telegram delivery status |
| `pipeline_runs` | Run metadata (day, status, stats) |
| `group_runs` | Per-group progress within a run |
| `job_runs` | Daily daemon state (workflow + Telegram status) |
| `telegram_chats` | Registered Telegram groups for handoff |

### Comment delivery status

| `publish_status` | Meaning |
|------------------|---------|
| `pending` | Generated, not yet sent to Telegram |
| `sent` | Delivered to Telegram, awaiting Reddit post |
| `published` | Team tapped **Done** after posting |
| `failed` | Send or validation error |

## Top-post selection

Across all posts scraped in a group:

1. Prefer posts with **non-empty title and body**
2. Sort by **`numberOfComments`** (desc)
3. Tiebreak with **`upVotes`** (desc)
4. Take top **N**

## Project structure

| File / folder | Purpose |
|---------------|---------|
| `app.py` | CLI entry point — full pipeline or individual phases |
| `pipeline_runner.py` | Programmatic runner used by the daemon |
| `scraper.py` | Apify Reddit actor (Phase 1) |
| `select_posts.py` | Group-level top-N ranking (Phase 2) |
| `comment_generator.py` | LLM prompt + OpenAI/Gemini call (Phase 3) |
| `comment_generator_prompt.py` | Prompt template |
| `telegram_notifier.py` | Telegram message formatting + send API |
| `telegram_sender.py` | Single-comment send helper |
| `telegram_bot.py` | Regenerate / Done button listener |
| `telegram_chats.py` | Group discovery and registration |
| `resend_telegram.py` | Retry unsent comments, register groups |
| `check_db_access.py` | Verify MongoDB / SSH tunnel |
| `schedule_loader.py` | Day → groups → subreddits mapping |
| `db/repository.py` | All MongoDB read/write |
| `db/connection.py` | MongoDB + optional SSH tunnel |
| `job/daemon.py` | Daily automated job |
| `actor_config.json` | Default Apify actor settings |
| `mara_schedule_structured_with_urls.json` | Weekly plan, URLs, rules |

## Documentation

| File | Contents |
|------|----------|
| [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md) | All manual `app.py` scenarios and MongoDB queries |
| [JOB_GUIDE.md](JOB_GUIDE.md) | Daemon, Telegram bot, resend, launchd, logs |
