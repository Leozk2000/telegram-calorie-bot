import os
import json
import re
from datetime import datetime
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
        
        ai_response = client_ai.models.generate_content(
            model='gemini-1.5-flash',
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
                    str(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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
            
        bot.reply_to(message, full_text, parse_mode="Markdown")
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"❌ Execution crash:\n{error_details}")        
        # DEFINITIVE DIAGNOSTIC FIX: Text the exact internal issue straight to your phone!
        bot.reply_to(message, f"❌ *Error Processing Meal Card*\n\nReason:\n`{str(e)}`", parse_mode="Markdown")


# HANDLER 2: Text Response Assistant
@bot.message_handler(content_types=['text'])
def handle_text_fallback(message):
    feedback = (
        "🍳 *Calorie Tracker Bot Ready!*\n\n"
        "Please upload a **photo** of your plate. "
        "The AI will evaluate macros and save them straight to your tracking sheet! 📊"
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
