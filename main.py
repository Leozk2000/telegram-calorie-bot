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
import matplotlib
matplotlib.use("Agg")  # headless backend - no display available on Render
import matplotlib.pyplot as plt

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

# In-memory mirror of the sheet's rows (list of dicts, same shape as
# sheet.get_all_records()). Every command reads from this instead of
# calling the Sheets API directly, so a growing sheet doesn't mean a
# growing full-table read on every /total, /delete, or /graph* command.
# This assumes a single running instance, which matches this app's Render
# free-tier deployment — the cache is kept in sync by updating it at the
# same time as every write the bot itself makes (append/update/delete).
# If the sheet is ever edited by hand outside the bot, run /refresh to
# force a full re-sync.
sheet_cache = []

def load_cache():
    """(Re)loads the entire sheet into memory. This is the ONLY place a
    full-table Sheets read happens — at startup, and on-demand via /refresh."""
    global sheet_cache
    sheet_cache = sheet.get_all_records()
    print(f"✅ Cache loaded: {len(sheet_cache)} rows")

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
        logged_row = None  # row number in the sheet, used later to attach the price
        try:
            match = re.search(r'```json\s*(\{.*?\})\s*```', full_text, re.DOTALL)
            if match:
                json_string = match.group(1)
                data = json.loads(json_string)
                
                new_row = {
                    "Date": str(now_sgt().strftime("%Y-%m-%d %H:%M:%S")),
                    "Telegram_ID": str(message.from_user.id),
                    "Meal": data.get("meal"),
                    "Calories": data.get("calories"),
                    "Protein": data.get("protein"),
                    "Carbs": data.get("carbs"),
                    "Fats": data.get("fat"),
                    "Price": "",  # filled in later by the price follow-up, if answered
                }
                append_result = sheet.append_row([
                    new_row["Date"], new_row["Telegram_ID"], new_row["Meal"],
                    new_row["Calories"], new_row["Protein"], new_row["Carbs"], new_row["Fats"]
                ])
                # gspread's append_row response includes something like
                # {"updates": {"updatedRange": "Sheet1!A12:G12", ...}}.
                # We pull the row number out of that so we can target the
                # exact same row later when the price comes back, without
                # re-scanning the whole sheet.
                updated_range = append_result.get("updates", {}).get("updatedRange", "")
                row_match = re.search(r'![A-Z]+(\d+)', updated_range)
                if row_match:
                    logged_row = int(row_match.group(1))
                    sheet_cache.append(new_row)  # keep the in-memory cache in sync
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

        # Follow up asking for the price, but only if we know which row to
        # attach it to. The reply is validated with a strict regex and cast
        # to a float before it ever reaches Gemini or the sheet — it's never
        # treated as an instruction or a formula, just a number.
        if logged_row is not None:
            prompt_msg = bot.send_message(
                message.chat.id,
                "💰 How much did this meal cost? Reply with a number (e.g. 12.50), or send /skip."
            )
            bot.register_next_step_handler(prompt_msg, handle_price_reply, logged_row)
        
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


# Matches an optional leading "$", digits, and an optional 1-2 decimal
# places — nothing else. Anything that doesn't fully match this (extra
# words, symbols, formula-like prefixes such as "=", "+", "@") is rejected
# outright rather than partially parsed.
PRICE_PATTERN = re.compile(r'^\$?\d+(\.\d{1,2})?$')

def handle_price_reply(message, row_number):
    """
    Next-step handler for the price follow-up. The reply is treated purely
    as data, never as an instruction: it's validated against a strict regex
    and converted to a float before being written anywhere. It never gets
    passed to Gemini, and writing it as a float (not a raw string) means
    Sheets can't misinterpret it as a formula even if someone typed
    something formula-shaped.
    """
    text = (message.text or "").strip()

    # Batch uploads aren't supported: if a second photo comes in while we're
    # still waiting on the price for the previous one, telebot's next-step
    # mechanism routes it here (not to the photo handler) since it's the
    # very next message in this chat. Reject it and re-ask for the pending
    # price instead of silently dropping or processing the new photo.
    if message.content_type == 'photo':
        retry_msg = bot.reply_to(
            message,
            "⚠️ Please answer the price for your last meal first (or send /skip), "
            "before uploading another photo."
        )
        bot.register_next_step_handler(retry_msg, handle_price_reply, row_number)
        return

    # Let /delete or /undo interrupt the price flow too — this is exactly
    # the "I uploaded the wrong photo" moment, so it should work here rather
    # than being swallowed as an invalid price reply.
    if text.lower() in ("/delete", "/undo"):
        handle_delete_last_entry(message)
        return

    if text.lower() == "/skip":
        bot.reply_to(message, "Okay, skipped — no price logged for this meal.")
        return

    if not PRICE_PATTERN.match(text):
        retry_msg = bot.reply_to(
            message,
            "⚠️ That doesn't look like a plain number. Please reply with just the amount "
            "(e.g. 12.50), or send /skip."
        )
        bot.register_next_step_handler(retry_msg, handle_price_reply, row_number)
        return

    price = float(text.lstrip("$"))

    try:
        # Column 8 = "Price", one column to the right of the existing
        # Date/Telegram_ID/Meal/Calories/Protein/Carbs/Fats columns (A-G).
        sheet.update_cell(row_number, 8, price)
        cache_index = row_number - 2  # -1 for the header row, -1 for 0-indexing
        if 0 <= cache_index < len(sheet_cache):
            sheet_cache[cache_index]["Price"] = price
        bot.reply_to(message, f"💰 Logged ${price:.2f} for this meal.")
    except Exception as e:
        print(f"⚠️ Failed to log price: {e}")
        bot.reply_to(message, f"❌ Couldn't save the price to the sheet.\nReason: `{str(e)}`", parse_mode="Markdown")


