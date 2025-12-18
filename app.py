import os
import sqlite3
import json
import logging
import datetime
import secrets
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor

# --- ১. কনফিগারেশন ---
# Render Environment Variables থেকে লোড হবে
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8320840106:AAF9P0LhVzcvvu-UGxWirLmaRKUm-P2Y9Zw")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "YOUR_BLOGGER_PUBLIC_URL_HERE") 
RENDER_URL = os.environ.get("RENDER_URL", "https://earnquick-bot.onrender.com/") 
BOT_USERNAME = "@EarnQuick_Official_bot"
SPONSOR_CHANNEL = "https://t.me/EarnQuickOfficial"
ADMIN_USER_ID = 8145444675 # আপনার অ্যাডমিন আইডি সেট করা হয়েছে

# আয়ের নিয়ম
AD_INCOME = 20.00          
DAILY_AD_LIMIT = 300       
REFERRAL_BONUS_TK = 125.00 
POINT_TO_TK_RATIO = 5000 / 20 
MIN_WITHDRAW_POINTS = 50000 
AD_TOKEN_TIMEOUT_SECONDS = 60 

DB_NAME = 'user_data.db'

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ২. ডেটাবেস ফাংশন ---

def initialize_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Users Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.00, 
            daily_ads_seen INTEGER DEFAULT 0,
            total_referrals INTEGER DEFAULT 0,
            referred_by INTEGER,
            last_ad_date TEXT 
        )
    ''')
    # Withdrawal Requests Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            method TEXT,
            number TEXT,
            status TEXT DEFAULT 'Pending',
            request_date TEXT
        )
    ''')
    # Ad Tokens Table (For Security)
    c.execute('''
        CREATE TABLE IF NOT EXISTS ad_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            created_at REAL,
            is_used BOOLEAN DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    data = c.fetchone()
    conn.close()
    return data

def create_user(user_id, username, referred_by=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)", 
                  (user_id, username, 0.00, 0, 0, referred_by, str(datetime.date.today())))
        conn.commit()
        if referred_by and referred_by != user_id:
            bonus_points = REFERRAL_BONUS_TK * POINT_TO_TK_RATIO 
            c.execute("UPDATE users SET balance = balance + ?, total_referrals = total_referrals + 1 WHERE user_id = ?", 
                      (bonus_points, referred_by))
            conn.commit()
    except Exception as e:
        logger.error(f"Error creating user: {e}")
    finally:
        conn.close()

def generate_ad_token(user_id):
    token = secrets.token_urlsafe(16)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO ad_tokens VALUES (?, ?, ?, ?)", 
              (token, user_id, datetime.datetime.now().timestamp(), 0))
    conn.commit()
    conn.close()
    return token

def verify_and_update_ad_status(user_id, ad_token):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT * FROM ad_tokens WHERE token = ? AND user_id = ? AND is_used = 0", (ad_token, user_id))
    token_data = c.fetchone()
    
    if not token_data:
        conn.close()
        return False, "Invalid or already used token."

    token_time = token_data[2]
    current_time = datetime.datetime.now().timestamp()
    
    if (current_time - token_time) > AD_TOKEN_TIMEOUT_SECONDS:
        conn.close()
        return False, "Token expired."

    today = str(datetime.date.today())
    c.execute("UPDATE users SET daily_ads_seen = 0 WHERE user_id = ? AND last_ad_date != ?", (user_id, today))
    conn.commit()

    c.execute("SELECT balance, daily_ads_seen FROM users WHERE user_id = ?", (user_id,))
    user_data = c.fetchone()
    ads_seen = user_data[1]

    if ads_seen < DAILY_AD_LIMIT:
        c.execute("UPDATE users SET balance = balance + ?, daily_ads_seen = daily_ads_seen + 1, last_ad_date = ? WHERE user_id = ?", 
                  (AD_INCOME, today, user_id))
        c.execute("UPDATE ad_tokens SET is_used = 1 WHERE token = ?", (ad_token,))
        conn.commit()
        conn.close()
        return True, ads_seen + 1
    else:
        conn.close()
        return False, "Quota exceeded."

def submit_withdrawal_request(user_id, amount, method, number):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    current_balance = c.fetchone()[0]

    if current_balance < amount or amount < MIN_WITHDRAW_POINTS:
        conn.close()
        return False, "Insufficient balance or minimum limit not met."

    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    c.execute("INSERT INTO withdrawal_requests (user_id, amount, method, number, request_date) VALUES (?, ?, ?, ?, ?)",
              (user_id, amount, method, number, str(datetime.datetime.now())))
    
    conn.commit()
    conn.close()
    return True, "Request submitted."


# --- ৩. টেলিগ্রাম হ্যান্ডলার্স ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    username = user.username if user.username else user.first_name
    
    referred_by = None
    if context.args:
        try:
            referred_by = int(context.args[0])
            if referred_by == user.id: referred_by = None
        except ValueError: pass 

    if not get_user_data(user.id):
        create_user(user.id, username, referred_by)

    web_app_button = InlineKeyboardButton(
        text="💰 ইনকাম শুরু করুন 💰",
        web_app=WebAppInfo(url=WEB_APP_URL)
    )
    
    keyboard = InlineKeyboardMarkup([
        [web_app_button],
        [InlineKeyboardButton("🔗 স্পন্সর চ্যানেল", url=SPONSOR_CHANNEL)]
    ])

    await update.message.reply_html(
        f"✅ স্বাগতম **{user.first_name}**!\n\n"
        f"**পয়েন্ট রেট:** {int(POINT_TO_TK_RATIO)} পয়েন্ট = ১ টাকা।",
        reply_markup=keyboard
    )

async def handle_mini_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
        action = data.get("action")
        
        if action == "ad_completed":
            ad_token = data.get("ad_token")
            success, result = verify_and_update_ad_status(user_id, ad_token)
            
            if success:
                ads_seen = result
                await update.message.reply_text(
                    f"🎉 সফল! আপনি {AD_INCOME:.2f} পয়েন্ট আয় করেছেন।\n"
                    f"আজকের বিজ্ঞাপন দেখা হয়েছে: {ads_seen}/{DAILY_AD_LIMIT}"
                )
            else:
                await update.message.reply_text(f"⚠️ বিজ্ঞাপন ভেরিফিকেশন ব্যর্থ: {result}")
        
        elif action == "withdraw_request":
            amount = float(data.get("amount"))
            method = data.get("method")
            number = data.get("number")
            
            success, result = submit_withdrawal_request(user_id, amount, method, number)

            if success:
                await application.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=f"💸 নতুন উইথড্রয়াল অনুরোধ!\nইউজার ID: {user_id}\nপয়েন্ট: {amount:.2f}\nটাকা: {amount / POINT_TO_TK_RATIO:.2f} ৳\nমেথড: {method} - {number}"
                )
                await update.message.reply_text("✅ আপনার উইথড্রয়াল অনুরোধ সফলভাবে জমা হয়েছে।")
            else:
                await update.message.reply_text(f"❌ উইথড্রয়াল ব্যর্থ: {result}")
        
    except Exception as e:
        logger.error(f"Error handling mini app data: {e}")
        await update.message.reply_text("ডেটা প্রসেস করতে ত্রুটি হয়েছে।")


# --- ৪. ফ্লাস্ক ওয়েব সার্ভার (API) ---

flask_app = Flask(__name__)
PORT = int(os.environ.get('PORT', 5000))

@flask_app.route('/webhook', methods=['POST'])
async def webhook_handler():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        executor.submit(lambda: application.update_queue.put_nowait(update))
        return "ok"
    return "ok"

@flask_app.route('/data', methods=['GET'])
def get_dashboard_data():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "User ID required"}), 400
    
    data = get_user_data(int(user_id))
    if not data:
        data = [int(user_id), 'N/A', 0.00, 0, 0, None, str(datetime.date.today())]

    user_data = {
        "user_id": data[0], "balance": f"{data[2]:.2f}", "daily_ads_seen": data[3], "total_referrals": data[4],
        "daily_ad_limit": DAILY_AD_LIMIT, "ad_income": AD_INCOME, "referral_bonus_tk": REFERRAL_BONUS_TK,
        "min_withdraw_points": MIN_WITHDRAW_POINTS 
    }
    return jsonify(user_data)

@flask_app.route('/get_ad_token', methods=['GET'])
def get_ad_token():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "User ID required"}), 400
        
    token = generate_ad_token(int(user_id))
    return jsonify({"token": token, "timeout": AD_TOKEN_TIMEOUT_SECONDS})


# টেলিগ্রাম বট সেটআপ
application = Application.builder().token(BOT_TOKEN).updater(None).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_mini_app_data))

executor = ThreadPoolExecutor(max_workers=4)
app = flask_app 

@flask_app.before_request
def before_request_check():
    if not os.path.exists(DB_NAME):
        initialize_db()

def setup_webhook():
    webhook_url = f"{RENDER_URL}webhook"
    application.bot.set_webhook(url=webhook_url) 
    logger.info(f"Webhook set to: {webhook_url}")

if os.environ.get("RENDER"):
    setup_webhook()
