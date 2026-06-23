# Reddit Bot — Pipeline Guide

## Setup (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Fill in `.env` (API keys, MongoDB SSH, Telegram bot token + group chat IDs).

Create a Telegram bot via [@BotFather](https://t.me/BotFather), add it to each handoff group, and map pipeline groups to chat IDs:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_IDS={"A":"-1001234567890","H":"-1009876543210"}
```

---

## Day numbers

| Day | Name |
|-----|------|
| 1 | Saturday |
| 2 | Sunday |
| 3 | Monday (REST — no groups) |
| 4 | Tuesday |
| 5 | Wednesday |
| 6 | Thursday |
| 7 | Friday |

---

## Pipeline overview

```
Phase 1: Scrape subreddits (Apify)     → raw_posts
Phase 2: Rank top N per group          → selected_posts
Phase 3: Generate LLM comments         → comments
Phase 4: Send comments to Telegram   → comments (publish_status updated)
```

Each run gets a unique `run_key` (e.g. `20260608_122706`). Use it with `--resume` to continue later.

---

## Run the entire pipeline

Scrape + rank + generate comments + (optionally) send to Telegram — all groups for a day:

```bash
# Full run: scrape → select → generate comments (default top 3 per group)
python3 app.py 1 3

# Same, but stop before comment generation
python3 app.py 1 3 --skip-comments

# Only one group
python3 app.py 1 3 --group A
```

After a full run, send comments in a second step:

```bash
python3 app.py 1 --resume 20260608_122706 --send-telegram
```

---

## Run each phase separately

### Phase 1 + 2 — Scrape and rank (no comments)

```bash
python3 app.py 1 3 --skip-comments
```

Saves all scraped posts to `raw_posts` and top N to `selected_posts`.  
Note the `run_key` printed at the start (e.g. `Run: 20260608_122706`).

---

### Phase 1 only — Resume scraping for a missing group

```bash
python3 app.py 1 3 --resume 20260608_122706 --group H --skip-comments
```

Scrapes only group H, then ranks it. Other groups from that run are untouched.

---

### Phase 3 — Generate comments only

Uses existing `selected_posts` from a previous run. Does not scrape again.

```bash
python3 app.py 1 --resume 20260608_122706 --group A --comments-only
```

---

### Phase 4 — Send comments to Telegram

Uses existing `comments` from MongoDB. Sends one Telegram message per comment to the chat configured for that pipeline group. Each message contains the generated comment text and the Reddit post URL.

```bash
# Preview — nothing is sent, DB is not updated
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram --dry-run

# Actually send
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram

# All groups in that run
python3 app.py 1 --resume 20260608_122706 --send-telegram
```

---

## Common command reference

| Goal | Command |
|------|---------|
| Full pipeline (no Telegram) | `python3 app.py <day> <top_n>` |
| Full pipeline, skip LLM | `python3 app.py <day> <top_n> --skip-comments` |
| One group only | `python3 app.py <day> <top_n> --group A` |
| Resume + comments only | `python3 app.py <day> --resume <RUN_KEY> --comments-only` |
| Resume + send Telegram | `python3 app.py <day> --resume <RUN_KEY> --send-telegram` |

---

## MongoDB collections

| Collection | What it stores |
|------------|----------------|
| `raw_posts` | Every scraped post (per subreddit, group, run) |
| `selected_posts` | Top N ranked posts per group |
| `comments` | Generated comment text + delivery status |
| `pipeline_runs` | Run metadata (day, status, stats) |
| `group_runs` | Per-group progress (scraping → selecting → done) |

### Useful queries

```javascript
// All data for a run
db.raw_posts.find({ run_key: "20260608_122706" })
db.selected_posts.find({ run_key: "20260608_122706" }).sort({ group_id: 1, rank: 1 })
db.comments.find({ run_key: "20260608_122706" })

// Sent comments only
db.comments.find({ run_key: "20260608_122706", publish_status: "sent" })
```

### Comment delivery fields (Phase 4)

| Field | Values |
|-------|--------|
| `publish_status` | `pending` → `sent` or `failed` |
| `published_at` | timestamp when sent or failed |
| `telegram_message_id` | Telegram message ID |
| `telegram_chat_id` | Telegram chat the message was sent to |
| `publish_error` | error message if failed |

---

## Project files

Two-line summary of each file — what it is and where it is used.

---

### `app.py`
Main entry point and orchestrator. Runs the full pipeline or individual phases via CLI flags (`--skip-comments`, `--comments-only`, `--send-telegram`, `--resume`, `--group`).

---

### `scraper.py`
Calls the Apify Reddit actor to scrape posts from a subreddit URL. Used in Phase 1 by `app.py`; polls Apify until the run finishes and returns post items.

---

### `select_posts.py`
Ranks scraped posts and picks top N per group (prefer title+body, then comment count, then upvotes). Used in Phase 2 by `app.py` after all subreddits in a group are scraped.

---

### `comment_generator.py`
Builds the LLM prompt and calls OpenAI or Gemini to generate a comment. Used in Phase 3 by `app.py` for each selected post.

---

### `comment_generator_prompt.py`
Contains the prompt template with placeholders (`{{subreddit_name}}`, `{{post_title}}`, etc.). Loaded by `comment_generator.py` when rendering the prompt sent to the LLM.

---

### `schedule_loader.py`
Reads the weekly schedule JSON and maps day numbers to groups and subreddits. Used by `app.py` to know which subreddits to scrape on each day and to attach subreddit rules to comment generation.

---

### `telegram_notifier.py`
Sends generated comments to Telegram group chats via the Bot API. Used in Phase 4 by `app.py`; maps pipeline group IDs to chat IDs from `TELEGRAM_CHAT_IDS` in `.env`.

---

### `db/connection.py`
Opens the MongoDB connection, optionally via SSH tunnel to the remote server. Used by `db/repository.py` on first database access.

---

### `db/repository.py`
All MongoDB read/write operations: runs, raw posts, selected posts, comments, delivery status. Used throughout `app.py` to persist and resume pipeline state.

---

### `actor_config.json`
Default settings for the Apify Reddit scraper actor (max posts, proxy, etc.). Loaded by `app.py`; subreddit URLs are overridden at runtime from the schedule file.

---

### `mara_schedule_structured_with_urls.json`
Weekly plan: which groups run on which day, subreddit URLs, names, and rules. Read by `schedule_loader.py` — drives scraping targets and comment prompt context.

---

### `requirements.txt`
Python package dependencies (Apify, OpenAI, MongoDB, etc.). Install with `pip install -r requirements.txt`.

---

### `.env`
Secrets and config (API keys, MongoDB SSH, Telegram bot, LLM provider). Loaded by `app.py` and several modules via `python-dotenv`. Not committed to git.

---

### `README.md`
Project overview and quick-start documentation. General reference; this file (`PIPELINE_GUIDE.md`) has the detailed run commands.

---

## Example: Saturday group A end-to-end

```bash
# 1) Scrape + rank + generate (creates run_key)
python3 app.py 1 3 --group A
# → run_key printed, e.g. 20260608_122706

# 2) Send to Telegram
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram --dry-run
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram
```
