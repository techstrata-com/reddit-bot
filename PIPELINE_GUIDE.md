# Pipeline Guide — Manual CLI

How to run `app.py` for every scenario: full runs, partial phases, resume, and Telegram handoff.

For the automated daily job, see [JOB_GUIDE.md](JOB_GUIDE.md). For project overview and setup, see [README.md](README.md).

---

## CLI reference

```
python3 app.py <day> [top_n] [options]
```

| Argument / flag | Description |
|-----------------|-------------|
| `day` | Mara day number `1`–`7` (Monday = 3 = REST) |
| `top_n` | Top posts per group (default `3`) |
| `--skip-comments` | Stop after Phase 1 + 2 (scrape + rank) |
| `--comments-only` | Phase 3 only — requires `--resume` |
| `--send-telegram` | Phase 4 — send comments to Telegram |
| `--dry-run` | With `--send-telegram`: preview without sending |
| `--resume RUN_KEY` | Continue an existing pipeline run |
| `--group A` | Limit to one group (repeatable) |

Each new run creates a `run_key` like `20260608_122706` (UTC timestamp). Note it from the output for `--resume`.

---

## Day numbers

| # | Day | Groups |
|---|-----|--------|
| 1 | Saturday | A, H |
| 2 | Sunday | B, I |
| 3 | Monday | REST — exits with no work |
| 4 | Tuesday | C, E |
| 5 | Wednesday | D, F |
| 6 | Thursday | D, I |
| 7 | Friday | C, H |

---

## Scenario 1 — Full pipeline (no Telegram)

Scrape → rank → generate comments for all groups on a day.

```bash
python3 app.py 7 3          # Friday, top 3 per group
python3 app.py 1            # Saturday, top 3 (default)
python3 app.py 5 5          # Wednesday, top 5 per group
```

---

## Scenario 2 — Full pipeline + Telegram in one run

Same as Scenario 1, then immediately sends each comment to Telegram after generation.

```bash
python3 app.py 7 3 --send-telegram

# Preview Telegram sends without actually sending
python3 app.py 7 3 --send-telegram --dry-run
```

