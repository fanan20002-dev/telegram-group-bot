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
        await update.effective_message.reply_text("\u0647\u0630\u0627 \u0627\u0644\u0623\u0645\u0631 \u064a\u0639\u0645\u0644 \u062f\u0627\u062e\u0644 \u0627\u0644\u0642\u0631\u0648\u0628 \u0641\u0642\u0637.")
        return False
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("\u26d4 \u0644\u064a\u0633 \u0644\u062f\u064a\u0643 \u0635\u0644\u0627\u062d\u064a\u0629.")
        return False
    return True

def panel_markup(owner=False):
    rows = [
        [InlineKeyboardButton("\ud83d\udee1\ufe0f \u0627\u0644\u062d\u0645\u0627\u064a\u0629", callback_data="security"),
         InlineKeyboardButton("\ud83d\udc65 \u0627\u0644\u0645\u062f\u0631\u0627\u0621", callback_data="managers")],
        [InlineKeyboardButton("\ud83d\udce2 \u0627\u0644\u0645\u0633\u062a\u062c\u062f\u0627\u062a", callback_data="alerts"),
         InlineKeyboardButton("\ud83d\udccb \u0627\u0644\u0633\u062c\u0644", callback_data="logs")],
        [InlineKeyboardButton("\u2699\ufe0f \u0625\u0639\u062f\u0627\u062f\u0627\u062a \u0627\u0644\u0642\u0631\u0648\u0628", callback_data="settings")]
    ]
    if owner:
        rows.append([InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0645\u062f\u064a\u0631", callback_data="add_manager_help")])
    return InlineKeyboardMarkup(rows)

async def start(update, context):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"\u0645\u0631\u062d\u0628\u064b\u0627 \ud83d\udc4b\n\u0631\u0642\u0645 \u062d\u0633\u0627\u0628\u0643: {update.effective_user.id}\n\n"
            "\u0627\u0633\u062a\u062e\u062f\u0645 /panel \u0644\u0641\u062a\u062d \u0644\u0648\u062d\u0629 \u0627\u0644\u062a\u062d\u0643\u0645."
        )
    else:
        await update.effective_message.reply_text("\u062a\u0645 \u062a\u0634\u063a\u064a\u0644 \u0627\u0644\u0628\u0648\u062a \u2705\n\u0627\u0633\u062a\u062e\u062f\u0645 /panel \u0644\u0644\u0648\u062d\u0629 \u0627\u0644\u062a\u062d\u0643\u0645.")

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "\ud83d\udcda \u0623\u0648\u0627\u0645\u0631 \u0627\u0644\u0628\u0648\u062a:\n\n"
        "\ud83d\udc51 /panel \u2014 \u0644\u0648\u062d\u0629 \u0627\u0644\u062a\u062d\u0643\u0645\n"
        "\ud83d\udc65 /addmanager /delmanager /managers\n"
        "\ud83d\udd28 /ban /unban /kick /mute /unmute /del /pin\n"
        "\ud83d\udee1\ufe0f /locks \u2014 \u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062d\u0645\u0627\u064a\u0629\n"
        "\u2699\ufe0f /settings \u2014 \u062d\u0627\u0644\u0629 \u0627\u0644\u062d\u0645\u0627\u064a\u0629\n"
        "\ud83c\udd94 /id /idgroup\n\n"
        "\u064a\u0645\u0643\u0646 \u0623\u064a\u0636\u064b\u0627 \u0643\u062a\u0627\u0628\u0629 \u0623\u0648\u0627\u0645\u0631 \u0639\u0631\u0628\u064a\u0629 \u0643\u0631\u0633\u0627\u0626\u0644 \u0639\u0627\u062f\u064a\u0629\u060c \u0645\u062b\u0644:\n"
        "\u0645\u0646\u0639 \u0627\u0644\u0631\u0648\u0627\u0628\u0637\u060c \u0645\u0646\u0639 \u0627\u0644\u0635\u0648\u0631\u060c \u0645\u0646\u0639 \u0627\u0644\u0641\u064a\u062f\u064a\u0648\u060c \u0645\u0646\u0639 \u0627\u0644\u0645\u0644\u0641\u0627\u062a\u060c \u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0631\u0648\u0627\u0628\u0637."
    )

