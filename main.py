import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
import telebot
import google.genai as genai
import gspread
from PIL import Image
import requests
from io import BytesIO
from flask import Flask, request, abort

# 1. Fetch credentials securely from Render's Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client_ai = genai.Client(api_key=GEMINI_KEY)

# Authenticate Google Sheets
creds_dict = json.loads(GOOGLE_CREDS_JSON)
client_sheet = gspread.service_account_from_dict(creds_dict)
sheet = client_sheet.open("Calorie Tracker Logs").sheet1

# Render's servers run in UTC by default, but we want log timestamps to
# reflect Singapore local time (GMT+8) regardless of where the container
# actually runs.
SGT = timezone(timedelta(hours=8))

def now_sgt() -> datetime:
    """Current time as a timezone-aware datetime in Singapore (GMT+8)."""
    return datetime.now(timezone.utc).astimezone(SGT)

def download_telegram_file(file_path: str, token: str, timeout: int = 15, retries: int = 3) -> bytes:
    """
    Ultra-robust downloader for Telegram file content.

    Correct endpoint shape (this was the actual bug):
        https://api.telegram.org/file/bot<TOKEN>/<file_path>

    Guards against:
      - missing/empty token or file_path
      - accidental whitespace/newlines in the token (common when pasted into
        Render's env var UI)
      - transient network errors / timeouts (retried with backoff)
      - non-200 responses or empty bodies
    """
    if not token or not token.strip():
        raise ValueError("TELEGRAM_TOKEN is missing or empty.")
    if not file_path:
        raise ValueError("file_path from get_file() is missing or empty.")

    # Defensive cleanup: strips stray whitespace/newlines that sometimes get
    # copy-pasted into env vars, without altering a valid token.
    clean_token = token.strip()

    url = f"https://api.telegram.org/file/bot{clean_token}/{file_path}"

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code != 200:
                raise requests.exceptions.HTTPError(
                    f"Telegram file server returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            if not resp.content:
                raise ValueError("Downloaded file content is empty.")
            return resp.content
        except (requests.exceptions.RequestException, ValueError) as e:
            last_error = e
            print(f"⚠️ Download attempt {attempt}/{retries} failed: {e}")

    raise RuntimeError(f"Failed to download Telegram file after {retries} attempts: {last_error}")


def generate_content_with_retry(client, model, contents, max_attempts=3):
    """
    Calls Gemini's generate_content with short exponential backoff, but only
    retries on transient overload (503 UNAVAILABLE) or connection/timeout
    errors. Other errors (e.g. 404 bad model name, 400 bad request) fail
    immediately since retrying them would just waste time on a guaranteed
    repeat failure.

    Backoff schedule: 2s, then 4s between attempts (worst case ~6s added
    before giving up on attempt 3).
    """
    delay = 2
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as e:
            error_text = str(e)
            is_transient = "503" in error_text or "UNAVAILABLE" in error_text or "overloaded" in error_text.lower()
            last_error = e
            if not is_transient or attempt == max_attempts:
                raise
            print(f"⚠️ Gemini overloaded (attempt {attempt}/{max_attempts}), retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2
    raise last_error


SYSTEM_PROMPT = """
You are an expert nutritionist AI. Analyze the uploaded meal photo or text.
Estimate portion sizes and calculate macros. 
First, you MUST output a raw JSON block wrapped in triple backticks containing the data keys exactly like this:
```json
{
  "meal": "Name of meal",
  "calories": 350,
  "protein": 20,
  "carbs": 40,
  "fat": 10
}
```
Second, right below the JSON block, write a beautiful, clean summary text card for the user using bold markdown formatting and emojis.
"""

# HANDLER 1: Process Food Photos Safely
@bot.message_handler(content_types=['photo'])
def handle_food_photo(message):
    try:
        print(f"📸 Photo received from User {message.from_user.id}. Querying Gemini AI...")
        bot.reply_to(message, "Analyzing your meal... 🔍")
        
        file_info = bot.get_file(message.photo[-1].file_id)
        file_bytes = download_telegram_file(file_info.file_path, TELEGRAM_TOKEN)
        img = Image.open(BytesIO(file_bytes))
        
        ai_response = generate_content_with_retry(
            client_ai,
            # Rolling alias on the Flash-Lite tier: cheaper, faster, and
            # generally has more headroom during high-demand periods than
            # full Flash, so fewer 503 UNAVAILABLE errors. Same alias-drift
            # trade-off as gemini-flash-latest applies (see note above) -
            # check https://ai.google.dev/gemini-api/docs/changelog if this
            # ever 404s again.
            model='gemini-flash-lite-latest',
            contents=[img, SYSTEM_PROMPT]
        )
        full_text = ai_response.text
        
        # SAFE EXTRACT FIX: Uses safe text regular expressions instead of list splitting
        try:
            match = re.search(r'```json\s*(\{.*?\})\s*```', full_text, re.DOTALL)
            if match:
                json_string = match.group(1)
                data = json.loads(json_string)
                
                sheet.append_row([
                    str(now_sgt().strftime("%Y-%m-%d %H:%M:%S")),
                    str(message.from_user.id),
                    data.get("meal"),
                    data.get("calories"),
                    data.get("protein"),
                    data.get("carbs"),
                    data.get("fat")
                ])
                print("✅ Entry securely added to your Google Sheet.")
        except Exception as sheet_error:
            print(f"⚠️ Sheets logging skipped: {sheet_error}")
            
        try:
            bot.reply_to(message, full_text, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException as markdown_error:
            # Gemini-generated text can contain unbalanced Markdown (e.g. an
            # unmatched *) that Telegram's parser rejects with a 400. Rather
            # than crash the whole handler over a formatting glitch, fall
            # back to sending the same text with no formatting at all so the
            # user still gets their meal info.
            print(f"⚠️ Markdown parse failed ({markdown_error}); resending as plain text.")
            bot.reply_to(message, full_text)
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"❌ Execution crash:\n{error_details}")        
        # DEFINITIVE DIAGNOSTIC FIX: Text the exact internal issue straight to your phone!
        error_message = f"❌ *Error Processing Meal Card*\n\nReason:\n`{str(e)}`"
        try:
            bot.reply_to(message, error_message, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, error_message)


# HANDLER 2: Daily Tally Command
# Usage:
#   /total            -> tallies today's meals (SGT)
#   /total 2026-09-09 -> tallies meals for that specific date (SGT)
@bot.message_handler(commands=['total', 'today'])
def handle_daily_total(message):
    try:
        user_id = str(message.from_user.id)

        parts = message.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            target_date = parts[1].strip()
            # Basic sanity check on the format so a typo doesn't silently
            # match zero rows without explanation.
            try:
                datetime.strptime(target_date, "%Y-%m-%d")
            except ValueError:
                bot.reply_to(message, "⚠️ Please use the format `/total YYYY-MM-DD`.", parse_mode="Markdown")
                return
        else:
            target_date = now_sgt().strftime("%Y-%m-%d")

        records = sheet.get_all_records()

        total_cal = total_protein = total_carbs = total_fat = 0.0
        meal_count = 0

        for row in records:
            row_date = str(row.get("Date", ""))[:10]
            row_user = str(row.get("Telegram_ID", ""))
            if row_date == target_date and row_user == user_id:
                total_cal += float(row.get("Calories") or 0)
                total_protein += float(row.get("Protein") or 0)
                total_carbs += float(row.get("Carbs") or 0)
                # Column is labeled "Fats" in the sheet; fall back to "Fat"
                # just in case the header ever gets singularized.
                total_fat += float(row.get("Fats", row.get("Fat")) or 0)
                meal_count += 1

        if meal_count == 0:
            bot.reply_to(message, f"No meals logged for {target_date}. 🍽️")
            return

        summary = (
            f"📊 *Daily Total — {target_date}*\n\n"
            f"🍱 Meals logged: {meal_count}\n"
            f"🔥 Calories: {total_cal:.0f} kcal\n"
            f"💪 Protein: {total_protein:.0f} g\n"
            f"🍞 Carbs: {total_carbs:.0f} g\n"
            f"🥑 Fat: {total_fat:.0f} g"
        )
        try:
            bot.reply_to(message, summary, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, summary)

    except Exception as e:
        print(f"⚠️ Failed to compute daily total: {e}")
        error_message = f"❌ *Couldn't compute your daily total*\n\nReason:\n`{str(e)}`"
        try:
            bot.reply_to(message, error_message, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, error_message)


# HANDLER 3: Text Response Assistant
@bot.message_handler(content_types=['text'])
def handle_text_fallback(message):
    feedback = (
        "🍳 *Calorie Tracker Bot Ready!*\n\n"
        "Please upload a **photo** of your plate. "
        "The AI will evaluate macros and save them straight to your tracking sheet! 📊\n\n"
        "Send /total to see today's tally, or /total YYYY-MM-DD for a specific day."
    )
    bot.reply_to(message, feedback, parse_mode="Markdown")

# Use the token as part of the webhook path so the endpoint can't be
# guessed/spammed by someone who doesn't already know your bot token.
# NOTE (future improvement): Render's free tier spins this instance down after
# ~15 min of no inbound HTTP traffic. This webhook fix resolves the 409 polling
# conflict, but does NOT prevent spin-down/cold-start on its own. To keep the
# instance warm, set up an external uptime pinger (e.g. UptimeRobot, or a cron
# job) hitting the "/" route every ~10 minutes. Not implemented yet — revisit later.

WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"

# Render sets this automatically to your live https URL, e.g.
# https://your-service-name.onrender.com
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running healthy!"

@app.route(WEBHOOK_PATH, methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') != 'application/json':
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
    bot.process_new_updates([update])
    return '', 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is not set. This is auto-provided by Render "
            "in Web Service deploys — if it's missing, check that this is "
            "actually running as a Render Web Service (not a background worker)."
        )

    full_webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

    # Always clear any old webhook/polling state before registering the new
    # one, so a stale hook from a previous deploy can't collide with this one.
    bot.remove_webhook()
    bot.set_webhook(url=full_webhook_url)
    print(f"✅ Webhook registered at {full_webhook_url}")

    print("🚀 Bot server running via webhook (no polling, no 409 conflicts)...")
    run_flask()  # run in the main thread now — this IS the server