# HANDLER 2: Delete Last Entry (with ownership + time-window guardrails)
# Usage: /delete or /undo — removes the sender's own most recent entry,
# but only if it's less than 1 hour old.
DELETE_WINDOW = timedelta(hours=1)

def handle_delete_last_entry(message):
    try:
        user_id = str(message.from_user.id)
        records = sheet_cache

        # Scan for the last row belonging to this user. Since entries are
        # appended in order, the last match is the most recent one — we
        # never touch a row that isn't this user's, so there's no way to
        # delete someone else's entry even by accident.
        target_index = None
        for i, row in enumerate(records):
            if str(row.get("Telegram_ID", "")) == user_id:
                target_index = i

        if target_index is None:
            bot.reply_to(message, "You don't have any logged meals to delete.")
            return

        row_data = records[target_index]
        row_number = target_index + 2  # +1 for the header row, +1 for 1-indexing

        try:
            logged_time = datetime.strptime(str(row_data.get("Date", "")), "%Y-%m-%d %H:%M:%S")
            logged_time = logged_time.replace(tzinfo=SGT)
        except ValueError:
            bot.reply_to(message, "⚠️ Couldn't read the timestamp on that entry, so it won't be deleted.")
            return

        age = now_sgt() - logged_time
        if age > DELETE_WINDOW:
            bot.reply_to(
                message,
                f"⏳ Your most recent entry (\"{row_data.get('Meal', 'that meal')}\") is over an hour "
                "old, so it can no longer be deleted with this command."
            )
            return

        sheet.delete_rows(row_number)
        del sheet_cache[target_index]  # keep the in-memory cache in sync
        bot.reply_to(message, f"🗑️ Deleted your last entry: \"{row_data.get('Meal', 'that meal')}\".")

    except Exception as e:
        print(f"⚠️ Failed to delete entry: {e}")
        error_message = f"❌ *Couldn't delete the entry*\n\nReason:\n`{str(e)}`"
        try:
            bot.reply_to(message, error_message, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, error_message)


@bot.message_handler(commands=['delete', 'undo'])
def handle_delete_command(message):
    handle_delete_last_entry(message)


# HANDLER 2b: Manual cache refresh
# Usage: /refresh — forces a full re-sync of the in-memory cache from the
# sheet. Only needed if you've edited the sheet by hand outside the bot;
# normal bot usage (photo -> price -> delete) keeps the cache in sync on
# its own.
@bot.message_handler(commands=['refresh'])
def handle_refresh_cache(message):
    try:
        load_cache()
        bot.reply_to(message, f"🔄 Cache refreshed — {len(sheet_cache)} rows loaded from the sheet.")
    except Exception as e:
        print(f"⚠️ Failed to refresh cache: {e}")
        bot.reply_to(message, f"❌ Couldn't refresh the cache.\nReason: `{str(e)}`", parse_mode="Markdown")


# Shared helper: pulls the numeric fields out of a sheet row safely.
# Missing/blank values (e.g. Price wasn't logged for older entries) come
# back as 0.0 rather than raising, so totals and charts degrade gracefully
# instead of crashing on incomplete rows.
def parse_row_values(row):
    def to_float(key):
        val = row.get(key, "")
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    return {
        "calories": to_float("Calories"),
        "protein": to_float("Protein"),
        "carbs": to_float("Carbs"),
        "fat": to_float("Fats"),
        "price": to_float("Price"),
    }


