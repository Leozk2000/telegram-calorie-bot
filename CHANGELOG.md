# Changelog

All notable changes to this project are documented here. Dates reflect when
changes were made during development, not necessarily separate public
releases.

## [1.1.0] - 2026-09-11

### Added
- **`/total` command** — tallies today's (or a specified date's) calories,
  protein, carbs, fat, and total price for the requesting user.
- **`/total` now includes total price spent**, alongside the nutrition tally.
- **Price tracking follow-up flow** — after logging a meal, the bot asks
  "How much did this meal cost?" via a validated reply (strict regex,
  rejects anything that isn't a plain number, supports `/skip`). The price
  is never passed to Gemini and is always cast to a float before being
  written to the sheet, so it can't be mistaken for an AI instruction or a
  spreadsheet formula.
- **Batch-upload rejection** — if a second photo arrives while a price
  question is still pending, it's rejected and the original price prompt is
  re-asked instead of being silently processed.
- **`/delete` (alias `/undo`)** — deletes the sender's own most recent
  entry, but only if it belongs to them and is less than 1 hour old.
  Reachable mid-price-prompt too (e.g. after uploading the wrong photo).
- **`/graphdaily`** — a two-panel line chart (calories + macros on top,
  price below) across every meal logged on a given day (defaults to today).
- **`/graphweekly`** — the same chart style, but showing daily totals across
  the last 7 days.
- **In-memory sheet cache** (`sheet_cache`) — the sheet is read in full only
  once, at startup. Every command (`/total`, `/delete`, `/graphdaily`,
  `/graphweekly`) reads from this cache instead of re-querying Google
  Sheets, keeping response times flat as the sheet grows.
- **`/refresh` command** — manually forces a full cache re-sync, for use
  after editing the sheet by hand outside the bot.
- **Native Telegram command menu** (`bot.set_my_commands()`), so `/total`,
  `/graphdaily`, `/graphweekly`, `/delete`, and `/refresh` appear in
  Telegram's built-in "/" command picker.
- **External uptime pinger** (cron-job.org, every 10 minutes) to prevent
  Render's free-tier spin-down after ~15 minutes of inactivity.

### Changed
- **Timestamps now logged in GMT+8 (Singapore time)** instead of the
  server's UTC clock — previously, logged times were 8 hours behind what
  Telegram displayed to the user.
- **`/today` removed** — it was an exact duplicate of `/total` and added
  nothing; `/total` is now the single source of truth for daily tallies.

### Dependencies
- Added `matplotlib` (for `/graphdaily` and `/graphweekly` chart rendering,
  using the headless `Agg` backend since Render has no display).

## [1.0.0] - Baseline (pre-existing before this documentation effort)

The bot's original working state, before the above features were added:

- Telegram bot using `pyTelegramBotAPI`, running via Flask webhook (not
  polling) on Render.
- Photo handler sends the image + a nutritionist system prompt to Gemini
  (`gemini-flash-lite-latest`), which returns a JSON block (meal, calories,
  protein, carbs, fat) followed by a formatted text summary.
- Robust Telegram file downloader with retries and defensive token cleanup.
- Retry-with-backoff wrapper around Gemini calls, retrying only on
  transient errors (503/overloaded), not on permanent failures like a bad
  model name.
- JSON block extracted via regex from Gemini's response and appended as a
  new row to a Google Sheet ("Calorie Tracker Logs") via `gspread`.
- Markdown-parse fallback: if Telegram rejects malformed Markdown from
  Gemini's output, the bot resends the same text with no formatting rather
  than failing silently.
- Webhook registration on startup, clearing any stale webhook/polling state
  first to avoid `409` conflicts across deploys.

## Roadmap

Not yet implemented — see `ideas.txt` for full details, feasibility notes,
and known limitations on each:

- Switching from Flask's dev server to `gunicorn` with threads, to support
  more concurrent users without losing the shared in-memory cache.
- User profiles (weight, height, goals, computed calorie/macro targets).
- Multi-model "panel" estimation to reduce single-model calorie estimation
  error.
- Workout/calorie-burn integration for net calorie tracking.
