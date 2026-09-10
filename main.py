import os
import json
from datetime import datetime
import threading
import telebot
import google.genai as genai
import gspread
from PIL import Image
import requests
from io import BytesIO
from flask import Flask

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

@bot.message_handler(content_types=['photo'])
def handle_food_photo(message):
    try:
        print(f"📸 Received photo from User ID {message.from_user.id}. Starting AI evaluation...")
        bot.reply_to(message, "Analyzing your meal... 🔍")
        
        # 1. Download photo from Telegram API cleanly
        file_info = bot.get_file(message.photo[-1].file_id)
        file_url = f"https://telegram.org{TELEGRAM_TOKEN}/{file_info.file_path}"
        response = requests.get(file_url)
        img = Image.open(BytesIO(response.content))
        
        # 2. Call Gemini API for cloud vision processing
        ai_response = client_ai.models.generate_content(
            model='gemini-1.5-flash',
            contents=[img, SYSTEM_PROMPT]
        )
        full_text = ai_response.text
        
        # 3. FIXED TEXT PARSING: Safely extract JSON data for Google Sheets
        try:
            if "```json" in full_text:
                # Split text cleanly using index placement instead of double-splitting a list
                parts = full_text.split("```json")
                json_string = parts[1].split("```")[0].strip()
                data = json.loads(json_string)
                
                # Append rows to Google Sheet logs
                sheet.append_row([
                    str(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    str(message.from_user.id),
                    data.get("meal"),
                    data.get("calories"),
                    data.get("protein"),
                    data.get("carbs"),
                    data.get("fat")
                ])
                print("✅ Log successfully saved to Google Sheets.")
        except Exception as sheet_error:
            print(f"⚠️ Google Sheets entry skipped or failed: {sheet_error}")
            
        # 4. Reply back to your phone with the pretty text card layout
        bot.reply_to(message, full_text, parse_mode="Markdown")
        
    except Exception as e:
        print(f"❌ Core processing error: {e}")
        bot.reply_to(message, f"Error processing meal: {str(e)}")


# PORT BINDING FIX: Create a tiny dummy web page for Render's scanner
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is running healthy!"

def run_flask():
    # Render automatically injects a PORT variable into the environment
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # STARTUP FIXED LOOP: Destroys ghost connections before launching the main thread
    try:
        print("🧼 Cleaning up old server connections...")
        bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"Webhook cleanup warning: {e}")
        
    # Start the web page scanner helper in a background thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("🚀 Bot server running with active web listener...")
    # Fixes the 409 error by instructing Telegram to kick off any old stale containers
    bot.infinity_polling(skip_pending=True)