async def panel(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("\u26d4 \u0644\u064a\u0633 \u0644\u062f\u064a\u0643 \u0635\u0644\u0627\u062d\u064a\u0629 \u0627\u0633\u062a\u062e\u062f\u0627\u0645 \u0644\u0648\u062d\u0629 \u0627\u0644\u0625\u062f\u0627\u0631\u0629.")
        return
    await update.effective_message.reply_text(
        "\ud83d\udc51 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0628\u0648\u062a\n\n\u0627\u062e\u062a\u0631 \u0627\u0644\u0642\u0633\u0645 \u0627\u0644\u0645\u0637\u0644\u0648\u0628:",
        reply_markup=panel_markup(is_owner(update.effective_user.id))
    )

async def addmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("\u26d4 \u0647\u0630\u0627 \u0627\u0644\u0623\u0645\u0631 \u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637.")
        return
    if not context.args:
        await update.effective_message.reply_text("\u0627\u0644\u0627\u0633\u062a\u062e\u062f\u0627\u0645: /addmanager \u0631\u0642\u0645_\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("\u0631\u0642\u0645 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 \u063a\u064a\u0631 \u0635\u062d\u064a\u062d.")
        return
    db.execute(
        "INSERT OR REPLACE INTO managers(user_id,added_by,added_at) VALUES(?,?,?)",
        (uid, update.effective_user.id, now())
    )
    db.commit()
    await update.effective_message.reply_text(f"\u2705 \u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0645\u062f\u064a\u0631: {uid}")

async def delmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("\u26d4 \u0647\u0630\u0627 \u0627\u0644\u0623\u0645\u0631 \u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637.")
        return
    if not context.args:
        await update.effective_message.reply_text("\u0627\u0644\u0627\u0633\u062a\u062e\u062f\u0627\u0645: /delmanager \u0631\u0642\u0645_\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("\u0631\u0642\u0645 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 \u063a\u064a\u0631 \u0635\u062d\u064a\u062d.")
        return
    db.execute("DELETE FROM managers WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"\u2705 \u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u0645\u062f\u064a\u0631: {uid}")

async def managers(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("\u26d4 \u0644\u064a\u0633 \u0644\u062f\u064a\u0643 \u0635\u0644\u0627\u062d\u064a\u0629.")
        return
    rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
    text = "\ud83d\udc65 \u0627\u0644\u0645\u062f\u0631\u0627\u0621:\n" + ("\n".join(f"\u2022 {r[0]}" for r in rows) if rows else "\u0644\u0627 \u064a\u0648\u062c\u062f \u0645\u062f\u0631\u0627\u0621 \u0625\u0636\u0627\u0641\u064a\u0648\u0646.")
    if OWNER_ID:
        text += f"\n\n\ud83d\udc51 \u0627\u0644\u0645\u0627\u0644\u0643: {OWNER_ID}"
    await update.effective_message.reply_text(text)

async def id_cmd(update, context):
    await update.effective_message.reply_text(f"\ud83c\udd94 \u0631\u0642\u0645\u0643: {update.effective_user.id}")

async def idgroup(update, context):
    await update.effective_message.reply_text(f"\ud83c\udd94 \u0631\u0642\u0645 \u0627\u0644\u0642\u0631\u0648\u0628: {update.effective_chat.id}")

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
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 \u0627\u0644\u0623\u0645\u0631 \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0639\u0636\u0648 \u0623\u0648 /ban \u0631\u0642\u0645_\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        log_action(update.effective_chat.id, update.effective_user.id, "ban", str(uid))
        await update.message.reply_text("\u2705 \u062a\u0645 \u062d\u0638\u0631 \u0627\u0644\u0639\u0636\u0648.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0644\u062d\u0638\u0631: {e}")

async def unban(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /unban \u0631\u0642\u0645_\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 \u0623\u0648 \u0628\u0627\u0644\u0631\u062f.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("\u2705 \u062a\u0645 \u0641\u0643 \u0627\u0644\u062d\u0638\u0631.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0641\u0643 \u0627\u0644\u062d\u0638\u0631: {e}")

async def kick(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /kick \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0639\u0636\u0648.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("\u2705 \u062a\u0645 \u0637\u0631\u062f \u0627\u0644\u0639\u0636\u0648.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0644\u0637\u0631\u062f: {e}")

async def mute(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /mute \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0639\u0636\u0648.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text("\u2705 \u062a\u0645 \u0643\u062a\u0645 \u0627\u0644\u0639\u0636\u0648.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0644\u0643\u062a\u0645: {e}")

async def unmute(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /unmute \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0639\u0636\u0648.")
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
        await update.message.reply_text("\u2705 \u062a\u0645 \u0641\u0643 \u0627\u0644\u0643\u062a\u0645.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0641\u0643 \u0627\u0644\u0643\u062a\u0645: {e}")

async def del_cmd(update, context):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /del \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0627\u0644\u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0645\u0631\u0627\u062f \u062d\u0630\u0641\u0647\u0627.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0644\u062d\u0630\u0641: {e}")

async def pin(update, context):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u0627\u0633\u062a\u062e\u062f\u0645 /pin \u0628\u0627\u0644\u0631\u062f \u0639\u0644\u0649 \u0627\u0644\u0631\u0633\u0627\u0644\u0629.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("\ud83d\udccc \u062a\u0645 \u0627\u0644\u062a\u062b\u0628\u064a\u062a.")
    except Exception as e:
        await update.message.reply_text(f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0644\u062a\u062b\u0628\u064a\u062a: {e}")

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
    "\u0645\u0646\u0639 \u0627\u0644\u0631\u0648\u0627\u0628\u0637": ("links", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0631\u0648\u0627\u0628\u0637": ("links", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0635\u0648\u0631": ("photos", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0635\u0648\u0631": ("photos", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0641\u064a\u062f\u064a\u0648": ("videos", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0641\u064a\u062f\u064a\u0648": ("videos", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0635\u0648\u062a": ("audio", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0635\u0648\u062a": ("audio", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0645\u0644\u0641\u0627\u062a": ("files", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0645\u0644\u0641\u0627\u062a": ("files", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0645\u0644\u0635\u0642\u0627\u062a": ("stickers", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0645\u0644\u0635\u0642\u0627\u062a": ("stickers", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0645\u062a\u062d\u0631\u0643\u0629": ("gif", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0645\u062a\u062d\u0631\u0643\u0629": ("gif", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0645\u0639\u0631\u0641\u0627\u062a": ("username", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0645\u0639\u0631\u0641\u0627\u062a": ("username", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u062a\u0627\u0642": ("tag", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u062a\u0627\u0642": ("tag", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0628\u0648\u062a\u0627\u062a": ("bots", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0628\u0648\u062a\u0627\u062a": ("bots", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0643\u064a\u0628\u0648\u0631\u062f": ("keyboard", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0643\u064a\u0628\u0648\u0631\u062f": ("keyboard", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0623\u0644\u0639\u0627\u0628": ("games", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0623\u0644\u0639\u0627\u0628": ("games", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u062a\u0643\u0631\u0627\u0631": ("repeat", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u062a\u0643\u0631\u0627\u0631": ("repeat", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u062f\u062e\u0648\u0644": ("join_lock", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u062f\u062e\u0648\u0644": ("join_lock", 0),
    "\u0645\u0646\u0639 \u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u062f\u062e\u0648\u0644": ("entry", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u062f\u062e\u0648\u0644": ("entry", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u0625\u0636\u0627\u0641\u0629": ("add_lock", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u0625\u0636\u0627\u0641\u0629": ("add_lock", 0),
    "\u0645\u0646\u0639 \u0627\u0644\u062a\u0639\u062f\u064a\u0644": ("edit", 1), "\u0627\u0644\u0633\u0645\u0627\u062d \u0628\u0627\u0644\u062a\u0639\u062f\u064a\u0644": ("edit", 0),
}

async def set_lock(update, context, field=None, value=None):
    if not await require_admin(update): return
    if field is None:
        cmd = (update.message.text or "").split()[0].lower().lstrip("/")
        field = LOCK_MAP.get(cmd)
        value = 0 if cmd.startswith("unlock") else 1
    set_setting(update.effective_chat.id, field, value)
    state = "\ud83d\udd12 \u062a\u0645 \u0627\u0644\u0645\u0646\u0639." if value else "\ud83d\udd13 \u062a\u0645 \u0627\u0644\u0633\u0645\u0627\u062d."
    await update.effective_message.reply_text(state)
    log_action(update.effective_chat.id, update.effective_user.id, "setting", f"{field}={value}")

async def locks(update, context):
    if not await require_admin(update): return
    s = get_settings(update.effective_chat.id)
    labels = {
        "links":"\u0627\u0644\u0631\u0648\u0627\u0628\u0637","photos":"\u0627\u0644\u0635\u0648\u0631","videos":"\u0627\u0644\u0641\u064a\u062f\u064a\u0648","audio":"\u0627\u0644\u0635\u0648\u062a","files":"\u0627\u0644\u0645\u0644\u0641\u0627\u062a",
        "stickers":"\u0627\u0644\u0645\u0644\u0635\u0642\u0627\u062a","gif":"\u0627\u0644\u0645\u062a\u062d\u0631\u0643\u0629","username":"\u0627\u0644\u0645\u0639\u0631\u0641\u0627\u062a","tag":"\u0627\u0644\u062a\u0627\u0642",
        "bots":"\u0627\u0644\u0628\u0648\u062a\u0627\u062a","keyboard":"\u0627\u0644\u0643\u064a\u0628\u0648\u0631\u062f","games":"\u0627\u0644\u0623\u0644\u0639\u0627\u0628","repeat":"\u0627\u0644\u062a\u0643\u0631\u0627\u0631",
        "join_lock":"\u0627\u0644\u062f\u062e\u0648\u0644","entry":"\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u062f\u062e\u0648\u0644","add_lock":"\u0627\u0644\u0625\u0636\u0627\u0641\u0629","notifications":"\u0627\u0644\u0625\u0634\u0639\u0627\u0631\u0627\u062a",
        "markdown":"\u0627\u0644\u0645\u0627\u0631\u0643\u062f\u0627\u0648\u0646","edit":"\u0627\u0644\u062a\u0639\u062f\u064a\u0644"
    }
    text = "\ud83d\udee1\ufe0f \u062d\u0627\u0644\u0629 \u0627\u0644\u062d\u0645\u0627\u064a\u0629:\n\n"
    for f in SETTING_FIELDS:
        text += f"\u2022 {labels.get(f,f)}: {'\ud83d\udd12 \u0645\u0645\u0646\u0648\u0639' if s[f] else '\ud83d\udd13 \u0645\u0633\u0645\u0648\u062d'}\n"
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
    await update.effective_message.reply_text("\ud83d\udce2 \u062a\u0645 \u062a\u0641\u0639\u064a\u0644 \u0645\u062a\u0627\u0628\u0639\u0629 \u0645\u0633\u062a\u062c\u062f\u0627\u062a \u0647\u0630\u0627 \u0627\u0644\u0642\u0631\u0648\u0628.")

async def logs_cmd(update, context):
    if not await require_admin(update): return
    rows = db.execute(
        "SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (update.effective_chat.id,)
    ).fetchall()
    if not rows:
        await update.effective_message.reply_text("\ud83d\udccb \u0644\u0627 \u064a\u0648\u062c\u062f \u0633\u062c\u0644 \u0628\u0639\u062f.")
        return
    text = "\ud83d\udccb \u0622\u062e\u0631 \u0627\u0644\u0639\u0645\u0644\u064a\u0627\u062a:\n\n" + "\n".join(
        f"\u2022 {a} \u2014 {d}" for a,d,_ in rows
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
        delete, reason = True, "\u0631\u0627\u0628\u0637"
    if s["photos"] and msg.photo:
        delete, reason = True, "\u0635\u0648\u0631\u0629"
    if s["videos"] and (msg.video or msg.video_note):
        delete, reason = True, "\u0641\u064a\u062f\u064a\u0648"
    if s["audio"] and (msg.audio or msg.voice):
        delete, reason = True, "\u0635\u0648\u062a"
    if s["files"] and (msg.document or msg.animation):
        delete, reason = True, "\u0645\u0644\u0641"
    if s["stickers"] and msg.sticker:
        delete, reason = True, "\u0645\u0644\u0635\u0642"
    if s["gif"] and msg.animation:
        delete, reason = True, "\u0645\u062a\u062d\u0631\u0643\u0629"
    if s["username"] and has_tag(msg.text or msg.caption):
        delete, reason = True, "\u0645\u0639\u0631\u0641"
    if s["tag"] and (msg.entities or msg.caption_entities):
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(getattr(e, "type", "") in ("mention", "text_mention") for e in entities):
            delete, reason = True, "\u062a\u0627\u0642"
    if s["bots"] and msg.from_user and msg.from_user.is_bot:
        delete, reason = True, "\u0628\u0648\u062a"
    if s["keyboard"] and msg.reply_markup:
        delete, reason = True, "\u0643\u064a\u0628\u0648\u0631\u062f"
    if s["games"] and msg.game:
        delete, reason = True, "\u0644\u0639\u0628\u0629"

    if s["repeat"] and msg.text:
        key = (chat.id, uid)
        old = list(recent_messages[key])
        if msg.text.strip() in old:
            delete, reason = True, "\u062a\u0643\u0631\u0627\u0631"
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
                log_action(update.effective_chat.id, msg.from_user.id if msg.from_user else 0, "delete_edit", "\u062a\u0639\u062f\u064a\u0644")
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
        await q.edit_message_text("\u26d4 \u0644\u064a\u0633 \u0644\u062f\u064a\u0643 \u0635\u0644\u0627\u062d\u064a\u0629.")
        return
    if q.data == "managers":
        rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
        text = "\ud83d\udc65 \u0627\u0644\u0645\u062f\u0631\u0627\u0621:\n" + ("\n".join(f"\u2022 {r[0]}" for r in rows) if rows else "\u0644\u0627 \u064a\u0648\u062c\u062f \u0645\u062f\u0631\u0627\u0621.")
        await q.edit_message_text(text)
    elif q.data == "security":
        await q.edit_message_text(
            "\ud83d\udee1\ufe0f \u0627\u0644\u062d\u0645\u0627\u064a\u0629:\n"
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
        await q.edit_message_text("\ud83d\udce2 \u0627\u0633\u062a\u062e\u062f\u0645 /alerts \u062f\u0627\u062e\u0644 \u0627\u0644\u0642\u0631\u0648\u0628 \u0644\u062a\u0641\u0639\u064a\u0644 \u0645\u062a\u0627\u0628\u0639\u0629 \u0627\u0644\u0645\u0633\u062a\u062c\u062f\u0627\u062a.")
    elif q.data == "settings":
        await q.edit_message_text("\u2699\ufe0f \u0627\u0633\u062a\u062e\u062f\u0645 /settings \u0623\u0648 /locks \u0644\u0639\u0631\u0636 \u062d\u0627\u0644\u0629 \u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a.")
    elif q.data == "add_manager_help":
        await q.edit_message_text("\u2795 \u0627\u0633\u062a\u062e\u062f\u0645:\n/addmanager \u0631\u0642\u0645_\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645")
    elif q.data == "logs":
        await q.edit_message_text("\ud83d\udccb \u0627\u0633\u062a\u062e\u062f\u0645 /logs \u062f\u0627\u062e\u0644 \u0627\u0644\u0642\u0631\u0648\u0628 \u0644\u0639\u0631\u0636 \u0622\u062e\u0631 \u0627\u0644\u0639\u0645\u0644\u064a\u0627\u062a.")

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
        raise RuntimeError("BOT_TOKEN \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f \u0641\u064a Environment Variables.")
    app = Application.builder().token(TOKEN).build()

    command_handlers = {
       "start": start, "help": help_cmd,
"panel": panel,
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
