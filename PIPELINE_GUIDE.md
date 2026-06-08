# Reddit Bot — Pipeline Guide

## Setup (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 -m playwright install chromium
```

Fill in `.env` (API keys, MongoDB SSH, Reddit username/password).

Log into Reddit once (saves browser session):

```bash
python3 app.py 1 --reddit-login
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
Phase 4: Post comments to Reddit       → comments (publish_status updated)
```

Each run gets a unique `run_key` (e.g. `20260608_122706`). Use it with `--resume` to continue later.

---

## Run the entire pipeline

Scrape + rank + generate comments + (optionally) post — all groups for a day:

```bash
# Full run: scrape → select → generate comments (default top 3 per group)
python3 app.py 1 3

# Same, but stop before comment generation
python3 app.py 1 3 --skip-comments

# Only one group
python3 app.py 1 3 --group A
```

After a full run, post comments in a second step (needs saved Reddit session):

```bash
python3 app.py 1 --resume 20260608_122706 --post-comments
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

### Phase 4 — Post comments to Reddit

Uses existing `comments` from MongoDB. Updates `publish_status` in DB.

```bash
# Preview — nothing is posted, DB is not updated
python3 app.py 1 --resume 20260608_122706 --group A --post-comments --dry-run

# Actually post
python3 app.py 1 --resume 20260608_122706 --group A --post-comments

# All groups in that run
python3 app.py 1 --resume 20260608_122706 --post-comments
```

---

## Common command reference

| Goal | Command |
|------|---------|
| Full pipeline (no posting) | `python3 app.py <day> <top_n>` |
| Full pipeline, skip LLM | `python3 app.py <day> <top_n> --skip-comments` |
| One group only | `python3 app.py <day> <top_n> --group A` |
| Resume + comments only | `python3 app.py <day> --resume <RUN_KEY> --comments-only` |
| Resume + post comments | `python3 app.py <day> --resume <RUN_KEY> --post-comments` |
| Reddit login (once) | `python3 app.py 1 --reddit-login` |

---

## MongoDB collections

| Collection | What it stores |
|------------|----------------|
| `raw_posts` | Every scraped post (per subreddit, group, run) |
| `selected_posts` | Top N ranked posts per group |
| `comments` | Generated comment text + publish status |
| `pipeline_runs` | Run metadata (day, status, stats) |
| `group_runs` | Per-group progress (scraping → selecting → done) |

### Useful queries

```javascript
// All data for a run
db.raw_posts.find({ run_key: "20260608_122706" })
db.selected_posts.find({ run_key: "20260608_122706" }).sort({ group_id: 1, rank: 1 })
db.comments.find({ run_key: "20260608_122706" })

// Posted comments only
db.comments.find({ run_key: "20260608_122706", publish_status: "posted" })
```

### Comment publish fields (Phase 4)

| Field | Values |
|-------|--------|
| `publish_status` | `pending` → `posted` or `failed` |
| `published_at` | timestamp when posted or failed |
| `reddit_comment_url` | link to the comment on Reddit |
| `publish_error` | error message if failed |

---

## Project files

Two-line summary of each file — what it is and where it is used.

---

### `app.py`
Main entry point and orchestrator. Runs the full pipeline or individual phases via CLI flags (`--skip-comments`, `--comments-only`, `--post-comments`, `--resume`, `--group`).

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

### `reddit_poster.py`
Thin wrapper for posting comments — calls the browser poster and normalizes success/error results. Used in Phase 4 by `app.py` via `post_comment_safe()`.

---

### `reddit_browser_poster.py`
Playwright-based Reddit automation: login, saved session, and posting comments on `www.reddit.com`. Used by `reddit_poster.py`; session stored in `.reddit_browser_session/`.

---

### `db/connection.py`
Opens the MongoDB connection, optionally via SSH tunnel to the remote server. Used by `db/repository.py` on first database access.

---

### `db/repository.py`
All MongoDB read/write operations: runs, raw posts, selected posts, comments, publish status. Used throughout `app.py` to persist and resume pipeline state.

---

### `actor_config.json`
Default settings for the Apify Reddit scraper actor (max posts, proxy, etc.). Loaded by `app.py`; subreddit URLs are overridden at runtime from the schedule file.

---

### `mara_schedule_structured_with_urls.json`
Weekly plan: which groups run on which day, subreddit URLs, names, and rules. Read by `schedule_loader.py` — drives scraping targets and comment prompt context.

---

### `requirements.txt`
Python package dependencies (Apify, OpenAI, MongoDB, Playwright, etc.). Install with `pip install -r requirements.txt`.

---

### `.env`
Secrets and config (API keys, MongoDB SSH, Reddit credentials, LLM provider). Loaded by `app.py` and several modules via `python-dotenv`. Not committed to git.

---

### `README.md`
Project overview and quick-start documentation. General reference; this file (`PIPELINE_GUIDE.md`) has the detailed run commands.

---

## Example: Saturday group A end-to-end

```bash
# 1) Scrape + rank + generate (creates run_key)
python3 app.py 1 3 --group A
# → run_key printed, e.g. 20260608_122706

# 2) Post to Reddit (after --reddit-login once)
python3 app.py 1 --resume 20260608_122706 --group A --post-comments --dry-run
python3 app.py 1 --resume 20260608_122706 --group A --post-comments
```
