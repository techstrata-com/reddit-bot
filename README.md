# Reddit Bot Pipeline

Scrapes Reddit posts for Mara’s weekly schedule, selects the top posts per **group**, generates AI comments, and stores everything in **MongoDB**.

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

# Reddit posting (username + password only — no API app needed)
REDDIT_USERNAME=your_reddit_username
REDDIT_PASSWORD=your_reddit_password
REDDIT_POST_DELAY_SECS=45
REDDIT_BROWSER_HEADLESS=false
REDDIT_BROWSER_CHANNEL=chrome
```

## Run

```bash
python3 app.py 7 3          # Friday, top 3 per group
python3 app.py 1            # Saturday, top 3 (default)
python3 app.py 3            # Monday — exits (REST)
python3 app.py 7 3 --skip-comments   # scrape + select only

# Resume: generate comments for group A from existing run
python3 app.py 1 --resume 20260608_122706 --group A --comments-only

# One-time setup: install browser + log into Reddit
python3 -m playwright install chromium
python3 app.py 1 --reddit-login

# Post generated comments to Reddit (dry-run first)
python3 app.py 1 --resume 20260608_122706 --group A --post-comments --dry-run
python3 app.py 1 --resume 20260608_122706 --group A --post-comments
```

## MongoDB collections

| Collection | What it stores |
|---|---|
| `raw_posts` | **All scraped posts** — includes `day_number`, `day_name`, `group_id`, full post data |
| `selected_posts` | **Top N per group** — rank, selection criteria, day + group info |
| `pipeline_runs` | Run metadata (when, status, stats) |
| `group_runs` | Per-group progress (scraping → selecting → done) |
| `comments` | Generated LLM comments + publish status (`pending` / `posted` / `failed`) |

### Flow

```
Phase 1: scrape all subreddits → save each batch to raw_posts
Phase 2: rank across group      → save top N to selected_posts
Phase 3: generate comments      → save to comments (optional)
Phase 4: post comments          → publish to Reddit via browser (username + password)
```

### Reddit posting

Uses Playwright + a saved browser session. No `client_id` needed.

1. Set `REDDIT_USERNAME` and `REDDIT_PASSWORD` in `.env`
2. Run `python3 -m playwright install chromium` once
3. Run `python3 app.py 1 --reddit-login` if 2FA/captcha blocks auto-login
4. Post with `--post-comments`

Session is saved in `.reddit_browser_session/` (gitignored).

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
| `mara_schedule_structured_with_urls.json` | Schedule, URLs, rules |
