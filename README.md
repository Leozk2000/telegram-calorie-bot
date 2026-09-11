# 🍳 Calorie Tracker Telegram Bot

A Telegram bot that analyzes photos of your meals using Google Gemini, estimates
calories and macros, logs everything (plus price, if you tell it) to a Google
Sheet, and can chart your nutrition and spending over time.

## Features

- 📸 Send a food photo → Gemini estimates meal, calories, protein, carbs, and fat
- 📊 `/total` — today's nutrition + spending tally (or any past date)
- 📈 `/graphdaily` — a chart of every meal logged today
- 📈 `/graphweekly` — a chart of daily totals over the last 7 days
- 💰 Optional price follow-up after each meal, for expense tracking alongside nutrition
- 🗑️ `/delete` — remove your own most recent entry, within 1 hour of logging it
- 🔄 `/refresh` — re-sync the bot's memory with the sheet after a manual edit
- Runs on Render's free tier, kept awake with an external pinger (see below)

## How it works, at a glance

```
Telegram photo → Render (Flask webhook) → Gemini (analysis) → Google Sheets (log)
                                                              → Telegram (reply)
```

Everything the bot needs to run lives in three places: a Telegram bot token, a
Gemini API key, and a Google service account with access to one Google Sheet.
None of these cost money at the usage levels this bot is designed for.

---

## 1. Create your Telegram bot (BotFather)

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and a unique username
   ending in `bot`).
3. BotFather will reply with a **token** that looks like
   `123456789:AAExampleTokenDoNotShareThis`. Save it — this is your
   `TELEGRAM_TOKEN`.
4. Keep this token secret. Anyone with it can control your bot.

## 2. Get a Google Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/).
2. Sign in with a Google account and click **Get API key** → **Create API key**.
3. Copy the key — this is your `GEMINI_KEY`.
4. The free tier has rate limits (requests per minute) that are periodically
   adjusted by Google — check the current limits on the
   [Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing) if
   you notice unexpected `429` errors.

## 3. Set up Google Sheets access (the trickiest part)

The bot writes to a Google Sheet using a **service account** — a robot Google
account that only your bot uses, separate from your personal Google login.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a new project (or reuse an existing one).
2. Under **APIs & Services → Library**, enable:
   - **Google Sheets API**
   - **Google Drive API**
3. Under **APIs & Services → Credentials**, click **Create Credentials →
   Service Account**. Give it any name (e.g. `calorie-bot`).
4. Open the new service account, go to the **Keys** tab, click **Add Key →
   Create new key → JSON**. This downloads a `.json` file — **do not commit
   this file to GitHub**. Its entire contents (as one JSON string) become your
   `GOOGLE_CREDS` environment variable.
5. Inside that JSON file, find the `"client_email"` field — it looks like
   `calorie-bot@your-project.iam.gserviceaccount.com`.
6. Create a new Google Sheet named **exactly** `Calorie Tracker Logs` (or
   update the name in `main.py` if you'd rather use a different one).
7. Click **Share** on the sheet and share it with the `client_email` address
   from step 5, giving it **Editor** access. Without this step, the bot can
   authenticate but won't be able to read or write the sheet.
8. Set up the header row (row 1) exactly like this:

   | A    | B           | C    | D        | E       | F     | G    | H     |
   |------|-------------|------|----------|---------|-------|------|-------|
   | Date | Telegram_ID | Meal | Calories | Protein | Carbs | Fats | Price |

   The bot writes to columns by position, but keeping real headers makes the
   sheet readable for you.

## 4. Deploy to Render

1. Push this repository to your own GitHub account.
2. On [Render](https://render.com/), click **New → Web Service** and connect
   your GitHub repo.
3. Configure the service:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
   - **Instance Type**: Free
4. Under **Environment**, add these environment variables:

   | Key             | Value                                                |
   |-----------------|-------------------------------------------------------|
   | `TELEGRAM_TOKEN` | The token from BotFather (step 1)                    |
   | `GEMINI_KEY`     | The API key from Google AI Studio (step 2)           |
   | `GOOGLE_CREDS`   | The full contents of the service account JSON file (step 3), pasted as one line |

   `RENDER_EXTERNAL_URL` is set automatically by Render — you don't need to add it.
5. Deploy. On a successful boot, the logs should show:
   ```
   ✅ Webhook registered at https://your-service-name.onrender.com/webhook/<token>
   ✅ Command menu registered
   ✅ Cache loaded: N rows
   🚀 Bot server running via webhook (no polling, no 409 conflicts)...
   ```
6. Message your bot on Telegram — try `/total` or send a food photo.

## 5. Keep it awake (Render free tier spins down after ~15 min idle)

Render's free tier suspends the service after a period of no incoming HTTP
traffic, causing a slow "cold start" on the next request. To prevent this:

1. Go to [cron-job.org](https://cron-job.org) and create a free account.
2. Create a new cron job hitting your Render URL's root path
   (`https://your-service-name.onrender.com/`) every **10 minutes**.
3. Leave the request method as `GET`. This just hits the bot's health-check
   route (`"Bot is running healthy!"`) — it doesn't touch the webhook or your
   bot token.

This is a "set once" step — no login requirement, no repo activity needed to
keep it running, unlike some alternatives (e.g. GitHub Actions scheduled
workflows, which auto-disable after 60 days of repo inactivity).

---

## Bot commands

| Command | What it does |
|---|---|
| Send a photo | Analyzes the meal, logs it, and asks for the price |
| `/total` | Today's nutrition + spending tally |
| `/total YYYY-MM-DD` | Tally for a specific past date |
| `/graphdaily` | Chart of today's meals |
| `/graphdaily YYYY-MM-DD` | Chart of meals on a specific date |
| `/graphweekly` | Chart of daily totals, last 7 days |
| `/delete` or `/undo` | Delete your own last entry (only within 1 hour of logging) |
| `/refresh` | Re-sync the bot's memory with the sheet (after a manual edit) |

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | Bot token from BotFather |
| `GEMINI_KEY` | Yes | Gemini API key from Google AI Studio |
| `GOOGLE_CREDS` | Yes | Full JSON contents of your Google service account key |
| `RENDER_EXTERNAL_URL` | Auto (Render sets this) | Your service's live HTTPS URL |
| `PORT` | Auto (Render sets this) | Port Flask listens on |

## Known limitations

- Designed for a single Render instance — see `ideas.txt` for notes on
  scaling to more concurrent users.
- Assumes chronological, one-at-a-time photo uploads; batch-uploading
  multiple photos before answering a price prompt isn't supported by design.
- Free-tier API limits (Gemini, Google Sheets) apply — see `ideas.txt` for
  current quota notes.
- AI calorie estimates from a single photo carry real margin of error,
  especially for complex/mixed plates — see `ideas.txt` for mitigation ideas
  under consideration.

## License

See `LICENSE`.
