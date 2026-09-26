import os
import re
import sqlite3
import threading
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ChatMemberHandler
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
BOOTSTRAP_CODE = os.environ.get("BOOTSTRAP_CODE", "").strip()
DB_PATH = os.environ.get("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("groupbot")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS managers(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS settings(
    chat_id INTEGER PRIMARY KEY
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    action TEXT,
    details TEXT,
    created_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS watched_groups(
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT
)
""")
db.commit()

SETTING_FIELDS = [
    "links", "photos", "videos", "audio", "files", "stickers", "gif",
    "username", "tag", "bots", "keyboard", "games", "repeat",
    "join_lock", "entry", "add_lock", "notifications", "markdown", "edit"
]

DEFAULTS = {field: 0 for field in SETTING_FIELDS}
DEFAULTS["notifications"] = 1

def now():
    return datetime.now(timezone.utc).isoformat()

def ensure_settings(chat_id):
    cols = ", ".join(f'"{f}" INTEGER DEFAULT {DEFAULTS[f]}' for f in SETTING_FIELDS)
    # SQLite cannot ALTER a missing table definition in one statement, so
    # create a fresh table shape when possible and migrate old installations.
    existing = db.execute("PRAGMA table_info(settings)").fetchall()
    existing_names = {row[1] for row in existing}
    if "chat_id" not in existing_names:
        db.execute("DROP TABLE IF EXISTS settings")
        db.execute(f"CREATE TABLE settings(chat_id INTEGER PRIMARY KEY, {cols})")
        db.commit()
        existing_names = {"chat_id", *SETTING_FIELDS}
    for field in SETTING_FIELDS:
        if field not in existing_names:
            db.execute(f'ALTER TABLE settings ADD COLUMN "{field}" INTEGER DEFAULT {DEFAULTS[field]}')
    row = db.execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        db.execute("INSERT INTO settings(chat_id) VALUES(?)", (chat_id,))
        db.commit()

# Migrate the small settings table used by the first version.
ensure_settings(0)
if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
    cols = {r[1] for r in db.execute("PRAGMA table_info(settings)").fetchall()}
    for field in SETTING_FIELDS:
        if field not in cols:
            db.execute(f'ALTER TABLE settings ADD COLUMN "{field}" INTEGER DEFAULT {DEFAULTS[field]}')
    db.commit()

def get_settings(chat_id):
    ensure_settings(chat_id)
    fields = ", ".join(f'"{f}"' for f in SETTING_FIELDS)
    row = db.execute(f"SELECT {fields} FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(zip(SETTING_FIELDS, row))

def set_setting(chat_id, field, value):
    if field not in SETTING_FIELDS:
        return
    ensure_settings(chat_id)
    db.execute(f'UPDATE settings SET "{field}"=? WHERE chat_id=?', (int(bool(value)), chat_id))
    db.commit()

def log_action(chat_id, user_id, action, details=""):
    try:
        db.execute(
            "INSERT INTO logs(chat_id,user_id,action,details,created_at) VALUES(?,?,?,?,?)",
            (chat_id, user_id, action, details[:500], now())
        )
        db.commit()
    except Exception:
        pass

def is_owner(uid):
    return bool(OWNER_ID and uid == OWNER_ID)

def is_manager(uid):
    if is_owner(uid):
        return True
    return bool(db.execute("SELECT 1 FROM managers WHERE user_id=?", (uid,)).fetchone())

async def require_admin(update):
    if not update.effective_user or not update.effective_chat:
        return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("ÙØ°Ø§ Ø§ÙØ£ÙØ± ÙØ¹ÙÙ Ø¯Ø§Ø®Ù Ø§ÙÙØ±ÙØ¨ ÙÙØ·.")
        return False
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("â ÙÙØ³ ÙØ¯ÙÙ ØµÙØ§Ø­ÙØ©.")
        return False
    return True

def panel_markup(owner=False):
    rows = [
        [InlineKeyboardButton("ð¡ï¸ Ø§ÙØ­ÙØ§ÙØ©", callback_data="security"),
         InlineKeyboardButton("ð¥ Ø§ÙÙØ¯Ø±Ø§Ø¡", callback_data="managers")],
        [InlineKeyboardButton("ð¢ Ø§ÙÙØ³ØªØ¬Ø¯Ø§Øª", callback_data="alerts"),
         InlineKeyboardButton("ð Ø§ÙØ³Ø¬Ù", callback_data="logs")],
        [InlineKeyboardButton("âï¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§ÙÙØ±ÙØ¨", callback_data="settings")]
    ]
    if owner:
        rows.append([InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© ÙØ¯ÙØ±", callback_data="add_manager_help")])
    return InlineKeyboardMarkup(rows)

async def start(update, context):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"ÙØ±Ø­Ø¨ÙØ§ ð\nØ±ÙÙ Ø­Ø³Ø§Ø¨Ù: {update.effective_user.id}\n\n"
            "Ø§Ø³ØªØ®Ø¯Ù /panel ÙÙØªØ­ ÙÙØ­Ø© Ø§ÙØªØ­ÙÙ."
        )
    else:
        await update.effective_message.reply_text("ØªÙ ØªØ´ØºÙÙ Ø§ÙØ¨ÙØª â\nØ§Ø³ØªØ®Ø¯Ù /panel ÙÙÙØ­Ø© Ø§ÙØªØ­ÙÙ.")

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "ð Ø£ÙØ§ÙØ± Ø§ÙØ¨ÙØª:\n\n"
        "ð /panel â ÙÙØ­Ø© Ø§ÙØªØ­ÙÙ\n"
        "ð¥ /addmanager /delmanager /managers\n"
        "ð¨ /ban /unban /kick /mute /unmute /del /pin\n"
        "ð¡ï¸ /locks â ÙØ§Ø¦ÙØ© Ø§ÙØ­ÙØ§ÙØ©\n"
        "âï¸ /settings â Ø­Ø§ÙØ© Ø§ÙØ­ÙØ§ÙØ©\n"
        "ð /id /idgroup\n\n"
        "ÙÙÙÙ Ø£ÙØ¶ÙØ§ ÙØªØ§Ø¨Ø© Ø£ÙØ§ÙØ± Ø¹Ø±Ø¨ÙØ© ÙØ±Ø³Ø§Ø¦Ù Ø¹Ø§Ø¯ÙØ©Ø ÙØ«Ù:\n"
        "ÙÙØ¹ Ø§ÙØ±ÙØ§Ø¨Ø·Ø ÙÙØ¹ Ø§ÙØµÙØ±Ø ÙÙØ¹ Ø§ÙÙÙØ¯ÙÙØ ÙÙØ¹ Ø§ÙÙÙÙØ§ØªØ Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ±ÙØ§Ø¨Ø·."
    )

async def panel(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("â ÙÙØ³ ÙØ¯ÙÙ ØµÙØ§Ø­ÙØ© Ø§Ø³ØªØ®Ø¯Ø§Ù ÙÙØ­Ø© Ø§ÙØ¥Ø¯Ø§Ø±Ø©.")
        return
    await update.effective_message.reply_text(
        "ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ¨ÙØª\n\nØ§Ø®ØªØ± Ø§ÙÙØ³Ù Ø§ÙÙØ·ÙÙØ¨:",
        reply_markup=panel_markup(is_owner(update.effective_user.id))
    )

async def addmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("â ÙØ°Ø§ Ø§ÙØ£ÙØ± ÙÙÙØ§ÙÙ ÙÙØ·.")
        return
    if not context.args:
        await update.effective_message.reply_text("Ø§ÙØ§Ø³ØªØ®Ø¯Ø§Ù: /addmanager Ø±ÙÙ_Ø§ÙÙØ³ØªØ®Ø¯Ù")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Ø±ÙÙ Ø§ÙÙØ³ØªØ®Ø¯Ù ØºÙØ± ØµØ­ÙØ­.")
        return
    db.execute(
        "INSERT OR REPLACE INTO managers(user_id,added_by,added_at) VALUES(?,?,?)",
        (uid, update.effective_user.id, now())
    )
    db.commit()
    await update.effective_message.reply_text(f"â ØªÙØª Ø¥Ø¶Ø§ÙØ© Ø§ÙÙØ¯ÙØ±: {uid}")

async def delmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("â ÙØ°Ø§ Ø§ÙØ£ÙØ± ÙÙÙØ§ÙÙ ÙÙØ·.")
        return
    if not context.args:
        await update.effective_message.reply_text("Ø§ÙØ§Ø³ØªØ®Ø¯Ø§Ù: /delmanager Ø±ÙÙ_Ø§ÙÙØ³ØªØ®Ø¯Ù")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Ø±ÙÙ Ø§ÙÙØ³ØªØ®Ø¯Ù ØºÙØ± ØµØ­ÙØ­.")
        return
    db.execute("DELETE FROM managers WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"â ØªÙ Ø­Ø°Ù Ø§ÙÙØ¯ÙØ±: {uid}")

async def managers(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("â ÙÙØ³ ÙØ¯ÙÙ ØµÙØ§Ø­ÙØ©.")
        return
    rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
    text = "ð¥ Ø§ÙÙØ¯Ø±Ø§Ø¡:\n" + ("\n".join(f"â¢ {r[0]}" for r in rows) if rows else "ÙØ§ ÙÙØ¬Ø¯ ÙØ¯Ø±Ø§Ø¡ Ø¥Ø¶Ø§ÙÙÙÙ.")
    if OWNER_ID:
        text += f"\n\nð Ø§ÙÙØ§ÙÙ: {OWNER_ID}"
    await update.effective_message.reply_text(text)

async def id_cmd(update, context):
    await update.effective_message.reply_text(f"ð Ø±ÙÙÙ: {update.effective_user.id}")

async def idgroup(update, context):
    await update.effective_message.reply_text(f"ð Ø±ÙÙ Ø§ÙÙØ±ÙØ¨: {update.effective_chat.id}")

def target_from_update(update, context):
    target = update.message.reply_to_message.from_user if update.message and update.message.reply_to_message else None
    if target:
        return target
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    return None

async def ban(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù Ø§ÙØ£ÙØ± Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø±Ø³Ø§ÙØ© Ø§ÙØ¹Ø¶Ù Ø£Ù /ban Ø±ÙÙ_Ø§ÙÙØ³ØªØ®Ø¯Ù")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        log_action(update.effective_chat.id, update.effective_user.id, "ban", str(uid))
        await update.message.reply_text("â ØªÙ Ø­Ø¸Ø± Ø§ÙØ¹Ø¶Ù.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± Ø§ÙØ­Ø¸Ø±: {e}")

async def unban(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /unban Ø±ÙÙ_Ø§ÙÙØ³ØªØ®Ø¯Ù Ø£Ù Ø¨Ø§ÙØ±Ø¯.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("â ØªÙ ÙÙ Ø§ÙØ­Ø¸Ø±.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± ÙÙ Ø§ÙØ­Ø¸Ø±: {e}")

async def kick(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /kick Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø±Ø³Ø§ÙØ© Ø§ÙØ¹Ø¶Ù.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("â ØªÙ Ø·Ø±Ø¯ Ø§ÙØ¹Ø¶Ù.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± Ø§ÙØ·Ø±Ø¯: {e}")

async def mute(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /mute Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø±Ø³Ø§ÙØ© Ø§ÙØ¹Ø¶Ù.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text("â ØªÙ ÙØªÙ Ø§ÙØ¹Ø¶Ù.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± Ø§ÙÙØªÙ: {e}")

async def unmute(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /unmute Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø±Ø³Ø§ÙØ© Ø§ÙØ¹Ø¶Ù.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, uid,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True, can_send_videos=True,
                can_send_video_notes=True, can_send_voice_notes=True,
                can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await update.message.reply_text("â ØªÙ ÙÙ Ø§ÙÙØªÙ.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± ÙÙ Ø§ÙÙØªÙ: {e}")

async def del_cmd(update, context):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /del Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø§ÙØ±Ø³Ø§ÙØ© Ø§ÙÙØ±Ø§Ø¯ Ø­Ø°ÙÙØ§.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± Ø§ÙØ­Ø°Ù: {e}")

async def pin(update, context):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Ø§Ø³ØªØ®Ø¯Ù /pin Ø¨Ø§ÙØ±Ø¯ Ø¹ÙÙ Ø§ÙØ±Ø³Ø§ÙØ©.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("ð ØªÙ Ø§ÙØªØ«Ø¨ÙØª.")
    except Exception as e:
        await update.message.reply_text(f"â ØªØ¹Ø°Ø± Ø§ÙØªØ«Ø¨ÙØª: {e}")

LOCK_MAP = {
    "locklinks": "links", "unlocklinks": "links",
    "lockphoto": "photos", "unlockphoto": "photos",
    "lockvideo": "videos", "unlockvideo": "videos",
    "lockaudio": "audio", "unlockaudio": "audio",
    "lockfile": "files", "unlockfile": "files",
    "lockstickers": "stickers", "unlockstickers": "stickers",
    "lockgif": "gif", "unlockgif": "gif",
    "lockusername": "username", "unlockusername": "username",
    "locktag": "tag", "unlocktag": "tag",
    "lockbots": "bots", "unlockbots": "bots",
    "lockkeyboard": "keyboard", "unlockkeyboard": "keyboard",
    "lockgames": "games", "unlockgames": "games",
    "lockrepeat": "repeat", "unlockrepeat": "repeat",
    "lockjoin": "join_lock", "unlockjoin": "join_lock",
    "lockentry": "entry", "unlockentry": "entry",
    "lockadd": "add_lock", "unlockadd": "add_lock",
    "locknotifications": "notifications", "unlocknotifications": "notifications",
    "lockmarkdown": "markdown", "unlockmarkdown": "markdown",
    "lockedit": "edit", "unlockedit": "edit",
}

ARABIC_ALIASES = {
    "ÙÙØ¹ Ø§ÙØ±ÙØ§Ø¨Ø·": ("links", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ±ÙØ§Ø¨Ø·": ("links", 0),
    "ÙÙØ¹ Ø§ÙØµÙØ±": ("photos", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØµÙØ±": ("photos", 0),
    "ÙÙØ¹ Ø§ÙÙÙØ¯ÙÙ": ("videos", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙÙØ¯ÙÙ": ("videos", 0),
    "ÙÙØ¹ Ø§ÙØµÙØª": ("audio", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØµÙØª": ("audio", 0),
    "ÙÙØ¹ Ø§ÙÙÙÙØ§Øª": ("files", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙÙÙØ§Øª": ("files", 0),
    "ÙÙØ¹ Ø§ÙÙÙØµÙØ§Øª": ("stickers", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙÙØµÙØ§Øª": ("stickers", 0),
    "ÙÙØ¹ Ø§ÙÙØªØ­Ø±ÙØ©": ("gif", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙØªØ­Ø±ÙØ©": ("gif", 0),
    "ÙÙØ¹ Ø§ÙÙØ¹Ø±ÙØ§Øª": ("username", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙØ¹Ø±ÙØ§Øª": ("username", 0),
    "ÙÙØ¹ Ø§ÙØªØ§Ù": ("tag", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØªØ§Ù": ("tag", 0),
    "ÙÙØ¹ Ø§ÙØ¨ÙØªØ§Øª": ("bots", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ¨ÙØªØ§Øª": ("bots", 0),
    "ÙÙØ¹ Ø§ÙÙÙØ¨ÙØ±Ø¯": ("keyboard", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙÙÙØ¨ÙØ±Ø¯": ("keyboard", 0),
    "ÙÙØ¹ Ø§ÙØ£ÙØ¹Ø§Ø¨": ("games", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ£ÙØ¹Ø§Ø¨": ("games", 0),
    "ÙÙØ¹ Ø§ÙØªÙØ±Ø§Ø±": ("repeat", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØªÙØ±Ø§Ø±": ("repeat", 0),
    "ÙÙØ¹ Ø§ÙØ¯Ø®ÙÙ": ("join_lock", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ¯Ø®ÙÙ": ("join_lock", 0),
    "ÙÙØ¹ Ø±Ø³Ø§Ø¦Ù Ø§ÙØ¯Ø®ÙÙ": ("entry", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø±Ø³Ø§Ø¦Ù Ø§ÙØ¯Ø®ÙÙ": ("entry", 0),
    "ÙÙØ¹ Ø§ÙØ¥Ø¶Ø§ÙØ©": ("add_lock", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØ¥Ø¶Ø§ÙØ©": ("add_lock", 0),
    "ÙÙØ¹ Ø§ÙØªØ¹Ø¯ÙÙ": ("edit", 1), "Ø§ÙØ³ÙØ§Ø­ Ø¨Ø§ÙØªØ¹Ø¯ÙÙ": ("edit", 0),
}

async def set_lock(update, context, field=None, value=None):
    if not await require_admin(update): return
    if field is None:
        cmd = (update.message.text or "").split()[0].lower().lstrip("/")
        field = LOCK_MAP.get(cmd)
        value = 0 if cmd.startswith("unlock") else 1
    set_setting(update.effective_chat.id, field, value)
    state = "ð ØªÙ Ø§ÙÙÙØ¹." if value else "ð ØªÙ Ø§ÙØ³ÙØ§Ø­."
    await update.effective_message.reply_text(state)
    log_action(update.effective_chat.id, update.effective_user.id, "setting", f"{field}={value}")

async def locks(update, context):
    if not await require_admin(update): return
    s = get_settings(update.effective_chat.id)
    labels = {
        "links":"Ø§ÙØ±ÙØ§Ø¨Ø·","photos":"Ø§ÙØµÙØ±","videos":"Ø§ÙÙÙØ¯ÙÙ","audio":"Ø§ÙØµÙØª","files":"Ø§ÙÙÙÙØ§Øª",
        "stickers":"Ø§ÙÙÙØµÙØ§Øª","gif":"Ø§ÙÙØªØ­Ø±ÙØ©","username":"Ø§ÙÙØ¹Ø±ÙØ§Øª","tag":"Ø§ÙØªØ§Ù",
        "bots":"Ø§ÙØ¨ÙØªØ§Øª","keyboard":"Ø§ÙÙÙØ¨ÙØ±Ø¯","games":"Ø§ÙØ£ÙØ¹Ø§Ø¨","repeat":"Ø§ÙØªÙØ±Ø§Ø±",
        "join_lock":"Ø§ÙØ¯Ø®ÙÙ","entry":"Ø±Ø³Ø§Ø¦Ù Ø§ÙØ¯Ø®ÙÙ","add_lock":"Ø§ÙØ¥Ø¶Ø§ÙØ©","notifications":"Ø§ÙØ¥Ø´Ø¹Ø§Ø±Ø§Øª",
        "markdown":"Ø§ÙÙØ§Ø±ÙØ¯Ø§ÙÙ","edit":"Ø§ÙØªØ¹Ø¯ÙÙ"
    }
    text = "ð¡ï¸ Ø­Ø§ÙØ© Ø§ÙØ­ÙØ§ÙØ©:\n\n"
    for f in SETTING_FIELDS:
        text += f"â¢ {labels.get(f,f)}: {'ð ÙÙÙÙØ¹' if s[f] else 'ð ÙØ³ÙÙØ­'}\n"
    await update.effective_message.reply_text(text)

async def settings_cmd(update, context):
    await locks(update, context)

async def alerts(update, context):
    if not await require_admin(update): return
    chat = update.effective_chat
    db.execute(
        "INSERT OR REPLACE INTO watched_groups(chat_id,title,added_at) VALUES(?,?,?)",
        (chat.id, chat.title or "", now())
    )
    db.commit()
    set_setting(chat.id, "notifications", 1)
    await update.effective_message.reply_text("ð¢ ØªÙ ØªÙØ¹ÙÙ ÙØªØ§Ø¨Ø¹Ø© ÙØ³ØªØ¬Ø¯Ø§Øª ÙØ°Ø§ Ø§ÙÙØ±ÙØ¨.")

async def logs_cmd(update, context):
    if not await require_admin(update): return
    rows = db.execute(
        "SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (update.effective_chat.id,)
    ).fetchall()
    if not rows:
        await update.effective_message.reply_text("ð ÙØ§ ÙÙØ¬Ø¯ Ø³Ø¬Ù Ø¨Ø¹Ø¯.")
        return
    text = "ð Ø¢Ø®Ø± Ø§ÙØ¹ÙÙÙØ§Øª:\n\n" + "\n".join(
        f"â¢ {a} â {d}" for a,d,_ in rows
    )
    await update.effective_message.reply_text(text)

async def ArabicTextCommand(update, context):
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not is_manager(update.effective_user.id):
        return
    text = (update.effective_message.text or "").strip().lower()
    if text in ARABIC_ALIASES:
        field, value = ARABIC_ALIASES[text]
        await set_lock(update, context, field, value)

recent_messages = defaultdict(lambda: deque(maxlen=6))

def has_link(text):
    return bool(text and re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", text, re.I))

def has_tag(text):
    return bool(text and re.search(r"(?<!\w)@\w{3,}", text))

async def message_filter(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    uid = msg.from_user.id if msg.from_user else 0
    # Owners/managers are exempt from automatic content deletion.
    if is_manager(uid):
        return
    s = get_settings(chat.id)
    delete = False
    reason = ""

    if s["links"] and has_link(msg.text or msg.caption):
        delete, reason = True, "Ø±Ø§Ø¨Ø·"
    if s["photos"] and msg.photo:
        delete, reason = True, "ØµÙØ±Ø©"
    if s["videos"] and (msg.video or msg.video_note):
        delete, reason = True, "ÙÙØ¯ÙÙ"
    if s["audio"] and (msg.audio or msg.voice):
        delete, reason = True, "ØµÙØª"
    if s["files"] and (msg.document or msg.animation):
        delete, reason = True, "ÙÙÙ"
    if s["stickers"] and msg.sticker:
        delete, reason = True, "ÙÙØµÙ"
    if s["gif"] and msg.animation:
        delete, reason = True, "ÙØªØ­Ø±ÙØ©"
    if s["username"] and has_tag(msg.text or msg.caption):
        delete, reason = True, "ÙØ¹Ø±Ù"
    if s["tag"] and (msg.entities or msg.caption_entities):
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(getattr(e, "type", "") in ("mention", "text_mention") for e in entities):
            delete, reason = True, "ØªØ§Ù"
    if s["bots"] and msg.from_user and msg.from_user.is_bot:
        delete, reason = True, "Ø¨ÙØª"
    if s["keyboard"] and msg.reply_markup:
        delete, reason = True, "ÙÙØ¨ÙØ±Ø¯"
    if s["games"] and msg.game:
        delete, reason = True, "ÙØ¹Ø¨Ø©"

    if s["repeat"] and msg.text:
        key = (chat.id, uid)
        old = list(recent_messages[key])
        if msg.text.strip() in old:
            delete, reason = True, "ØªÙØ±Ø§Ø±"
        recent_messages[key].append(msg.text.strip())

    if delete:
        try:
            await msg.delete()
            log_action(chat.id, uid, "delete", reason)
        except Exception as e:
            log.warning("Delete failed: %s", e)

async def edited_filter(update, context):
    msg = update.edited_message
    if not msg or not update.effective_chat:
        return
    if not is_manager(msg.from_user.id if msg.from_user else 0):
        s = get_settings(update.effective_chat.id)
        if s["edit"]:
            try:
                await msg.delete()
                log_action(update.effective_chat.id, msg.from_user.id if msg.from_user else 0, "delete_edit", "ØªØ¹Ø¯ÙÙ")
            except Exception:
                pass

async def chat_member_handler(update, context):
    cm = update.chat_member
    if not cm or not cm.chat:
        return
    chat_id = cm.chat.id
    s = get_settings(chat_id)
    old = cm.old_chat_member.status
    new = cm.new_chat_member.status
    # Welcome / entry message deletion is handled by service messages.
    if s["entry"] and new == "member" and old in ("left", "kicked"):
        # Telegram may not allow deleting every service message in every chat state.
        try:
            await context.bot.delete_message(chat_id, cm.new_chat_member.user.id)
        except Exception:
            pass

async def new_members(update, context):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    s = get_settings(update.effective_chat.id)
    if s["entry"]:
        try:
            await msg.delete()
        except Exception:
            pass

async def service_add_handler(update, context):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    s = get_settings(update.effective_chat.id)
    if s["add_lock"]:
        # Telegram does not expose a universal "who added whom" action that can
        # always be reversed safely; delete the service message and report it.
        try:
            await msg.delete()
        except Exception:
            pass

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    if not is_manager(q.from_user.id):
        await q.edit_message_text("â ÙÙØ³ ÙØ¯ÙÙ ØµÙØ§Ø­ÙØ©.")
        return
    if q.data == "managers":
        rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
        text = "ð¥ Ø§ÙÙØ¯Ø±Ø§Ø¡:\n" + ("\n".join(f"â¢ {r[0]}" for r in rows) if rows else "ÙØ§ ÙÙØ¬Ø¯ ÙØ¯Ø±Ø§Ø¡.")
        await q.edit_message_text(text)
    elif q.data == "security":
        await q.edit_message_text(
            "ð¡ï¸ Ø§ÙØ­ÙØ§ÙØ©:\n"
            "/locklinks /unlocklinks\n"
            "/lockphoto /unlockphoto\n"
            "/lockvideo /unlockvideo\n"
            "/lockaudio /unlockaudio\n"
            "/lockfile /unlockfile\n"
            "/lockstickers /unlockstickers\n"
            "/lockgif /unlockgif\n"
            "/lockusername /unlockusername\n"
            "/locktag /unlocktag\n"
            "/lockbots /unlockbots\n"
            "/lockkeyboard /unlockkeyboard\n"
            "/lockgames /unlockgames\n"
            "/lockrepeat /unlockrepeat\n"
            "/lockjoin /unlockjoin\n"
            "/lockentry /unlockentry\n"
            "/lockadd /unlockadd\n"
            "/lockedit /unlockedit"
        )
    elif q.data == "alerts":
        await q.edit_message_text("ð¢ Ø§Ø³ØªØ®Ø¯Ù /alerts Ø¯Ø§Ø®Ù Ø§ÙÙØ±ÙØ¨ ÙØªÙØ¹ÙÙ ÙØªØ§Ø¨Ø¹Ø© Ø§ÙÙØ³ØªØ¬Ø¯Ø§Øª.")
    elif q.data == "settings":
        await q.edit_message_text("âï¸ Ø§Ø³ØªØ®Ø¯Ù /settings Ø£Ù /locks ÙØ¹Ø±Ø¶ Ø­Ø§ÙØ© Ø§ÙØ¥Ø¹Ø¯Ø§Ø¯Ø§Øª.")
    elif q.data == "add_manager_help":
        await q.edit_message_text("â Ø§Ø³ØªØ®Ø¯Ù:\n/addmanager Ø±ÙÙ_Ø§ÙÙØ³ØªØ®Ø¯Ù")
    elif q.data == "logs":
        await q.edit_message_text("ð Ø§Ø³ØªØ®Ø¯Ù /logs Ø¯Ø§Ø®Ù Ø§ÙÙØ±ÙØ¨ ÙØ¹Ø±Ø¶ Ø¢Ø®Ø± Ø§ÙØ¹ÙÙÙØ§Øª.")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram bot is running.")
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ØºÙØ± ÙÙØ¬ÙØ¯ ÙÙ Environment Variables.")
    app = Application.builder().token(TOKEN).build()

    command_handlers = {
        "start": start, "help": help_cmd, "panel": panel,
        "addmanager": addmanager, "delmanager": delmanager, "managers": managers,
        "id": id_cmd, "idgroup": idgroup, "ban": ban, "unban": unban,
        "kick": kick, "mute": mute, "unmute": unmute, "del": del_cmd, "pin": pin,
        "alerts": alerts, "logs": logs_cmd, "settings": settings_cmd, "locks": locks
    }
    for name, fn in command_handlers.items():
        app.add_handler(CommandHandler(name, fn))

    for cmd in LOCK_MAP:
        app.add_handler(CommandHandler(cmd, set_lock))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members), group=1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, ArabicTextCommand), group=2)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_filter), group=3)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_filter), group=4)
    app.add_error_handler(lambda update, context: log.error("Update error: %s", context.error))

    threading.Thread(target=start_health_server, daemon=True).start()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
