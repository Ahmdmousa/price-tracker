# Price Tracker

A personal price tracker for Egyptian stores, similar to Kanbkam.

It checks your product list on a schedule, keeps the price history, and alerts you on **Telegram** and **email** when:
- a price drops,
- a price hits your target,
- an item comes back in stock.

A **dashboard** with price charts is published on GitHub Pages. Everything runs free on GitHub Actions.

| Store | Status |
|---|---|
| Amazon.eg | ✅ Works. Reads the buy-box price and detects "Currently unavailable" |
| B.TECH | ✅ Works. Reads the product's JSON-LD |
| Jumia | ⚠️ Intermittent. Cloudflare returns 403 after a few automated requests |
| Noon | ❌ Blocks non-browser clients (the connection hangs). Kept so you get a clear error |

## How it works
- An hourly GitHub Actions run (`.github/workflows/track.yml`) does three things:
  1. reads new Telegram messages (add, remove and target commands),
  2. checks any product not checked in the last `CHECK_INTERVAL_HOURS` (default 3),
  3. sends alerts and commits `data/db.json` and `docs/index.html` back to the repo.
- `data/db.json` is the database: products plus their price history. The history records a point whenever the price or stock changes, and at least once a day.

## Setup (about 10 minutes)

1. **Create a GitHub repo** and push this folder to it.
   - GitHub Pages on a free account needs a **public** repo. Your tracked product list and prices will then be visible, but your tokens stay secret.
   - With a private repo everything else still works; only the Pages dashboard needs GitHub Pro.
2. **Telegram bot**
   - In Telegram, message **@BotFather** → `/newbot` → copy the token.
   - Send any message to your new bot, then run locally:
     `TELEGRAM_BOT_TOKEN=<token> python -m tracker chat-id`
     That prints your chat id. Only this chat can control the bot.
3. **Email (optional)**
   - With Gmail, create an *App password* (Google Account → Security → 2-Step Verification → App passwords).
4. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `SMTP_HOST` = `smtp.gmail.com`, `SMTP_PORT` = `587`, `SMTP_USER` = your Gmail, `SMTP_PASSWORD` = the app password, `EMAIL_TO` = where alerts go
   - Optional **variables**: `CHECK_INTERVAL_HOURS` (default 3) and `MIN_DROP_PCT` (default 1, so drops under 1% are ignored).
5. **Turn on Pages**: Settings → Pages → *Deploy from a branch* → `main` / `docs`.
6. **Start it**: Actions → *Track prices* → *Run workflow*.

## Using it

Telegram:
```
https://www.amazon.eg/dp/B0C9JP6J45          → start tracking
https://btech.com/en/p/<id> 8500             → track with a target price
/list                                        → numbered list with prices
/target 2 8000                               → set target (0 clears it)
/remove 2                                    → stop tracking
```
Replies arrive on the next hourly run. GitHub cron can lag a few minutes.

Command line (local):
```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m tracker add "<url>" --target 500
.venv\Scripts\python -m tracker list
.venv\Scripts\python -m tracker run --force      # check everything now
.venv\Scripts\python -m tracker remove 1
```

## Notes
- Requests are spaced 2–5 s apart, and each product is checked at most every few hours. Keep it that way: it is gentle on the stores and less likely to get blocked.
- GitHub's servers are more likely to be blocked than your home connection, especially by Amazon. If checks start failing in Actions, you get a ⚠️ alert after 3 failures. Running `python -m tracker run` on your own PC with Windows Task Scheduler is a drop-in fallback.
- To add a store: add its domain to `STORES` in `tracker/stores.py`. If the store's pages have JSON-LD `Product` data, that is all it needs.