Requires Telegram bot setup (see [README.md](README.md#telegram-setup-one-time)).

---

## Scenario 3 — Scrape and rank only (no LLM)

Useful to review selected posts before spending on LLM calls.

```bash
python3 app.py 1 3 --skip-comments
```

Saves to `raw_posts` and `selected_posts`. Note the printed `run_key`.

---

## Scenario 4 — Single group only

Run any scenario above for one pipeline group.

```bash
python3 app.py 1 3 --group A
python3 app.py 1 3 --group A --skip-comments
python3 app.py 1 3 --group H --send-telegram
```

---

## Scenario 5 — Resume: scrape a missing group

If a previous run completed some groups but not others, resume and target the missing group.

```bash
python3 app.py 1 3 --resume 20260608_122706 --group H --skip-comments
```

Only group H is scraped and ranked. Other groups from that run are untouched.

---

## Scenario 6 — Resume: generate comments only

Uses existing `selected_posts` from a prior run. Does not scrape again.

```bash
# One group
python3 app.py 1 --resume 20260608_122706 --group A --comments-only

# All groups that have selected_posts in that run
python3 app.py 1 --resume 20260608_122706 --comments-only
```

Skips posts that already have a comment for that `run_key`.

---

## Scenario 7 — Resume: send to Telegram only

Uses existing `comments` from MongoDB. Does not scrape or generate.

```bash
# Dry-run first
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram --dry-run

# Send for one group
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram

# Send for all groups in the run
python3 app.py 1 --resume 20260608_122706 --send-telegram
```

Alternative retry tool (also handles group registration):

```bash
python resend_telegram.py --dry-run
python resend_telegram.py --run-key 20260608_122706
python resend_telegram.py --day 1 --group A
```

---

## Scenario 8 — REST day

```bash
python3 app.py 3
# → "Monday is REST - no groups scheduled."
```

---

## Scenario 9 — Split workflow (common pattern)

Run phases on separate days or after manual review:

```bash
# Day 1: scrape + rank
python3 app.py 1 3 --skip-comments
# → run_key: 20260608_120000

# Day 2: generate comments after reviewing selected_posts
python3 app.py 1 --resume 20260608_120000 --comments-only

# Day 3: send to Telegram
python3 app.py 1 --resume 20260608_120000 --send-telegram --dry-run
python3 app.py 1 --resume 20260608_120000 --send-telegram
```

---

## Command cheat sheet

| Goal | Command |
|------|---------|
| Full pipeline | `python3 app.py <day> <top_n>` |
| Full + Telegram | `python3 app.py <day> <top_n> --send-telegram` |
| Scrape + rank only | `python3 app.py <day> <top_n> --skip-comments` |
| One group | `python3 app.py <day> <top_n> --group A` |
| Resume + comments | `python3 app.py <day> --resume <RUN_KEY> --comments-only` |
| Resume + Telegram | `python3 app.py <day> --resume <RUN_KEY> --send-telegram` |
| Preview Telegram | add `--dry-run` to any `--send-telegram` command |

**Invalid combinations:**

- `--comments-only` without `--resume`
- `--send-telegram` with `--skip-comments`

---

## Pipeline phases

```
Phase 1: Scrape subreddits (Apify)     → raw_posts
Phase 2: Rank top N per group          → selected_posts
Phase 3: Generate LLM comments       → comments
Phase 4: Send to Telegram              → comments (publish_status updated)
```

### Telegram message format (Phase 4)

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

After **Done**, buttons are removed and status shows ✅ Published.

---

## MongoDB queries

```javascript
// All data for a run
db.raw_posts.find({ run_key: "20260608_122706" })
db.selected_posts.find({ run_key: "20260608_122706" }).sort({ group_id: 1, rank: 1 })
db.comments.find({ run_key: "20260608_122706" })

// Selected posts for group A on Saturday
db.selected_posts.find({ day_number: 1, group_id: "A" })

// Comments awaiting Telegram send
db.comments.find({ run_key: "20260608_122706", publish_status: "pending" })

// Comments sent but not yet posted on Reddit
db.comments.find({ publish_status: "sent" })

// Comments marked published by team
db.comments.find({ publish_status: "published" })
```

### Comment document fields (Phase 4)

| Field | Description |
|-------|-------------|
| `publish_status` | `pending` → `sent` / `failed` → `published` |
| `telegram_message_id` | Telegram message ID |
| `telegram_chat_id` | Chat the message was sent to |
| `published_at` | When sent or marked published |
| `published_by_username` | Telegram user who tapped Done |
| `publish_error` | Error message if failed |

---

## Example: Saturday group A end-to-end

```bash
# 1) Scrape + rank + generate
python3 app.py 1 3 --group A
# → note run_key, e.g. 20260608_122706

# 2) Send to Telegram
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram --dry-run
python3 app.py 1 --resume 20260608_122706 --group A --send-telegram
```

Or combine steps 1 and 2:

```bash
python3 app.py 1 3 --group A --send-telegram
```

---

## Key source files

| File | Role in pipeline |
|------|------------------|
| `app.py` | CLI orchestrator |
| `scraper.py` | Phase 1 — Apify Reddit scrape |
| `select_posts.py` | Phase 2 — ranking logic |
| `comment_generator.py` | Phase 3 — LLM calls |
| `comment_generator_prompt.py` | Prompt template |
| `telegram_notifier.py` | Phase 4 — Telegram API |
| `telegram_chats.py` | Target group resolution |
| `schedule_loader.py` | Day → groups → subreddits |
| `db/repository.py` | MongoDB persistence |
| `actor_config.json` | Apify actor defaults |
| `mara_schedule_structured_with_urls.json` | Weekly schedule data |