# HANDLER 3: Daily Tally Command
# Usage:
#   /total            -> tallies today's meals (SGT)
#   /total 2026-09-09 -> tallies meals for that specific date (SGT)
@bot.message_handler(commands=['total'])
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

        records = sheet_cache  # in-memory cache, no full-table Sheets read

        total_cal = total_protein = total_carbs = total_fat = total_price = 0.0
        meal_count = 0

        for row in records:
            row_date = str(row.get("Date", ""))[:10]
            row_user = str(row.get("Telegram_ID", ""))
            if row_date == target_date and row_user == user_id:
                vals = parse_row_values(row)
                total_cal += vals["calories"]
                total_protein += vals["protein"]
                total_carbs += vals["carbs"]
                total_fat += vals["fat"]
                total_price += vals["price"]
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
            f"🥑 Fat: {total_fat:.0f} g\n"
            f"💰 Total spent: ${total_price:.2f}"
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


def build_line_chart(title, x_labels, calories, protein, carbs, fat, price):
    """
    Builds a two-panel PNG chart and returns it as an in-memory buffer ready
    for bot.send_photo().

    Top panel: Calories on its own left axis, Protein/Carbs/Fat (grams) on
    a shared right axis via twinx(). Bottom panel: Price on its own axis.
    Splitting it this way keeps calories (hundreds) from squashing grams
    (tens) or price (single/double digits) flat on one shared scale.
    """
    x = list(range(len(x_labels)))
    fig, (ax_nutrition, ax_price) = plt.subplots(
        2, 1, figsize=(8, 7), gridspec_kw={"height_ratios": [2, 1]}
    )

    ax_cal = ax_nutrition
    ax_macro = ax_nutrition.twinx()

    ax_cal.plot(x, calories, color="#e6550d", marker="o", label="Calories (kcal)")
    ax_macro.plot(x, protein, color="#3182bd", marker="o", label="Protein (g)")
    ax_macro.plot(x, carbs, color="#31a354", marker="o", label="Carbs (g)")
    ax_macro.plot(x, fat, color="#756bb1", marker="o", label="Fat (g)")

    ax_cal.set_ylabel("Calories (kcal)", color="#e6550d")
    ax_macro.set_ylabel("Grams")
    ax_cal.set_title(title)
    ax_cal.set_xticks(x)
    ax_cal.set_xticklabels(x_labels, fontsize=8)
    ax_cal.grid(alpha=0.25)

    lines_1, labels_1 = ax_cal.get_legend_handles_labels()
    lines_2, labels_2 = ax_macro.get_legend_handles_labels()
    ax_cal.legend(lines_1 + lines_2, labels_1 + labels_2, loc="upper left", fontsize=8)

    ax_price.plot(x, price, color="#c51b8a", marker="o", label="Price ($)")
    ax_price.set_ylabel("Price ($)")
    ax_price.set_xticks(x)
    ax_price.set_xticklabels(x_labels, fontsize=8)
    ax_price.grid(alpha=0.25)
    ax_price.legend(loc="upper left", fontsize=8)

    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)  # free the figure - important on a long-running server
    buf.seek(0)
    return buf


# HANDLER 4: Daily Graph — nutrition & price across each meal in a day
# Usage:
#   /graphdaily            -> today's meals (SGT)
#   /graphdaily 2026-09-09 -> meals for that specific date (SGT)
@bot.message_handler(commands=['graphdaily'])
def handle_graph_daily(message):
    try:
        user_id = str(message.from_user.id)

        parts = message.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            target_date = parts[1].strip()
            try:
                datetime.strptime(target_date, "%Y-%m-%d")
            except ValueError:
                bot.reply_to(message, "⚠️ Please use the format `/graphdaily YYYY-MM-DD`.", parse_mode="Markdown")
                return
        else:
            target_date = now_sgt().strftime("%Y-%m-%d")

        records = sheet_cache  # in-memory cache, no full-table Sheets read
        day_rows = [
            row for row in records
            if str(row.get("Date", ""))[:10] == target_date and str(row.get("Telegram_ID", "")) == user_id
        ]

        if not day_rows:
            bot.reply_to(message, f"No meals logged for {target_date}. 🍽️")
            return

        # Rows come out of the sheet in the order they were logged, which
        # is already chronological, but sort explicitly to be safe.
        day_rows.sort(key=lambda r: str(r.get("Date", "")))

        labels, calories, protein, carbs, fat, price = [], [], [], [], [], []
        for row in day_rows:
            time_str = str(row.get("Date", ""))[11:16] or "?"
            meal_name = str(row.get("Meal", "Meal"))[:14]
            labels.append(f"{time_str}\n{meal_name}")
            vals = parse_row_values(row)
            calories.append(vals["calories"])
            protein.append(vals["protein"])
            carbs.append(vals["carbs"])
            fat.append(vals["fat"])
            price.append(vals["price"])

        chart_buf = build_line_chart(
            title=f"Meals on {target_date}",
            x_labels=labels, calories=calories, protein=protein,
            carbs=carbs, fat=fat, price=price
        )
        bot.send_photo(message.chat.id, chart_buf, caption=f"📈 Nutrition & spend across meals — {target_date}")

    except Exception as e:
        print(f"⚠️ Failed to build daily graph: {e}")
        error_message = f"❌ *Couldn't build the daily graph*\n\nReason:\n`{str(e)}`"
        try:
            bot.reply_to(message, error_message, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, error_message)


