# Reddit Bot Pipeline

Scrapes Reddit posts for Mara’s weekly schedule, selects the top posts per **group**, generates AI comments, and stores everything in **MongoDB**. Phase 4 sends each generated comment to a Telegram group for manual posting.

## How it works

```
python app.py 7 3
        │
        ▼
  weekly_activity_plan  →  groups for that day
        │
        ▼
  For each group:
    scrape all subreddits → save posts to MongoDB
        │
        ▼
    select top N across the group → save selections
        │
        ▼
    generate 1 comment per selected post → save comments
        │
        ▼
    (optional) send each comment + post URL to Telegram
```

**Example:** Friday (day 7) runs groups C & H with `top_n=3` → **6 comments** stored in MongoDB.

## Day numbers

| # | Day | Groups |
|---|-----|--------|
| 1 | Saturday | A, H |
| 2 | Sunday | B, I |
| 3 | Monday | REST (off) |
| 4 | Tuesday | C, E |
| 5 | Wednesday | D, F |
| 6 | Thursday | D, I |
| 7 | Friday | C, H |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env`:

```env
# MongoDB via SSH tunnel (remote server)
MONGODB_SSH_ENABLED=true
MONGODB_SSH_HOST=mdstudio.oriele.ai
MONGODB_SSH_USER=oriele
MONGODB_SSH_KEY_PATH=/Users/borhan/Desktop/keys/id_ed25519
MONGODB_SSH_REMOTE_HOST=127.0.0.1
MONGODB_SSH_REMOTE_PORT=27017
MONGODB_DB=RedditBot

# Or direct connection (disable SSH):
# MONGODB_SSH_ENABLED=false
# MONGODB_URI=mongodb://localhost:27017

APIFY_API_TOKEN=...

LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4

# Telegram handoff (Phase 4)
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_IDS={"A":"-1001234567890","H":"-1009876543210"}
TELEGRAM_SEND_DELAY_SECS=1
```

Add the bot to each Telegram group and use the group's chat ID in `TELEGRAM_CHAT_IDS`. Keys are pipeline group IDs (`A`, `B`, `C`, …).

## Run

```bash
python3 app.py 7 3          # Friday, top 3 per group
python3 app.py 1            # Saturday, top 3 (default)
python3 app.py 3            # Monday — exits (REST)
python3 app.py 7 3 --skip-comments   # scrape + select only

# Resume: generate comments for group A from existing run
python3 app.py 1 --resume 20260617_090906 --group A --comments-only

# Send generated comments to Telegram (dry-run first)
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram --dry-run
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram
```

## MongoDB collections

| Collection | What it stores |
|---|---|
| `raw_posts` | **All scraped posts** — includes `day_number`, `day_name`, `group_id`, full post data |
| `selected_posts` | **Top N per group** — rank, selection criteria, day + group info |
| `pipeline_runs` | Run metadata (when, status, stats) |
| `group_runs` | Per-group progress (scraping → selecting → done) |
| `comments` | Generated LLM comments + delivery status (`pending` / `sent` / `failed`) |

### Flow

```
Phase 1: scrape all subreddits → save each batch to raw_posts
Phase 2: rank across group      → save top N to selected_posts
Phase 3: generate comments      → save to comments (optional)
Phase 4: send to Telegram       → comment text + post URL per message
```

### Telegram message format

Each message sent in Phase 4:

```
{generated comment}

{reddit post url}
```

### Example queries

```javascript
// All raw posts for Saturday (day 1)
db.raw_posts.find({ day_number: 1 })

// Selected posts for a specific run
db.selected_posts.find({ run_key: "20260608_120000" }).sort({ group_id: 1, rank: 1 })

// Selected posts for group A on Saturday
db.selected_posts.find({ day_number: 1, group_id: "A" })
```

## Top-post selection

Across all posts in a group:

1. Prefer posts with **non-empty title and body**
2. Sort by **`numberOfComments`** (desc)
3. Tiebreak with **`upVotes`** (desc)
4. Take top **N**

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Entry point |
| `db/repository.py` | MongoDB persistence |
| `schedule_loader.py` | Groups, weekday plan, rules |
| `scraper.py` | Apify scraping |
| `select_posts.py` | Group-level top-N selection |
| `comment_generator.py` | Prompt + LLM |
| `telegram_notifier.py` | Phase 4 Telegram delivery |
| `job/daemon.py` | Daily automated job (workflow + scheduled Telegram) |
| `JOB_GUIDE.md` | Full job setup, logging, and launchd install |
| `mara_schedule_structured_with_urls.json` | Schedule, URLs, rules |

## Daily automated job

Runs every day at **00:01 Los Angeles time** (equivalent to `python app.py {day} 3`), then sends Telegram messages in a **scheduled hourly slot between 8 AM and 4 PM LA** (not immediately). Includes **Regenerate** / **Done** buttons and logs one file per day in `../reddit-bot-logs/`.

```bash
python -m job.daemon              # run manually (foreground)
./scripts/install_job.sh          # install as macOS background service
```

See [JOB_GUIDE.md](JOB_GUIDE.md) for full details.