# HANDLER 5: Weekly Graph — daily totals over the last 7 days
@bot.message_handler(commands=['graphweekly'])
def handle_graph_weekly(message):
    try:
        user_id = str(message.from_user.id)
        records = sheet_cache  # in-memory cache, no full-table Sheets read

        today = now_sgt().date()
        day_range = [today - timedelta(days=offset) for offset in range(6, -1, -1)]  # oldest -> newest

        daily_totals = {
            d.strftime("%Y-%m-%d"): {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "price": 0.0}
            for d in day_range
        }

        for row in records:
            if str(row.get("Telegram_ID", "")) != user_id:
                continue
            row_date = str(row.get("Date", ""))[:10]
            if row_date in daily_totals:
                vals = parse_row_values(row)
                for key in daily_totals[row_date]:
                    daily_totals[row_date][key] += vals[key]

        if all(sum(day.values()) == 0 for day in daily_totals.values()):
            bot.reply_to(message, "No meals logged in the past 7 days. 🍽️")
            return

        labels = [d.strftime("%a\n%d %b") for d in day_range]
        calories = [daily_totals[d.strftime("%Y-%m-%d")]["calories"] for d in day_range]
        protein = [daily_totals[d.strftime("%Y-%m-%d")]["protein"] for d in day_range]
        carbs = [daily_totals[d.strftime("%Y-%m-%d")]["carbs"] for d in day_range]
        fat = [daily_totals[d.strftime("%Y-%m-%d")]["fat"] for d in day_range]
        price = [daily_totals[d.strftime("%Y-%m-%d")]["price"] for d in day_range]

        chart_buf = build_line_chart(
            title="Past 7 Days — Daily Totals",
            x_labels=labels, calories=calories, protein=protein,
            carbs=carbs, fat=fat, price=price
        )
        bot.send_photo(message.chat.id, chart_buf, caption="📈 Daily nutrition & spend totals — last 7 days")

    except Exception as e:
        print(f"⚠️ Failed to build weekly graph: {e}")
        error_message = f"❌ *Couldn't build the weekly graph*\n\nReason:\n`{str(e)}`"
        try:
            bot.reply_to(message, error_message, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            bot.reply_to(message, error_message)


# HANDLER 6: Text Response Assistant
@bot.message_handler(content_types=['text'])
def handle_text_fallback(message):
    feedback = (
        "🍳 *Calorie Tracker Bot Ready!*\n\n"
        "Please upload a **photo** of your plate. "
        "The AI will evaluate macros and save them straight to your tracking sheet! 📊\n\n"
        "Send /total to see today's tally, or /total YYYY-MM-DD for a specific day.\n"
        "Send /graphdaily for a chart of today's meals, or /graphweekly for the last 7 days.\n"
        "Uploaded the wrong photo? Send /delete within an hour to remove it.\n"
        "Edited the sheet by hand? Send /refresh to re-sync."
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

    # Populates the "/" command menu shown in the Telegram chat UI (tap the
    # icon next to the message box, or type "/" to see it pop up). This is
    # purely cosmetic/discoverability — it doesn't affect routing, so the
    # @bot.message_handler(commands=[...]) handlers still do the real work.
    bot.set_my_commands([
        telebot.types.BotCommand("total", "Today's nutrition & spend tally"),
        telebot.types.BotCommand("graphdaily", "Graph today's meals"),
        telebot.types.BotCommand("graphweekly", "Graph the last 7 days"),
        telebot.types.BotCommand("delete", "Delete your last entry (within 1h)"),
        telebot.types.BotCommand("refresh", "Reload cache after a manual sheet edit"),
    ])
    print("✅ Command menu registered")

    # Load the sheet into memory once here. From this point on, every
    # command reads from sheet_cache instead of hitting the Sheets API,
    # and bot-driven writes (photo log, price, delete) keep it in sync.
    load_cache()

    print("🚀 Bot server running via webhook (no polling, no 409 conflicts)...")
    run_flask()  # run in the main thread now — this IS the server
