import os
import re
import asyncio
import sqlite3
import threading
import logging
import tempfile
import uuid
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
db.execute("""
CREATE TABLE IF NOT EXISTS delegated_owners(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
""")
# Store a display name when Telegram has made it available.
cols = {r[1] for r in db.execute("PRAGMA table_info(delegated_owners)").fetchall()}
if "display_name" not in cols:
    db.execute("ALTER TABLE delegated_owners ADD COLUMN display_name TEXT DEFAULT ''")
if "username" not in cols:
    db.execute("ALTER TABLE delegated_owners ADD COLUMN username TEXT DEFAULT ''")
db.execute("""
CREATE TABLE IF NOT EXISTS publish_permissions(
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    granted_by INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(user_id, chat_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS group_roles(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    granted_by INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,user_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS protection_managers(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS message_archive(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    chat_title TEXT,
    user_id INTEGER,
    username TEXT,
    display_name TEXT,
    message_id INTEGER,
    message_text TEXT,
    created_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS archive_settings(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS member_activity(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    stars INTEGER NOT NULL DEFAULT 0,
    custom_title TEXT DEFAULT '',
    PRIMARY KEY(chat_id,user_id)
)
""")
# Track usernames too, so changes can be detected the next time a member sends a message.
member_activity_cols = {r[1] for r in db.execute("PRAGMA table_info(member_activity)").fetchall()}
if "username" not in member_activity_cols:
    db.execute("ALTER TABLE member_activity ADD COLUMN username TEXT DEFAULT ''")
db.execute("""
CREATE TABLE IF NOT EXISTS welcome_messages(
    chat_id INTEGER PRIMARY KEY,
    message TEXT NOT NULL DEFAULT ''
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS notification_preferences(
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(user_id,event)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS game_scores(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    points INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id,user_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS game_config(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    challenge_enabled INTEGER NOT NULL DEFAULT 1
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS game_challenges(
    token TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
""")
db.execute("""CREATE TABLE IF NOT EXISTS game_score_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    points INTEGER NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS game_rounds(
    token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
    answer TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS daily_game_challenges(
    chat_id INTEGER NOT NULL, challenge_date TEXT NOT NULL, message_id INTEGER,
    answer TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, winner_id INTEGER,
    PRIMARY KEY(chat_id, challenge_date)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS game_xo(
    token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, player_x INTEGER NOT NULL,
    player_o INTEGER, board TEXT NOT NULL DEFAULT '         ', turn TEXT NOT NULL DEFAULT 'X',
    status TEXT NOT NULL DEFAULT 'waiting', message_id INTEGER, created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS pending_verifications(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,user_id)
)""")
db.commit()

SETTING_FIELDS = [
    "links", "photos", "videos", "audio", "files", "stickers", "gif",
    "username", "tag", "bots", "keyboard", "games", "repeat",
    "join_lock", "entry", "add_lock", "notifications", "markdown", "edit", "archive",
    "public_commands", "public_activity", "public_protection", "flood", "verify_new_members"
]

DEFAULTS = {field: 0 for field in SETTING_FIELDS}
DEFAULTS["notifications"] = 1
DEFAULTS["public_commands"] = 1
DEFAULTS["public_activity"] = 1
DEFAULTS["public_protection"] = 1


# ردود آلية قابلة للتخصيص لكل قروب، محفوظة في SQLite.
db.execute("""CREATE TABLE IF NOT EXISTS auto_reply_settings(
    chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS auto_replies(
    chat_id INTEGER NOT NULL, trigger_text TEXT NOT NULL, reply_text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(chat_id, trigger_text)
)""")
db.commit()

DEFAULT_AUTO_REPLIES = [
    ("شكراً", "العفو، حياك الله 🌹"),
    ("صباح الخير", "صباح النور والسرور ☀️"),
    ("مساء الخير", "مساء النور والورد 🌙"),
    ("جزاك الله خير", "وإياك، بارك الله فيك 🤍"),
    ("قوانين القروب", "📌 يرجى الالتزام بقوانين القروب واحترام الجميع."),
    ("مساعدة", "🤖 حياك الله! اكتب /help لمعرفة الأوامر المتاحة."),
    ("من معاي", "الذكاء الاصطناعي"),
]
_auto_reply_last = {}
_AUTO_REPLY_COOLDOWN_SECONDS = 30

def normalize_auto_reply(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[\u064b-\u065f\u0670ـ]", "", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()

def ensure_auto_replies(chat_id):
    db.execute("INSERT OR IGNORE INTO auto_reply_settings(chat_id,enabled) VALUES(?,1)", (chat_id,))
    for trigger, reply in DEFAULT_AUTO_REPLIES:
        db.execute("INSERT OR IGNORE INTO auto_replies(chat_id,trigger_text,reply_text,enabled) VALUES(?,?,?,1)", (chat_id, trigger, reply))
    db.commit()

async def auto_replies_on(update, context):
    if not await require_admin(update): return
    chat_id = update.effective_chat.id
    ensure_auto_replies(chat_id)
    db.execute("UPDATE auto_reply_settings SET enabled=1 WHERE chat_id=?", (chat_id,)); db.commit()
    log_action(chat_id, update.effective_user.id, "auto_replies", "enabled")
    await update.effective_message.reply_text("🤖 تم تشغيل الردود الآلية في هذا القروب.")

async def auto_replies_off(update, context):
    if not await require_admin(update): return
    chat_id = update.effective_chat.id
    ensure_auto_replies(chat_id)
    db.execute("UPDATE auto_reply_settings SET enabled=0 WHERE chat_id=?", (chat_id,)); db.commit()
    log_action(chat_id, update.effective_user.id, "auto_replies", "disabled")
    await update.effective_message.reply_text("⏸️ تم إيقاف الردود الآلية في هذا القروب.")

async def auto_replies_list(update, context):
    if not await require_admin(update): return
    chat_id = update.effective_chat.id
    ensure_auto_replies(chat_id)
    state = db.execute("SELECT enabled FROM auto_reply_settings WHERE chat_id=?", (chat_id,)).fetchone()
    rows = db.execute("SELECT trigger_text,reply_text,enabled FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
    lines = ["🤖 الردود الآلية: " + ("مفعّلة" if state and state[0] else "متوقفة"), ""]
    lines.extend(f"{'✅' if enabled else '⏸️'} {trigger} ← {reply}" for trigger,reply,enabled in rows)
    lines += ["", "تشغيل: /autoreplies_on | إيقاف: /autoreplies_off", "إضافة: /addreply كلمة | الرد"]
    await update.effective_message.reply_text("\n".join(lines)[:3900])

async def add_auto_reply(update, context):
    if not await require_admin(update): return
    raw = (update.effective_message.text or "").partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text("استخدم: /addreply كلمة أو عبارة | الرد التلقائي"); return
    trigger, reply = (part.strip() for part in raw.split("|", 1))
    if not trigger or not reply or len(trigger) > 100 or len(reply) > 1000:
        await update.effective_message.reply_text("❌ تأكد من كتابة العبارة والرد، وبحد أقصى 100 حرف للعبارة و1000 للرد."); return
    chat_id = update.effective_chat.id
    ensure_auto_replies(chat_id)
    db.execute("INSERT INTO auto_replies(chat_id,trigger_text,reply_text,enabled) VALUES(?,?,?,1) ON CONFLICT(chat_id,trigger_text) DO UPDATE SET reply_text=excluded.reply_text,enabled=1", (chat_id, trigger, reply)); db.commit()
    log_action(chat_id, update.effective_user.id, "auto_reply_added", trigger)
    await update.effective_message.reply_text(f"✅ تم حفظ الرد الآلي.\n🗣️ العبارة: {trigger}\n💬 الرد: {reply}")

async def delete_auto_reply(update, context):
    if not await require_admin(update): return
    trigger = " ".join(context.args).strip()
    if not trigger:
        await update.effective_message.reply_text("استخدم: /delreply العبارة"); return
    chat_id = update.effective_chat.id
    cur = db.execute("DELETE FROM auto_replies WHERE chat_id=? AND trigger_text=?", (chat_id, trigger)); db.commit()
    if cur.rowcount:
        log_action(chat_id, update.effective_user.id, "auto_reply_deleted", trigger)
        await update.effective_message.reply_text("🗑️ تم حذف الرد الآلي.")
    else: await update.effective_message.reply_text("لم أجد عبارة مطابقة. استخدم /autoreplies لعرض الردود.")

def now():
    return datetime.now(timezone.utc).isoformat()

def record_game_points(chat_id, user_id, points, event_type):
    """يسجل نقاط الجولة لأغراض المتصدرين الأسبوعيين والشهريين."""
    if points > 0:
        db.execute("INSERT INTO game_score_events(chat_id,user_id,points,event_type,created_at) VALUES(?,?,?,?,?)",
                   (chat_id, user_id, points, event_type, now()))
        db.commit()

def game_board_markup(token, board):
    cells = ["❌" if c == "X" else "⭕" if c == "O" else "▫️" for c in board]
    rows = []
    for start in (0, 3, 6):
        rows.append([InlineKeyboardButton(cells[i], callback_data=f"xo_move:{token}:{i}") for i in range(start, start+3)])
    return InlineKeyboardMarkup(rows)

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

def user_display_name(user_id):
    row = db.execute("SELECT display_name, username FROM delegated_owners WHERE user_id=?", (user_id,)).fetchone()
    if row:
        name, username = row
        if name:
            return f"{name} (@{username})" if username else name
    return f"المستخدم {user_id}"

def remember_delegated_user(user):
    if not user:
        return
    if db.execute("SELECT 1 FROM delegated_owners WHERE user_id=?", (user.id,)).fetchone():
        db.execute("UPDATE delegated_owners SET display_name=?, username=? WHERE user_id=?",
                   (user.full_name or '', user.username or '', user.id))
        db.commit()

def can_publish_to(uid, chat_id):
    """المالك والمفوّض ينشران في جميع القروبات المسجلة التي يوجد فيها البوت."""
    if is_owner(uid):
        return True
    if not is_delegated_owner(uid):
        return False
    return bool(db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone())

async def can_publish_to_live(bot, uid, chat_id):
    """السماح للمالك والمفوّض بالنشر إلى القروبات المسجلة؛ صلاحية الإرسال الفعلية تعتمد على وجود البوت فيها."""
    if is_owner(uid):
        return True
    return is_delegated_owner(uid) and bool(
        db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    )

async def can_access_group(bot, uid, chat_id):
    """المالك المفوّض يملك صلاحيات الإدارة التشغيلية في كل قروب مسجل للبوت؛ الصلاحيات العليا تبقى للمالك الأساسي."""
    if is_owner(uid):
        return True
    if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
        return False
    if is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
        return True
    if not group_role(chat_id, uid):
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
        return member.status not in ("left", "kicked")
    except Exception as exc:
        log.warning("تعذر التحقق من عضوية المستخدم %s في القروب %s: %s", uid, chat_id, exc)
        return False

async def accessible_groups(bot, uid):
    groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
    if is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
        return groups
    allowed = []
    for chat_id, title in groups:
        if await can_access_group(bot, uid, chat_id):
            allowed.append((chat_id, title))
    return allowed

def is_delegated_owner(uid):
    return is_owner(uid) or bool(db.execute(
        "SELECT 1 FROM delegated_owners WHERE user_id=?", (uid,)
    ).fetchone())

def is_general_manager(uid):
    # المالك المفوض يملك الإدارة التشغيلية في القروبات المسجلة، دون صلاحيات النظام العليا.
    return is_owner(uid) or is_delegated_owner(uid) or bool(db.execute(
        "SELECT 1 FROM managers WHERE user_id=?", (uid,)
    ).fetchone())

def is_protection_manager(uid):
    # المالك المفوض يملك إعدادات الحماية داخل القروبات المسجلة؛ إدارة النظام العليا للمالك الأساسي فقط.
    return is_owner(uid) or is_delegated_owner(uid) or bool(db.execute(
        "SELECT 1 FROM protection_managers WHERE user_id=?", (uid,)
    ).fetchone())

def is_manager(uid):
    return is_general_manager(uid)

def can_use_panel(uid):
    # المالك المفوض يحتاج لوحة الإدارة التشغيلية حتى لو لم تُمنح له صلاحية نشر منفصلة.
    # صلاحية كل مجموعة تُفحص عند فتح القسم أو تنفيذ الإجراء، أما إدارة المالكين والأرشيف فتبقى للمالك الأساسي.
    return is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)

def can_manage_security(uid):
    return is_general_manager(uid) or is_protection_manager(uid)

def role_name(uid):
    if is_owner(uid):
        return "👑 المالك الأساسي"
    if is_delegated_owner(uid):
        return "👑 مالك مفوّض"
    if is_general_manager(uid):
        return "🔨 مدير عام"
    if is_protection_manager(uid):
        return "🛡️ مدير حماية"
    return "👤 عضو"

GROUP_ROLE_PERMISSIONS = {
    "مشرف": {"delete", "mute"},
    "مشرف عام": {"delete", "mute", "kick", "pin", "protection"},
    "مدير": {"delete", "mute", "kick", "pin", "protection"},
    "مدير عام": {"delete", "mute", "kick", "pin", "protection", "manage_roles", "logs", "welcome", "stats"},
}


def group_role(chat_id, uid):
    row = db.execute("SELECT role FROM group_roles WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
    return row[0] if row else ""


def has_group_permission(chat_id, uid, permission):
    if is_owner(uid):
        return True
    if is_delegated_owner(uid):
        return True  # مفوّض البوت يملك إدارة تشغيلية للقروبات التي هو عضو فيها
    if is_general_manager(uid) or is_protection_manager(uid):
        return True
    return permission in GROUP_ROLE_PERMISSIONS.get(group_role(chat_id, uid), set())


async def require_group_permission(update, permission, message=None):
    if not await require_admin(update):
        return False
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if has_group_permission(chat_id, uid, permission):
        return True
    await update.effective_message.reply_text(message or "⛔ لا تملك الصلاحية المطلوبة في هذا القروب.")
    return False


async def require_admin(update):
    if not update.effective_user or not update.effective_chat:
        return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل القروب فقط.")
        return False
    uid = update.effective_user.id
    if not (is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)
            or group_role(update.effective_chat.id, uid)):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية الإدارة في هذا القروب.")
        return False
    # أوامر الإشراف للمفوّض تعمل فقط داخل القروب الذي يرسل الأمر فيه.
    if is_delegated_owner(uid) and not is_owner(uid):
        if not await can_access_group(update.get_bot(), uid, update.effective_chat.id):
            await update.effective_message.reply_text("⛔ لا تملك صلاحية التحكم في هذا القروب.")
            return False
    return True

def register_group(chat):
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    db.execute(
        "INSERT OR REPLACE INTO watched_groups(chat_id,title,added_at) "
        "VALUES(?,?,COALESCE((SELECT added_at FROM watched_groups WHERE chat_id=?),?))",
        (chat.id, chat.title or "", chat.id, now())
    )
    db.commit()

def panel_markup(uid):
    # واجهة موحدة بعناوين وأيقونات مرتبة؛ الصلاحيات الحساسة للمالك الأساسي فقط.
    if is_delegated_owner(uid) and not is_owner(uid):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡️ الحماية", callback_data="security_groups"), InlineKeyboardButton("🔨 الإشراف", callback_data="administration_groups")],
            [InlineKeyboardButton("📣 النشر", callback_data="broadcast_start"), InlineKeyboardButton("👋 الترحيب", callback_data="welcome")],
            [InlineKeyboardButton("📊 الإحصائيات", callback_data="statistics"), InlineKeyboardButton("📋 السجلات", callback_data="logs")],
            [InlineKeyboardButton("🎮 الألعاب والتحديات", callback_data="games"), InlineKeyboardButton("📢 التنبيهات", callback_data="alerts")],
            [InlineKeyboardButton("👥 الرتب الإدارية", callback_data="roles_groups"), InlineKeyboardButton("💚 صحة البوت", callback_data="health")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡️ الحماية", callback_data="security"), InlineKeyboardButton("🔨 إدارة الأعضاء", callback_data="administration")],
        [InlineKeyboardButton("📣 النشر", callback_data="broadcast_start"), InlineKeyboardButton("👋 الترحيب", callback_data="welcome")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="statistics"), InlineKeyboardButton("📋 سجل العمليات", callback_data="logs")],
        [InlineKeyboardButton("🎮 الألعاب والتحديات", callback_data="games"), InlineKeyboardButton("📢 التنبيهات", callback_data="alerts")],
        [InlineKeyboardButton("⚙️ إعدادات القروبات", callback_data="settings"), InlineKeyboardButton("👥 الرتب الإدارية", callback_data="roles_groups")],
        [InlineKeyboardButton("💚 صحة البوت", callback_data="health"), InlineKeyboardButton("👑 إدارة المالكين المفوضين", callback_data="delegated_owner")],
        [InlineKeyboardButton("📨 سجل الرسائل الخاص (للمالك)", callback_data="archive")],
        [InlineKeyboardButton("💾 النسخ الاحتياطي (للمالك)", callback_data="owner_backup"), InlineKeyboardButton("⚙️ النظام الأعلى (للمالك)", callback_data="owner_system")],
    ])

def settings_page_markup(chat_id, page=0):
    items = [
        ("منع الروابط", "links"), ("الكيبورد", "keyboard"),
        ("الصوت", "audio"), ("صور GIF المتحركة", "gif"), ("الملفات", "files"),
        ("منع تكرار الرسائل", "repeat"), ("الفيديو", "videos"), ("الصور", "photos"),
        ("أسماء المستخدمين", "username"), ("الإشارات للأعضاء", "tag"), ("البوتات", "bots"),
        ("الألعاب", "games"), ("الملصقات", "stickers"), ("حذف الرسائل المعدلة", "edit"),
        ("رسائل الدخول", "entry"), ("منع إضافة الأعضاء", "add_lock"),
        ("إشعارات البوت", "notifications"),
        ("إظهار دليل الأوامر للأعضاء", "public_commands"),
        ("إظهار نشاط الأعضاء", "public_activity"), ("إظهار معلومات الحماية للأعضاء", "public_protection"),
        ("🧯 الحماية من إغراق الرسائل", "flood"), ("👤 التحقق من الأعضاء الجدد", "verify_new_members")
    ]
    per_page = 12
    pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    s = get_settings(chat_id)
    rows = []
    for label, field in items[page*per_page:(page+1)*per_page]:
        state = "نعم" if s.get(field, 0) else "لا"
        rows.append([
            InlineKeyboardButton(f"← {label}", callback_data=f"noop:{field}"),
            InlineKeyboardButton(state, callback_data=f"toggle:{chat_id}:{field}:{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("السابق", callback_data=f"settings_page:{chat_id}:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("التالي", callback_data=f"settings_page:{chat_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def settings_text(page):
    return (
        f"⚙️ إعدادات المجموعة — الصفحة {page+1}\n\n"
        "• «نعم» = مقفول / ممنوع\n"
        "• «لا» = مفتوح / مسموح\n\n"
        "اضغط «نعم» أو «لا» لتغيير الإعداد."
    )

def group_list_markup():
    rows = []
    groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
    for chat_id, title in groups[:30]:
        rows.append([InlineKeyboardButton(f"📊 {title or chat_id}", callback_data=f"group:{chat_id}")])
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد قروبات مسجلة بعد", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def group_list_markup_for(prefix):
    groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
    rows = [[InlineKeyboardButton(f"👥 {title or chat_id}", callback_data=f"{prefix}:{chat_id}")] for chat_id, title in groups[:30]]
    if not rows:
        rows = [[InlineKeyboardButton("لا توجد قروبات مسجلة بعد", callback_data="noop")]]
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)

async def report_cmd(update, context):
    """بلاغ خاص للمشرفين عن رسالة بالقروب دون نشر البلاغ في العلن."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("استخدم /report بالرد على الرسالة داخل القروب للإبلاغ عنها بسرية.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("ℹ️ للإبلاغ بسرية، ردّ على الرسالة المخالفة واكتب /report.")
        return
    target = msg.reply_to_message
    body = (target.text or target.caption or "[رسالة غير نصية]")[:800]
    reporter_name = user.full_name or "عضو"
    target_user = target.from_user
    target_name = target_user.full_name if target_user else "مرسل غير معروف"
    report_text = (
        "🚩 بلاغ عضو جديد\n"
        f"القروب: {chat.title or chat.id} (ID: {chat.id})\n"
        f"المبلّغ: {reporter_name} (ID: {user.id})\n"
        f"عن العضو: {target_name} (ID: {target_user.id if target_user else 'غير متاح'})\n"
        f"رقم الرسالة: {target.message_id}\n"
        f"المحتوى: {body}"
    )
    await notify_owners(context, report_text, "reports")
    try:
        await msg.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id=chat.id, text="✅ وصل بلاغك للمشرفين بسرية. شكرًا لمساعدتك في حماية القروب.")
    log_action(chat.id, user.id, "member_report", f"reported message {target.message_id}; target={target_user.id if target_user else 0}")


async def start(update, context):
    remember_delegated_user(update.effective_user)
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"مرحبًا 👋\nرقم حسابك: {update.effective_user.id}\n\n"
            "استخدم /panel لفتح لوحة التحكم."
        )
    else:
        await update.effective_message.reply_text("تم تشغيل البوت ✅\nاستخدم /panel للوحة التحكم.")

async def help_cmd(update, context):
    uid = update.effective_user.id if update.effective_user else 0
    text = (
        "📚 دليل البوت العربي\n\n"
        "🏠 /panel — لوحة التحكم من الخاص (للمصرح لهم)\n"
        "🛡️ من الخاص: اختر القروب ثم غيّر إعدادات الحماية.\n"
        "🔨 من الخاص: اختر القروب ثم نفّذ الحظر والكتم والطرد والحذف والتثبيت برقم المستخدم أو الرسالة.\n"
        "🎮 الألعاب والتحديات متاحة من قائمة الأعضاء داخل القروب.\n"
        "🆔 /id — عرض رقم حسابك\n"
        "🆔 /idgroup — عرض رقم المجموعة\n"
        "🚩 /report — بلاغ سري (بالرد على الرسالة)\n"
        "📊 الإحصائيات والسجلات من لوحة التحكم\n\n"
        "أوامر الأعضاء: /id و /idgroup و /settings.\n"
        "أوامر الإدارة العربية (للمصرح لهم): منع الروابط، السماح بالروابط، منع الصور، السماح بالصور، منع الفيديو، السماح بالفيديو، منع الملفات، السماح بالملفات، منع الملصقات، السماح بالملصقات، منع التكرار، السماح بالتكرار.\n"
        "تُعرض الأوامر العامة بحسب إعدادات المجموعة."
    )
    if is_owner(uid):
        text += (
            "\n\n👑 أوامر المالك الأساسي فقط:\n"
            "/addowner — إضافة مالك مفوض\n/delowner — إزالة مالك مفوض\n"
        "رفع مشرف / رفع مشرف عام / رفع مدير / رفع مدير عام — بالرد على العضو\n"
        "تنزيل رتبة — بالرد على العضو\n"
            "/addmanager — إضافة مدير عام\n/delmanager — إزالة مدير عام\n"
            "/addprotect — إضافة مشرف حماية\n/delprotect — إزالة مشرف حماية\n"
            "/archive_on — تفعيل أرشفة الرسائل بعد إعلانها\n"
            "/archive_off — إيقاف أرشفة الرسائل\n/archive — عرض سجل الرسائل الخاص\n"
            "/grantpublish USER_ID CHAT_ID — منح صلاحية النشر في قروب\n"
            "/revokepublish USER_ID CHAT_ID — سحب صلاحية النشر من قروب\n"
            "استخدم CHAT_ID=all لمنح/سحب صلاحية النشر في كل القروبات المسجلة."
        )
    await update.effective_message.reply_text(text)

async def panel(update, context):
    if not can_use_panel(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية لوحة الإدارة.")
        return
    if update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        register_group(update.effective_chat)
    await update.effective_message.reply_text(
        "👑 لوحة التحكم الرئيسية\n\n"
        f"الصلاحية: {role_name(update.effective_user.id)}\n\n"
        "اختر القسم المطلوب. يمكنك إدارة القروبات من الخاص عبر اختيار القروب من القائمة، ثم اختيار الإجراء:",
        reply_markup=panel_markup(update.effective_user.id)
    )

async def addowner(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك الأساسي فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /addowner رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    if uid == OWNER_ID:
        await update.effective_message.reply_text("هذا الحساب هو المالك الأساسي بالفعل.")
        return
    name = target.full_name if hasattr(target, "full_name") else ""
    username = target.username if hasattr(target, "username") and target.username else ""
    db.execute("""INSERT INTO delegated_owners(user_id,added_by,added_at,display_name,username)
                  VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET added_by=excluded.added_by,
                  added_at=excluded.added_at, display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE delegated_owners.display_name END,
                  username=CASE WHEN excluded.username!='' THEN excluded.username ELSE delegated_owners.username END""",
               (uid, update.effective_user.id, now(), name, username))
    db.commit()
    await update.effective_message.reply_text(
        f"👑 تم منح {name or 'المستخدم'} صلاحية «مالك مفوّض» (المعرّف: {uid}).\n"
        "✅ لديه صلاحيات الإدارة التشغيلية والحماية والنشر في جميع القروبات المسجلة التي يوجد فيها البوت.\n"
        "🔐 إدارة المالكين والأرشيف الخاص والنسخ الاحتياطي وإعدادات النظام العليا تبقى للمالك الأساسي."
    )

async def delowner(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك الأساسي فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /delowner رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    name = user_display_name(uid)
    db.execute("DELETE FROM delegated_owners WHERE user_id=?", (uid,))
    db.execute("DELETE FROM publish_permissions WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم عزل {name} وإلغاء جميع صلاحياته وتفويضات النشر.")

async def grantpublish(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ منح صلاحية النشر للمالك الأساسي فقط.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("الاستخدام: /grantpublish USER_ID CHAT_ID أو /grantpublish USER_ID all")
        return
    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح.")
        return
    if not is_delegated_owner(target_uid) or is_owner(target_uid):
        await update.effective_message.reply_text("أضف المستخدم أولًا كمالك مفوّض باستخدام /addowner.")
        return
    raw_chat = context.args[1].lower()
    if raw_chat == "all":
        db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,0,?,?)",
                   (target_uid, update.effective_user.id, now()))
        db.commit()
        await update.effective_message.reply_text(f"✅ مُنحت {user_display_name(target_uid)} صلاحية النشر في جميع القروبات المسجلة حاليًا ومستقبلًا.")
        return
    try:
        chat_id = int(raw_chat)
    except ValueError:
        await update.effective_message.reply_text("معرّف القروب غير صحيح. استخدم all أو رقم القروب.")
        return
    group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    if not group:
        await update.effective_message.reply_text("هذا القروب غير مسجل. أضف البوت إليه ونفّذ /panel داخله أولًا.")
        return
    all_granted = db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0", (target_uid,)).fetchone()
    if not all_granted:
        db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,?,?,?)",
                   (target_uid, chat_id, update.effective_user.id, now()))
    db.commit()
    await update.effective_message.reply_text(f"✅ مُنحت {user_display_name(target_uid)} صلاحية النشر في القروب: {group[0] or chat_id}.")

async def revokepublish(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ سحب صلاحية النشر للمالك الأساسي فقط.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("الاستخدام: /revokepublish USER_ID CHAT_ID أو /revokepublish USER_ID all")
        return
    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح.")
        return
    raw_chat = context.args[1].lower()
    if raw_chat == "all":
        db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
    else:
        try:
            chat_id = int(raw_chat)
        except ValueError:
            await update.effective_message.reply_text("معرّف القروب غير صحيح. استخدم all أو رقم القروب.")
            return
        all_granted = db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0", (target_uid,)).fetchone()
        if all_granted:
            db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
            for other_chat_id, in db.execute("SELECT chat_id FROM watched_groups WHERE chat_id!=?", (chat_id,)).fetchall():
                db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,?,?,?)", (target_uid, other_chat_id, update.effective_user.id, now()))
        else:
            db.execute("DELETE FROM publish_permissions WHERE user_id=? AND chat_id=?", (target_uid, chat_id))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم سحب صلاحية النشر المطلوبة من {user_display_name(target_uid)}.")

async def owners_cmd(update, context):
    if not can_use_panel(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    rows = db.execute("SELECT user_id FROM delegated_owners ORDER BY user_id").fetchall()
    named_rows = db.execute("SELECT user_id,display_name,username FROM delegated_owners ORDER BY user_id").fetchall()
    text = "👑 المالكون المفوّضون:\n\n"
    text += "\n".join(f"• {name or 'اسم غير معروف'}" + (f" (@{username})" if username else "") + f" — ID: {user_id}" for user_id,name,username in named_rows) if named_rows else "لا يوجد مالكون مفوّضون."
    text += f"\n\n👑 المالك الأساسي: {OWNER_ID}"
    await update.effective_message.reply_text(text)

async def addprotect(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ إضافة مدير الحماية للمالك الأساسي فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /addprotect رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    db.execute("INSERT OR REPLACE INTO protection_managers(user_id,added_by,added_at) VALUES(?,?,?)",
               (uid, update.effective_user.id, now()))
    db.commit()
    await update.effective_message.reply_text(f"🛡️ تمت إضافة مدير الحماية: {uid}")

async def delprotect(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ حذف مدير الحماية للمالك الأساسي فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /delprotect رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    db.execute("DELETE FROM protection_managers WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم حذف مدير الحماية: {uid}")

async def permissions_cmd(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    d = db.execute("SELECT COUNT(*) FROM delegated_owners").fetchone()[0]
    m = db.execute("SELECT COUNT(*) FROM managers").fetchone()[0]
    p = db.execute("SELECT COUNT(*) FROM protection_managers").fetchone()[0]
    await update.effective_message.reply_text(
        "👑 الصلاحيات\n\n"
        f"👑 المالك الأساسي: {OWNER_ID}\n"
        f"👑 مالكون مفوّضون: {d}\n"
        f"🔨 مديرون عامون: {m}\n"
        f"🛡️ مديرو حماية: {p}\n\n"
        "إضافة مالك مفوّض: /addowner\n"
        "إضافة مدير عام: /addmanager\n"
        "إضافة مدير حماية: /addprotect"
    )

async def addmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /addmanager رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    db.execute(
        "INSERT OR REPLACE INTO managers(user_id,added_by,added_at) VALUES(?,?,?)",
        (uid, update.effective_user.id, now())
    )
    db.commit()
    await update.effective_message.reply_text(f"✅ تمت إضافة المدير: {uid}")

async def delmanager(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    target = target_from_update(update, context)
    if not target:
        await update.effective_message.reply_text("استخدمه بالرد على العضو أو /delmanager رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    db.execute("DELETE FROM managers WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم حذف المدير: {uid}")

async def managers(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
    text = "👥 المدراء:\n" + ("\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مدراء إضافيون.")
    if OWNER_ID:
        text += f"\n\n👑 المالك: {OWNER_ID}"
    await update.effective_message.reply_text(text)

async def notify_owners(context, text, event="general"):
    recipients = {OWNER_ID} if OWNER_ID else set()
    recipients.update(r[0] for r in db.execute("SELECT user_id FROM delegated_owners").fetchall())
    for recipient in recipients:
        pref = db.execute("SELECT enabled FROM notification_preferences WHERE user_id=? AND event=?", (recipient, event)).fetchone()
        if pref and not pref[0]:
            continue
        try:
            await context.bot.send_message(chat_id=recipient, text=text)
        except Exception as exc:
            log.info("تعذر إرسال إشعار إلى %s: %s", recipient, exc)

def archive_is_enabled(chat_id):
    row = db.execute("SELECT enabled FROM archive_settings WHERE chat_id=?", (chat_id,)).fetchone()
    return bool(row and row[0])

async def archive_on(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك الأساسي فقط.")
        return
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("استخدم الأمر داخل المجموعة التي تريد تفعيل الأرشفة فيها.")
        return
    register_group(chat)
    db.execute("INSERT OR REPLACE INTO archive_settings(chat_id,enabled) VALUES(?,1)", (chat.id,))
    db.commit()
    await update.effective_message.reply_text(
        "⚠️ تم تفعيل تسجيل الرسائل الجديدة لهذه المجموعة.\n"
        "يرجى إعلان ذلك لأعضاء المجموعة وفق قواعدها. أرسل /archive_off لإيقاف التسجيل."
    )

async def archive_off(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك الأساسي فقط.")
        return
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("استخدم الأمر داخل المجموعة.")
        return
    db.execute("INSERT OR REPLACE INTO archive_settings(chat_id,enabled) VALUES(?,0)", (chat.id,))
    db.commit()
    await update.effective_message.reply_text("✅ تم إيقاف تسجيل الرسائل الجديدة لهذه المجموعة.")

async def archive_cmd(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ سجل الرسائل خاص بالمالك الأساسي فقط.")
        return
    if context.args:
        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("استخدم: /archive أو /archive رقم_المجموعة")
            return
    else:
        await update.effective_message.reply_text("استخدم /archive رقم_المجموعة لعرض أحدث الرسائل المسجلة. تحصل على رقم المجموعة عبر /idgroup داخلها.")
        return
    rows = db.execute(
        "SELECT chat_title,display_name,username,message_text,created_at FROM message_archive WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (chat_id,)
    ).fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد رسائل مسجلة لهذه المجموعة.")
        return
    parts = ["📨 أحدث الرسائل المسجلة (للمالك الأساسي فقط):"]
    for title, name, username, body, created in rows:
        parts.append(f"\n📍 {title or chat_id}\n👤 {name or 'عضو'} {('@'+username) if username else ''}\n🕒 {created}\n💬 {(body or '[رسالة غير نصية]')[:500]}")
    output = "\n".join(parts)
    await update.effective_message.reply_text(output[:4000])

async def group_role_command(update, context):
    """ترقية/تنزيل رتبة داخل البوت في القروب الحالي، دون تغيير رتبة تيليجرام الأصلية."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not (is_owner(user.id) or is_delegated_owner(user.id)):
        await msg.reply_text("⛔ رفع الرتب داخل البوت للمالك الأساسي أو المالك المفوّض فقط.")
        return
    if is_delegated_owner(user.id) and not is_owner(user.id):
        if not await can_access_group(context.bot, user.id, chat.id):
            await msg.reply_text("⛔ يجب أن تكون عضوًا في القروب لإدارة رتب البوت فيه.")
            return
    text = (msg.text or "").strip()
    match = re.fullmatch(r"(?:رفع|ترقية)\s+(مشرف عام|مدير عام|مشرف|مدير)", text)
    demote = text in {"تنزيل رتبة", "إزالة الإشراف", "تنزيل مشرف"}
    if not match and not demote:
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text("استخدم الأمر بالرد على رسالة العضو، مثال: رفع مشرف عام")
        return
    if target.is_bot or is_owner(target.id) or is_delegated_owner(target.id):
        await msg.reply_text("⛔ لا يمكن تغيير رتبة مالك البوت أو المالك المفوّض من هنا.")
        return
    current = group_role(chat.id, target.id)
    if demote:
        if not current:
            await msg.reply_text("ℹ️ العضو لا يملك رتبة إدارية داخل البوت في هذا القروب.")
            return
        db.execute("DELETE FROM group_roles WHERE chat_id=? AND user_id=?", (chat.id, target.id))
        db.commit()
        log_action(chat.id, user.id, "group_role_removed", f"target={target.id};old_role={current}")
        await msg.reply_text(f"✅ تم تنزيل رتبة {target.full_name} وإلغاء صلاحياته الإدارية داخل البوت في هذا القروب.")
        return
    role = match.group(1)
    # المفوّض يمكنه توزيع رتب القروب، لكنه لا يستطيع منح صلاحية مالك البوت أو تغيير تفويضات المالكين.
    db.execute("INSERT OR REPLACE INTO group_roles(chat_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",
               (chat.id, target.id, role, user.id, now()))
    db.commit()
    log_action(chat.id, user.id, "group_role_granted", f"target={target.id};role={role}")
    labels = {"delete": "حذف الرسائل", "mute": "الكتم وفك الكتم", "kick": "الطرد والحظر", "pin": "تثبيت الرسائل", "protection": "قفل وفتح الروابط وبقية إعدادات الحماية", "manage_roles": "إدارة رتب القروب", "logs": "عرض السجلات", "welcome": "إدارة الترحيب", "stats": "عرض الإحصائيات"}
    permissions = "، ".join(labels.get(item, item) for item in sorted(GROUP_ROLE_PERMISSIONS[role]))
    await msg.reply_text(
        f"✅ تم رفع {target.full_name} إلى رتبة {role} داخل البوت.\n"
        f"🛡️ الصلاحيات المفعّلة تلقائيًا: {permissions}.\n"
        "ℹ️ هذه رتبة داخل البوت فقط، ولا تغيّر رتبة العضو أو صلاحياته الأصلية في تيليجرام."
    )


async def id_cmd(update, context):
    await update.effective_message.reply_text(f"🆔 رقمك: {update.effective_user.id}")

async def idgroup(update, context):
    await update.effective_message.reply_text(f"🆔 رقم القروب: {update.effective_chat.id}")

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
    if not await require_group_permission(update, "kick"): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو أو /ban رقم_المستخدم")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        log_action(update.effective_chat.id, update.effective_user.id, "ban", str(uid))
        await update.message.reply_text("✅ تم حظر العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الحظر: {e}")

async def unban(update, context):
    if not await require_admin(update): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم /unban رقم_المستخدم أو بالرد.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("✅ تم فك الحظر.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر فك الحظر: {e}")

async def kick(update, context):
    if not await require_group_permission(update, "kick"): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم /kick بالرد على رسالة العضو.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text("✅ تم طرد العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الطرد: {e}")

async def mute(update, context):
    if not await require_group_permission(update, "mute"): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم /mute بالرد على رسالة العضو.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text("✅ تم كتم العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الكتم: {e}")

async def unmute(update, context):
    if not await require_group_permission(update, "mute"): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم /unmute بالرد على رسالة العضو.")
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
        await update.message.reply_text("✅ تم فك الكتم.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر فك الكتم: {e}")

async def del_cmd(update, context):
    if not await require_group_permission(update, "delete"): return
    if not update.message.reply_to_message:
        await update.message.reply_text("استخدم /del بالرد على الرسالة المراد حذفها.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الحذف: {e}")

async def pin(update, context):
    if not await require_group_permission(update, "pin"): return
    if not update.message.reply_to_message:
        await update.message.reply_text("استخدم /pin بالرد على الرسالة.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("📌 تم التثبيت.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر التثبيت: {e}")

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
    "منع الروابط": ("links", 1), "السماح بالروابط": ("links", 0),
    "منع الصور": ("photos", 1), "السماح بالصور": ("photos", 0),
    "منع الفيديو": ("videos", 1), "السماح بالفيديو": ("videos", 0),
    "منع الصوت": ("audio", 1), "السماح بالصوت": ("audio", 0),
    "منع الملفات": ("files", 1), "السماح بالملفات": ("files", 0),
    "منع الملصقات": ("stickers", 1), "السماح بالملصقات": ("stickers", 0),
    "منع المتحركة": ("gif", 1), "السماح بالمتحركة": ("gif", 0),
    "منع المعرفات": ("username", 1), "السماح بالمعرفات": ("username", 0),
    "منع التاق": ("tag", 1), "السماح بالتاق": ("tag", 0),
    "منع البوتات": ("bots", 1), "السماح بالبوتات": ("bots", 0),
    "منع الكيبورد": ("keyboard", 1), "السماح بالكيبورد": ("keyboard", 0),
    "منع الألعاب": ("games", 1), "السماح بالألعاب": ("games", 0),
    "منع التكرار": ("repeat", 1), "السماح بالتكرار": ("repeat", 0),
    "منع الدخول": ("join_lock", 1), "السماح بالدخول": ("join_lock", 0),
    "منع رسائل الدخول": ("entry", 1), "السماح برسائل الدخول": ("entry", 0),
    "منع الإضافة": ("add_lock", 1), "السماح بالإضافة": ("add_lock", 0),
    "منع التعديل": ("edit", 1), "السماح بالتعديل": ("edit", 0),
}

async def set_lock(update, context, field=None, value=None):
    if not await require_group_permission(update, "protection"): return
    if field is None:
        cmd = (update.message.text or "").split()[0].lower().lstrip("/")
        field = LOCK_MAP.get(cmd)
        value = 0 if cmd.startswith("unlock") else 1
    set_setting(update.effective_chat.id, field, value)
    state = "🔒 تم المنع." if value else "🔓 تم السماح."
    await update.effective_message.reply_text(state)
    log_action(update.effective_chat.id, update.effective_user.id, "setting", f"{field}={value}")

async def locks(update, context):
    if not (can_manage_security(update.effective_user.id) or is_delegated_owner(update.effective_user.id)):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية الحماية.")
        return
    register_group(update.effective_chat)
    await update.effective_message.reply_text(
        settings_text(0),
        reply_markup=settings_page_markup(update.effective_chat.id, 0)
    )

async def public_settings(update, context):
    """Public, Arabic help menu for every group member; never exposes admin controls."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(
            "📚 دليل البوت العربي\n\n"
            "⚙️ اكتب «اعدادات» داخل القروب لعرض هذا الدليل.\n"
            "🛡️ لوحة الإدارة والصلاحيات الخاصة تظهر للمصرح لهم فقط."
        )
        return
    register_group(chat)
    settings = get_settings(chat.id)
    buttons = []
    if settings.get("public_commands", 1):
        buttons.append(InlineKeyboardButton("📚 دليل البوت", callback_data="public:commands"))
    if settings.get("public_activity", 1):
        buttons.append(InlineKeyboardButton("🏆 لوحة نشاط الأعضاء", callback_data="public:activity"))
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2) if buttons[i:i+2]]
    # واجهة الأعضاء تعرض خدمات عامة فقط؛ لا تعرض أي خيارات تشغيلية أو تحديثات النظام.
    rows.append([InlineKeyboardButton("🎮 مركز الألعاب والتحديات", callback_data=f"member_games:{chat.id}")])
    rows.append([InlineKeyboardButton("🎯 تحدي اليوم", callback_data=f"game_daily:{chat.id}")])
    if settings.get("public_protection", 1):
        rows.append([InlineKeyboardButton("🛡️ معلومات الحماية", callback_data="public:protection")])
    text = "🤖 مركز الأعضاء\n\n📚 دليل البوت • 🏆 لوحة المتصدرين • 🎮 الألعاب • 🎯 تحدي اليوم\n\n🔒 لوحة الإدارة وإعدادات النظام مخصصة للمصرّح لهم فقط."
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(rows) if rows else None)

async def settings_cmd(update, context):
    # /settings is a public help entry point; admin controls stay behind /panel.
    await public_settings(update, context)

async def alerts(update, context):
    if not await require_admin(update): return
    chat = update.effective_chat
    db.execute(
        "INSERT OR REPLACE INTO watched_groups(chat_id,title,added_at) VALUES(?,?,?)",
        (chat.id, chat.title or "", now())
    )
    db.commit()
    set_setting(chat.id, "notifications", 1)
    await update.effective_message.reply_text("📢 تم تفعيل متابعة مستجدات هذا القروب.")

async def logs_cmd(update, context):
    if not await require_admin(update): return
    rows = db.execute(
        "SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (update.effective_chat.id,)
    ).fetchall()
    if not rows:
        await update.effective_message.reply_text("📋 لا يوجد سجل بعد.")
        return
    text = "📋 آخر العمليات:\n\n" + "\n".join(
        f"• {a} — {d}" for a,d,_ in rows
    )
    await update.effective_message.reply_text(text)

async def ArabicTextCommand(update, context):
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = (update.effective_message.text or "").strip().lower()
    if text in {"اعدادات", "إعدادات", "اعدادات البوت", "إعدادات البوت", "مساعدة", "اوامر البوت", "أوامر البوت"}:
        await public_settings(update, context)
        return
    if re.fullmatch(r"(?:رفع|ترقية)\s+(?:مشرف عام|مدير عام|مشرف|مدير)", text) or text in {"تنزيل رتبة", "إزالة الإشراف", "تنزيل مشرف"}:
        await group_role_command(update, context)
        return
    # Mass delete: "مسح 1" through "مسح 500". Only authorized roles may execute.
    match = re.fullmatch(r"مسح\s+(\d{1,3})", text)
    if match:
        if not has_group_permission(update.effective_chat.id, update.effective_user.id, "delete"):
            await update.effective_message.reply_text("⛔ ليس لديك صلاحية مسح الرسائل في هذا القروب.")
            return
        count = int(match.group(1))
        if not 1 <= count <= 500:
            await update.effective_message.reply_text("⚠️ استخدم عددًا من 1 إلى 500، مثل: مسح 50")
            return
        chat_id = update.effective_chat.id
        ids = list(recent_message_ids[chat_id])
        # Exclude the command message itself, and delete most recent observed messages only.
        ids = [mid for mid in ids if mid != update.effective_message.message_id][-count:]
        deleted = 0
        failed = 0
        for mid in reversed(ids):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                deleted += 1
            except Exception as exc:
                failed += 1
                log.info("Bulk delete skipped message %s in %s: %s", mid, chat_id, exc)
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        try:
            report = await context.bot.send_message(chat_id, f"🗑️ اكتملت عملية المسح.\nتم حذف: {deleted} رسالة\nتعذر حذف: {failed} رسالة\n\nملاحظة: يستطيع البوت مسح الرسائل التي رصدها منذ تشغيله فقط، ولا يستطيع جلب سجل الرسائل القديم من تيليجرام.")
        except Exception:
            report = None
        log_action(chat_id, update.effective_user.id, "bulk_delete", f"requested={count};deleted={deleted};failed={failed}")
        return
    if text in ARABIC_ALIASES:
        field, value = ARABIC_ALIASES[text]
        await set_lock(update, context, field, value)

recent_messages = defaultdict(lambda: deque(maxlen=6))
recent_message_ids = defaultdict(lambda: deque(maxlen=500))
# حماية اختيارية من الإغراق: لا تعمل إلا عند تفعيلها من لوحة الحماية.
flood_message_times = defaultdict(deque)
flood_notice_last = {}

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
    register_group(chat)
    # الردود الآلية: تهيئة الردود الافتراضية مرة واحدة لكل قروب، مع تهدئة لمنع الإزعاج.
    if msg.from_user and not msg.from_user.is_bot and msg.text and not msg.text.startswith("/"):
        ensure_auto_replies(chat.id)
        enabled_row = db.execute("SELECT enabled FROM auto_reply_settings WHERE chat_id=?", (chat.id,)).fetchone()
        if enabled_row and enabled_row[0]:
            normalized = normalize_auto_reply(msg.text)
            rules = db.execute("SELECT trigger_text,reply_text FROM auto_replies WHERE chat_id=? AND enabled=1 ORDER BY LENGTH(trigger_text) DESC", (chat.id,)).fetchall()
            for trigger, reply in rules:
                needle = normalize_auto_reply(trigger)
                if needle and needle in normalized:
                    key = (chat.id, trigger)
                    now_ts = __import__("time").monotonic()
                    if now_ts - _auto_reply_last.get(key, -1e9) >= _AUTO_REPLY_COOLDOWN_SECONDS:
                        try:
                            await msg.reply_text(reply)
                            _auto_reply_last[key] = now_ts
                        except Exception as exc:
                            log.info("تعذر إرسال الرد الآلي في %s: %s", chat.id, exc)
                    break
    # The Bot API cannot read old chat history; retain IDs of messages observed while running.
    recent_message_ids[chat.id].append(msg.message_id)
    if msg.from_user and not msg.from_user.is_bot:
        current_name = msg.from_user.full_name or "عضو"
        current_username = msg.from_user.username or ""
        previous = db.execute(
            "SELECT display_name, username FROM member_activity WHERE chat_id=? AND user_id=?",
            (chat.id, uid)
        ).fetchone()
        if previous:
            old_name = previous[0] or "عضو"
            old_username = previous[1] or ""
            changed = (old_name != current_name) or (bool(old_username) and old_username != current_username)
            if changed:
                old_user_label = f"@{old_username}" if old_username else "بدون اسم مستخدم"
                new_user_label = f"@{current_username}" if current_username else "بدون اسم مستخدم"
                change_text = (
                    "🔔 تم رصد تغيير في بيانات عضو\n"
                    f"👥 القروب: {chat.title or chat.id}\n"
                    f"👤 العضو: {current_name}\n"
                    f"🆔 المعرّف الثابت: {uid}\n"
                    f"📝 الاسم السابق: {old_name} ({old_user_label})\n"
                    f"🆕 الاسم الحالي: {current_name} ({new_user_label})"
                )
                try:
                    await context.bot.send_message(chat_id=chat.id, text=change_text)
                except Exception as exc:
                    log.info("تعذر نشر تنبيه تغيير الاسم في %s: %s", chat.id, exc)
                await notify_owners(context, change_text, "membership")
        db.execute(
            "INSERT INTO member_activity(chat_id,user_id,display_name,message_count,stars,custom_title,username) "
            "VALUES(?,?,?,?,?, '', ?) ON CONFLICT(chat_id,user_id) DO UPDATE SET "
            "display_name=excluded.display_name, username=excluded.username, "
            "message_count=member_activity.message_count+1, "
            "stars=CAST((member_activity.message_count+1)/10 AS INTEGER)",
            (chat.id, uid, current_name, 1, 0, current_username)
        )
        db.commit()
    if msg.from_user and not msg.from_user.is_bot and archive_is_enabled(chat.id):
        body = msg.text or msg.caption or "[رسالة غير نصية]"
        db.execute(
            "INSERT INTO message_archive(chat_id,chat_title,user_id,username,display_name,message_id,message_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (chat.id, chat.title or "", uid, msg.from_user.username or "", msg.from_user.full_name or "", msg.message_id, body[:3000], now())
        )
        db.commit()
    # المدراء معفيون من فلاتر الحذف التلقائي.
    if is_manager(uid) or is_delegated_owner(uid) or is_protection_manager(uid):
        return
    s = get_settings(chat.id)
    if s.get("flood", 0) and msg.from_user and not msg.from_user.is_bot:
        key = (chat.id, uid)
        now_mono = time.monotonic()
        times = flood_message_times[key]
        while times and now_mono - times[0] > 8:
            times.popleft()
        times.append(now_mono)
        if len(times) >= 7:
            try:
                await msg.delete()
                log_action(chat.id, uid, "anti_flood", f"7+ messages within 8 seconds; message {msg.message_id}")
            except Exception as exc:
                log.info("تعذر حذف رسالة الإغراق في %s: %s", chat.id, exc)
            last_notice = flood_notice_last.get(key, 0)
            if now_mono - last_notice >= 60:
                flood_notice_last[key] = now_mono
                await notify_owners(context, f"🧯 رصد إغراق رسائل\nالقروب: {chat.title or chat.id}\nالعضو: {msg.from_user.full_name} (ID: {uid})\nالإجراء: حذف الرسالة بعد رصد 7 رسائل خلال 8 ثوانٍ.", "security")
            return
    delete = False
    reason = ""

    if s["links"] and has_link(msg.text or msg.caption):
        delete, reason = True, "رابط"
    if s["photos"] and msg.photo:
        delete, reason = True, "صورة"
    if s["videos"] and (msg.video or msg.video_note):
        delete, reason = True, "فيديو"
    if s["audio"] and (msg.audio or msg.voice):
        delete, reason = True, "صوت"
    if s["files"] and (msg.document or msg.animation):
        delete, reason = True, "ملف"
    if s["stickers"] and msg.sticker:
        delete, reason = True, "ملصق"
    if s["gif"] and msg.animation:
        delete, reason = True, "متحركة"
    if s["username"] and has_tag(msg.text or msg.caption):
        delete, reason = True, "معرف"
    if s["tag"] and (msg.entities or msg.caption_entities):
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(getattr(e, "type", "") in ("mention", "text_mention") for e in entities):
            delete, reason = True, "تاق"
    if s["bots"] and msg.from_user and msg.from_user.is_bot:
        delete, reason = True, "بوت"
    if s["keyboard"] and msg.reply_markup:
        delete, reason = True, "كيبورد"
    if s["games"] and msg.game:
        delete, reason = True, "لعبة"

    if s["repeat"] and msg.text:
        key = (chat.id, uid)
        old = list(recent_messages[key])
        if msg.text.strip() in old:
            delete, reason = True, "تكرار"
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
                log_action(update.effective_chat.id, msg.from_user.id if msg.from_user else 0, "delete_edit", "تعديل")
            except Exception:
                pass

async def chat_member_handler(update, context):
    cm = update.chat_member
    if not cm or not cm.chat:
        return
    chat_id = cm.chat.id
    register_group(cm.chat)
    s = get_settings(chat_id)
    old = cm.old_chat_member.status
    new = cm.new_chat_member.status
    member = cm.new_chat_member.user
    if member and old in ("member", "administrator", "restricted") and new in ("left", "kicked"):
        log_action(chat_id, member.id, "member_leave", f"{member.full_name} ({new})")
        await notify_owners(context, f"🚪 مغادرة عضو\nالمجموعة: {cm.chat.title or chat_id}\nالعضو: {member.full_name}\nالمعرّف: {member.id}", "membership")
    elif member and old in ("left", "kicked") and new in ("member", "administrator", "restricted") and not member.is_bot:
        log_action(chat_id, member.id, "member_join", member.full_name)
        await notify_owners(context, f"👋 دخول عضو\nالمجموعة: {cm.chat.title or chat_id}\nالعضو: {member.full_name}\nالمعرّف: {member.id}", "membership")
    if member and member.is_bot and member.id == context.bot.id and old in ("left", "kicked") and new in ("member", "administrator"):
        await notify_owners(context, f"📡 تمت إضافة البوت إلى مجموعة\nالمجموعة: {cm.chat.title or chat_id}\nالمعرّف: {chat_id}\nالحالة: {new}", "groups")
    if member and member.is_bot and member.id == context.bot.id and new in ("left", "kicked"):
        await notify_owners(context, f"⚠️ أُزيل البوت أو حُظر من مجموعة\nالمجموعة: {cm.chat.title or chat_id}\nالمعرّف: {chat_id}", "groups")
    # Welcome / entry message deletion is handled by service messages.
    if s["entry"] and new == "member" and old in ("left", "kicked"):
        # Telegram may not allow deleting every service message in every chat state.
        try:
            await context.bot.delete_message(chat_id, cm.new_chat_member.user.id)
        except Exception:
            pass

async def verification_timeout(bot, chat_id, user_id):
    """إزالة العضو الذي لم يكمل التحقق خلال دقيقتين؛ تعمل فقط عند نجاح التقييد أولًا."""
    await asyncio.sleep(120)
    row = db.execute("SELECT 1 FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    if not row:
        return
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        db.execute("DELETE FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        db.commit()
        log_action(chat_id, user_id, "verification_timeout", "لم يكمل التحقق خلال 120 ثانية")
        try:
            await bot.send_message(chat_id, f"🚫 تمت إزالة العضو {user_id} لعدم إكمال التحقق خلال دقيقتين.")
        except Exception:
            pass
    except Exception as exc:
        log.warning("تعذر إزالة عضو لم يكمل التحقق %s في %s: %s", user_id, chat_id, exc)


async def new_members(update, context):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    register_group(update.effective_chat)
    s = get_settings(update.effective_chat.id)
    welcome_row = db.execute("SELECT message FROM welcome_messages WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
    for member in msg.new_chat_members:
        if not member.is_bot:
            await notify_owners(context, f"👋 دخول عضو جديد\nالمجموعة: {update.effective_chat.title or update.effective_chat.id}\nالعضو: {member.full_name}\nالمعرّف: {member.id}", "membership")
            if welcome_row and welcome_row[0]:
                welcome_text = welcome_row[0].replace("{name}", member.full_name).replace("{group}", update.effective_chat.title or "القروب")
                try:
                    await context.bot.send_message(update.effective_chat.id, welcome_text)
                except Exception as e:
                    log.warning("Welcome message failed for %s: %s", update.effective_chat.id, e)
            if s.get("verify_new_members", 0):
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=update.effective_chat.id, user_id=member.id,
                        permissions=ChatPermissions(can_send_messages=False, can_send_audios=False,
                            can_send_documents=False, can_send_photos=False, can_send_videos=False,
                            can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False,
                            can_send_other_messages=False, can_add_web_page_previews=False)
                    )
                    db.execute("INSERT OR REPLACE INTO pending_verifications(chat_id,user_id,created_at) VALUES(?,?,?)",
                               (update.effective_chat.id, member.id, now()))
                    db.commit()
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"👤 أهلًا {member.full_name}! أكمل التحقق خلال دقيقتين للمشاركة في القروب.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ أنا إنسان — تحقق", callback_data=f"verify_member:{update.effective_chat.id}:{member.id}")]])
                    )
                    context.application.create_task(verification_timeout(context.bot, update.effective_chat.id, member.id))
                except Exception as exc:
                    db.execute("DELETE FROM pending_verifications WHERE chat_id=? AND user_id=?", (update.effective_chat.id, member.id))
                    db.commit()
                    log.warning("تعذر بدء التحقق للعضو %s في %s: %s", member.id, update.effective_chat.id, exc)
                    await notify_owners(context, f"⚠️ تعذر بدء التحقق من عضو جديد\nالقروب: {update.effective_chat.title or update.effective_chat.id}\nالعضو: {member.full_name} (ID: {member.id})\nتحقق من صلاحيات البوت كمشرف.", "security")
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

async def broadcast_draft_message(update, context):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != ChatType.PRIVATE:
        return
    remember_delegated_user(user)
    if context.user_data.get("awaiting_delegated_owner_id"):
        if not is_owner(user.id):
            context.user_data.pop("awaiting_delegated_owner_id", None)
            await msg.reply_text("⛔ هذا الإجراء للمالك الأساسي فقط.")
            return
        raw_id = (msg.text or "").strip()
        if not raw_id.isdigit():
            await msg.reply_text("أرسل رقم المستخدم فقط، أو اضغط إلغاء من لوحة التحكم.")
            return
        target_uid = int(raw_id)
        if target_uid <= 0 or target_uid == OWNER_ID:
            await msg.reply_text("❌ رقم غير صالح أو هذا الحساب هو المالك الأساسي بالفعل.")
            return
        try:
            target_chat = await context.bot.get_chat(target_uid)
            if getattr(target_chat, "is_bot", False):
                await msg.reply_text("❌ لا يمكن تعيين حساب بوت كمالك مفوّض.")
                return
            display_name = getattr(target_chat, "full_name", "") or getattr(target_chat, "first_name", "") or ""
            username = getattr(target_chat, "username", "") or ""
        except Exception:
            # Telegram may not reveal a user until they have started the bot; ID can still be recorded.
            display_name, username = "", ""
        db.execute("""INSERT INTO delegated_owners(user_id,added_by,added_at,display_name,username)
                      VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET added_by=excluded.added_by,
                      added_at=excluded.added_at,
                      display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE delegated_owners.display_name END,
                      username=CASE WHEN excluded.username!='' THEN excluded.username ELSE delegated_owners.username END""",
                   (target_uid, user.id, now(), display_name, username))
        db.commit()
        context.user_data.pop("awaiting_delegated_owner_id", None)
        await msg.reply_text(
            f"✅ تم تسجيل المعرّف {target_uid} كمالك مفوّض.\n"
            "✅ لديه صلاحيات الإدارة التشغيلية والحماية والنشر في جميع القروبات المسجلة التي يوجد فيها البوت.\n"
            "🔐 إدارة المالكين والأرشيف الخاص والنسخ الاحتياطي وإعدادات النظام العليا تبقى للمالك الأساسي.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 إدارة المالكين المفوضين", callback_data="delegated_owner")]])
        )
        return
    pending_action = context.user_data.get("awaiting_admin_action")
    if pending_action:
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("أرسل رقمًا صحيحًا فقط، أو افتح لوحة /panel للإلغاء.")
            return
        action = pending_action.get("action")
        target_id = int(raw)
        chat_id = int(pending_action.get("chat_id"))
        if not await can_access_group(context.bot, user.id, chat_id):
            context.user_data.pop("awaiting_admin_action", None)
            await msg.reply_text("⛔ لم تعد تملك صلاحية إدارة هذا القروب.")
            return
        try:
            if action == "ban":
                await context.bot.ban_chat_member(chat_id, target_id)
            elif action == "unban":
                await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            elif action == "kick":
                await context.bot.ban_chat_member(chat_id, target_id)
                await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            elif action == "mute":
                await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False))
            elif action == "unmute":
                await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
            elif action == "delete":
                await context.bot.delete_message(chat_id, target_id)
            elif action == "pin":
                await context.bot.pin_chat_message(chat_id, target_id)
            labels = {"ban":"حظر العضو", "unban":"فك حظر العضو", "kick":"طرد العضو", "mute":"كتم العضو", "unmute":"إلغاء كتم العضو", "delete":"حذف الرسالة", "pin":"تثبيت الرسالة"}
            log_action(chat_id, user.id, action, str(target_id))
            await msg.reply_text(f"✅ تم تنفيذ: {labels.get(action, action)}\nالقروب: {chat_id}\nالرقم: {target_id}")
        except Exception as exc:
            log.warning("Private group action %s failed in %s for %s: %s", action, chat_id, target_id, exc)
            await msg.reply_text("❌ تعذر تنفيذ الإجراء. تأكد من صحة الرقم، وأن البوت مشرف في القروب ولديه الصلاحية المطلوبة، وأن العضو/الرسالة قابلان لهذا الإجراء.")
        finally:
            context.user_data.pop("awaiting_admin_action", None)
        return
    welcome_chat_id = context.user_data.get("awaiting_welcome_chat")
    if welcome_chat_id and (is_general_manager(user.id) or is_protection_manager(user.id) or is_delegated_owner(user.id)) and await can_access_group(context.bot, user.id, welcome_chat_id):
        body = msg.text or msg.caption
        if not body:
            await msg.reply_text("أرسل نص ترحيب أو تعليقًا نصيًا.")
            return
        db.execute("INSERT OR REPLACE INTO welcome_messages(chat_id,message) VALUES(?,?)", (welcome_chat_id, body[:1000]))
        db.commit()
        log_action(welcome_chat_id, user.id, "welcome_set", "تم تحديث رسالة الترحيب")
        context.user_data.pop("awaiting_welcome_chat", None)
        await msg.reply_text("✅ تم حفظ رسالة الترحيب. ستُرسل عند دخول أعضاء جدد إذا كان البوت قادرًا على إرسال الرسائل في القروب.")
        return
    if not context.user_data.get("awaiting_broadcast") or not (is_owner(user.id) or is_delegated_owner(user.id)):
        return
    context.user_data["broadcast_draft"] = {"chat_id": chat.id, "message_id": msg.message_id}
    selected = context.user_data.get("broadcast_selected_groups", [])
    await msg.reply_text(
        "👀 معاينة الإعلان جاهزة. سيتم إرساله فقط إلى القروبات المحددة والمصرح لك بها.\n"
        f"عدد القروبات المستهدفة: {len(selected)}\nهل تريد المتابعة؟",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد النشر للقروبات المحددة", callback_data="broadcast_confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")]
        ])
    )
    context.user_data["awaiting_broadcast"] = False

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data == "health":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ هذا القسم للمصرّح لهم فقط.")
            return
        try:
            groups_count = db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0]
            delegated_count = db.execute("SELECT COUNT(*) FROM delegated_owners").fetchone()[0] if is_owner(uid) else None
            logs_count = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            pending_count = db.execute("SELECT COUNT(*) FROM pending_verifications").fetchone()[0]
            db_status = "🟢 متصلة"
        except Exception:
            log.exception("Health panel database check failed")
            groups_count, logs_count, pending_count = 0, 0, 0
            db_status = "🔴 تعذر الفحص"
        health_lines = [
            "💚 مؤشر صحة البوت",
            "",
            f"🗄️ قاعدة البيانات: {db_status}",
            f"👥 القروبات المسجلة: {groups_count}",
            f"📋 العمليات المسجلة: {logs_count}",
            f"🕵️ طلبات التحقق المعلقة: {pending_count}",
            "🌐 فحص الاستضافة: نقطة HTTP مفعّلة عند تشغيل الخدمة.",
            "",
            "ℹ️ هذه قراءة داخلية للحالة، ولا تؤكد وحدها أن Telegram API أو كل صلاحيات البوت تعمل دون أخطاء.",
        ]
        if delegated_count is not None:
            health_lines.insert(4, f"🤝 المالكـون المفوضون: {delegated_count}")
        await q.edit_message_text("\n".join(health_lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث الحالة", callback_data="health")],
            [InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")],
        ]))
        return

    if data.startswith("verify_member:"):
        try:
            _, chat_raw, user_raw = data.split(":", 2)
            chat_id, target_uid = int(chat_raw), int(user_raw)
        except ValueError:
            await q.answer("بيانات التحقق غير صحيحة.", show_alert=True)
            return
        if uid != target_uid:
            await q.answer("زر التحقق مخصص للعضو الجديد فقط.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat_id, target_uid)).fetchone():
            await q.answer("تم التحقق مسبقًا أو انتهت المهلة.", show_alert=True)
            return
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target_uid,
                permissions=ChatPermissions(can_send_messages=True, can_send_audios=True,
                    can_send_documents=True, can_send_photos=True, can_send_videos=True,
                    can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True)
            )
            db.execute("DELETE FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat_id, target_uid))
            db.commit()
            log_action(chat_id, target_uid, "verification_complete", "اكتمل التحقق")
            await q.edit_message_text("✅ تم التحقق بنجاح، أهلًا بك في القروب!")
        except Exception as exc:
            log.warning("تعذر إكمال التحقق للعضو %s في %s: %s", target_uid, chat_id, exc)
            await q.answer("تعذر إكمال التحقق. أبلغ مشرف القروب ليتأكد من صلاحيات البوت.", show_alert=True)
        return

    if data == "noop" or data.startswith("noop:"):
        await q.answer("هذا الخيار للتوضيح فقط.", show_alert=False)
        return

    if data.startswith("member_games:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("معرّف القروب غير صحيح.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await q.answer("هذا القروب غير مسجل.", show_alert=True)
            return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,))
        db.commit()
        cfg = db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if not cfg or not cfg[0]:
            await q.edit_message_text("🎮 الألعاب متوقفة حاليًا في هذا القروب.")
            return
        rows = [
            [InlineKeyboardButton("🧠 أسئلة ثقافية وذكاء", callback_data=f"game_quiz:{chat_id}"), InlineKeyboardButton("🔤 تحدي الكلمات", callback_data=f"game_word:{chat_id}")],
            [InlineKeyboardButton("🧩 فك الكلمات المبعثرة", callback_data=f"game_scramble:{chat_id}"), InlineKeyboardButton("✊ حجر ورقة مقص", callback_data=f"game_rps:{chat_id}")],
            [InlineKeyboardButton("⭕ إكس أو لاعبين", callback_data=f"game_xo_start:{chat_id}"), InlineKeyboardButton("⚡ تحدي السرعة", callback_data=f"game_speed:{chat_id}")],
            [InlineKeyboardButton("🎯 تحدي اليوم", callback_data=f"game_daily:{chat_id}"), InlineKeyboardButton("🏆 المتصدرون", callback_data=f"game_scores:{chat_id}")],
            [InlineKeyboardButton("⬅️ مركز الأعضاء", callback_data="public:back")]
        ]
        await q.edit_message_text("🎮 مركز الألعاب والتحديات\n\nاختر اللعبة التي تريد المشاركة فيها:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "public:commands":
        chat_id = q.message.chat.id
        settings = get_settings(chat_id)
        lines = ["📚 أوامر البوت المتاحة للأعضاء", "", "🆔 /id — عرض رقم حسابك",
                 "🆔 /idgroup — عرض رقم القروب", "⚙️ /settings — فتح دليل المساعدة", ""]
        if settings.get("public_protection", 1):
            lines += ["🛡️ أوامر الحماية (للمصرح لهم فقط):",
                      "🔗 /locklinks و /unlocklinks — قفل/فتح الروابط",
                      "🖼️ /lockphoto و /unlockphoto — قفل/فتح الصور",
                      "🎥 /lockvideo و /unlockvideo — قفل/فتح الفيديو",
                      "🔊 /lockaudio و /unlockaudio — قفل/فتح الصوت",
                      "📁 /lockfile و /unlockfile — قفل/فتح الملفات",
                      "🎭 /lockstickers و /unlockstickers — قفل/فتح الملصقات",
                      "🎞️ /lockgif و /unlockgif — قفل/فتح الصور المتحركة",
                      "🏷️ /locktag و /unlocktag — منع/السماح بالإشارات",
                      "🔁 /lockrepeat و /unlockrepeat — منع/السماح بالتكرار",
                      "🎮 /lockgames و /unlockgames — قفل/فتح الألعاب", ""]
        lines += ["👥 أوامر الإدارة (للمصرح لهم فقط):",
                  "🔨 /kick — طرد عضو (بالرد على رسالته)",
                  "🚫 /ban — حظر عضو (بالرد أو بالمعرّف)",
                  "🔓 /unban — فك الحظر باستخدام المعرّف",
                  "🔇 /mute و /unmute — تقييد/إلغاء تقييد عضو",
                  "🗑️ /del — حذف رسالة (بالرد عليها)",
                  "🧹 مسح 1 إلى مسح 500 — مسح الرسائل التي رصدها البوت منذ تشغيله (للمصرح لهم)",
                  "📌 /pin — تثبيت رسالة (بالرد عليها)",
                  "📋 /logs — عرض سجل العمليات للمدير المصرح", "",
                  "🔐 عرض الأوامر لا يمنح صلاحية استخدامها؛ التنفيذ يقتصر على المصرح لهم، وقد يتطلب منح البوت صلاحيات مشرف.",
                  "⬅️ اكتب «اعدادات» للعودة إلى قائمة المساعدة."]
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ اعدادات", callback_data="public:back")]]))
        return
    if data == "public:activity":
        chat_id = q.message.chat.id
        settings = get_settings(chat_id)
        if not settings.get("public_activity", 1):
            await q.edit_message_text("⭐ عرض نشاط الأعضاء غير متاح حاليًا.")
            return
        rows = db.execute("SELECT display_name,message_count,stars,custom_title FROM member_activity WHERE chat_id=? ORDER BY message_count DESC, stars DESC LIMIT 15", (chat_id,)).fetchall()
        if rows:
            lines = ["⭐ ترتيب نشاط الأعضاء (آخر 15 عضوًا):", ""]
            for i, (name, count, stars, title) in enumerate(rows, 1):
                label = title or ("عضو نشط" if count >= 20 else "عضو متفاعل" if count >= 5 else "عضو")
                lines.append(f"{i}. {name or 'عضو'} — 💬 {count} رسالة — ⭐ {stars} — 🏷️ {label}")
            text = "\n".join(lines)
        else:
            text = "⭐ لا توجد إحصائيات بعد. سيبدأ العد من الرسائل التي يستقبلها البوت من الآن."
        text += "\n\nℹ️ النجوم والألقاب للتشجيع فقط ولا تمنح صلاحيات إدارية."
        await q.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ اعدادات", callback_data="public:back")]]))
        return
    if data == "public:back":
        # Rebuild the public menu in the same group.
        settings = get_settings(q.message.chat.id)
        buttons = []
        if settings.get("public_commands", 1): buttons.append(InlineKeyboardButton("📚 دليل البوت", callback_data="public:commands"))
        if settings.get("public_activity", 1): buttons.append(InlineKeyboardButton("🏆 لوحة نشاط الأعضاء", callback_data="public:activity"))
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2) if buttons[i:i+2]]
        if settings.get("public_protection", 1): rows.append([InlineKeyboardButton("🛡️ مركز الحماية", callback_data="public:protection")])
        await q.edit_message_text("🤖 مركز المساعدة\n\n📚 دليل البوت • 🏆 نشاط الأعضاء • 🛡️ معلومات الحماية\n🎮 يمكن المشاركة في الألعاب المتاحة داخل القروب.\n\n🔐 أدوات الإدارة الخاصة لا تظهر هنا، ولا تتاح إلا للحسابات المصرّح لها.", reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        return
    if data == "public:protection":
        await q.edit_message_text(
            "🛡️ معلومات الحماية\n\n"
            "يعمل البوت على المساعدة في الحفاظ على تنظيم القروب وفق الإعدادات المعتمدة من الإدارة.\n"
            "🔒 إعدادات الحماية وأدوات الإدارة غير متاحة للأعضاء العاديين."
        )
        return

    if data == "owner_backup":
        if not is_owner(uid):
            await q.answer("⛔ النسخ الاحتياطي للمالك الأساسي فقط.", show_alert=True)
            return
        # Create a consistent SQLite snapshot without exposing the live database file.
        backup_path = None
        try:
            fd, backup_path = tempfile.mkstemp(prefix="telegram_group_bot_backup_", suffix=".db")
            os.close(fd)
            backup_db = sqlite3.connect(backup_path)
            try:
                db.backup(backup_db)
            finally:
                backup_db.close()
            with open(backup_path, "rb") as backup_file:
                await context.bot.send_document(
                    chat_id=uid,
                    document=backup_file,
                    filename="telegram_group_bot_backup.db",
                    caption="💾 نسخة احتياطية من قاعدة بيانات البوت. احتفظ بها في مكان آمن ولا تشاركها؛ قد تحتوي على سجلات وإعدادات خاصة."
                )
            await q.edit_message_text("✅ تم إنشاء النسخة الاحتياطية وإرسالها إلى محادثتك الخاصة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        except Exception as exc:
            log.exception("Backup failed")
            await q.edit_message_text("❌ تعذر إنشاء النسخة الاحتياطية أو إرسالها. تحقق من أن البوت يستطيع مراسلتك خاصًا وأن قاعدة البيانات قابلة للقراءة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        finally:
            if backup_path and os.path.exists(backup_path):
                try: os.remove(backup_path)
                except OSError: pass
        return

    if data == "owner_system":
        if not is_owner(uid):
            await q.answer("⛔ إعدادات النظام العليا للمالك الأساسي فقط.", show_alert=True)
            return
        groups_count = db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0]
        delegated_count = db.execute("SELECT COUNT(*) FROM delegated_owners").fetchone()[0]
        await q.edit_message_text(
            "⚙️ إعدادات النظام العليا — المالك الأساسي فقط\n\n"
            f"👥 القروبات المسجلة: {groups_count}\n"
            f"🤝 المالكون المفوّضون: {delegated_count}\n\n"
            "🔐 هذا القسم خاص بالمالك الأساسي. المالك المفوّض يستطيع استخدام لوحة التشغيل للقروبات التي هو عضو فيها، بما يشمل النشر والترحيب والإحصائيات والسجلات والحماية وأوامر الإشراف، لكنه لا يستطيع إدارة المالكين أو النسخ الاحتياطي أو إعدادات النظام العليا.\n\n"
            "ملاحظة: إعدادات الحماية داخل القروب تُدار وفق صلاحياته التشغيلية، أما الصلاحيات العليا وإدارة المفوّضين فتبقى للمالك الأساسي.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💾 إنشاء نسخة احتياطية", callback_data="owner_backup")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "home":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        await q.edit_message_text(
            "👑 لوحة التحكم الرئيسية\n\n"
            f"الصلاحية: {role_name(uid)}\n\nاختر القسم المطلوب:",
            reply_markup=panel_markup(uid)
        )
        return

    if data.startswith("settings_group:"):
        try: chat_id = int(data.split(":", 1)[1])
        except ValueError: await q.answer("معرّف القروب غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        await q.edit_message_text(settings_text(0), reply_markup=settings_page_markup(chat_id, 0))
        return

    if data.startswith("settings_page:"):
        parts = data.split(":")
        try:
            if len(parts) == 3: chat_id, page = int(parts[1]), int(parts[2])
            else: chat_id, page = q.message.chat.id, int(parts[1])
        except ValueError: await q.answer("بيانات الإعدادات غير صحيحة", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية إعدادات هذا القروب.", show_alert=True); return
        await q.edit_message_text(settings_text(page), reply_markup=settings_page_markup(chat_id, page))
        return

    if data.startswith("toggle:"):
        parts = data.split(":")
        try:
            if len(parts) == 4: chat_id, field, page = int(parts[1]), parts[2], int(parts[3])
            else: field, page, chat_id = parts[1], int(parts[2]), q.message.chat.id
        except (ValueError, IndexError): await q.answer("بيانات الإعداد غير صحيحة", show_alert=True); return
        if field not in SETTING_FIELDS or not await can_access_group(context.bot, uid, chat_id):
            await q.answer("ليس لديك صلاحية تغيير هذا الإعداد.", show_alert=True); return
        s = get_settings(chat_id)
        new_value = 0 if s[field] else 1
        set_setting(chat_id, field, new_value)
        log_action(chat_id, uid, "setting", f"{field}={new_value}")
        await q.edit_message_text(settings_text(page), reply_markup=settings_page_markup(chat_id, page))
        return

    if data in ("security", "security_groups"):
        if not (is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الحماية."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🛡️ {title or cid}", callback_data=f"settings_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("🛡️ الحماية وإعدادات القروبات\nاختر قروبًا أنت عضو فيه لتغيير إعداداته. تتطلب إجراءات الحذف صلاحيات إدارية للبوت في تيليجرام.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data in ("administration", "administration_groups"):
        if not (is_owner(uid) or is_general_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإدارة."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🔨 {title or cid}", callback_data=f"admin_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("🔨 أوامر الإشراف\nاختر قروبًا لعرض الأوامر المتاحة. تُنفّذ الأوامر داخل القروب وبحسب صلاحيات البوت في تيليجرام.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("admin_group:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية هذا القروب", show_alert=True); return
        title = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        await q.edit_message_text(
            f"🔨 إدارة القروب — {title[0] if title else chat_id}\n\n"
            "اختر الإجراء، ثم أرسل رقم المستخدم أو رقم الرسالة في الخاص. "
            "تتطلب الإجراءات صلاحيات مشرف البوت المناسبة داخل تيليجرام.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 حظر عضو", callback_data=f"admin_action:ban:{chat_id}"), InlineKeyboardButton("🔓 فك الحظر", callback_data=f"admin_action:unban:{chat_id}")],
                [InlineKeyboardButton("👢 طرد عضو", callback_data=f"admin_action:kick:{chat_id}"), InlineKeyboardButton("🔇 كتم عضو", callback_data=f"admin_action:mute:{chat_id}")],
                [InlineKeyboardButton("🔊 إلغاء الكتم", callback_data=f"admin_action:unmute:{chat_id}")],
                [InlineKeyboardButton("🗑️ حذف رسالة برقمها", callback_data=f"admin_action:delete:{chat_id}"), InlineKeyboardButton("📌 تثبيت رسالة برقمها", callback_data=f"admin_action:pin:{chat_id}")],
                [InlineKeyboardButton("🛡️ إعدادات الحماية", callback_data=f"settings_group:{chat_id}")],
                [InlineKeyboardButton("⬅️ القروبات", callback_data="administration_groups")]
            ])
        )
        return

    if data.startswith("admin_action:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"ban", "unban", "kick", "mute", "unmute", "delete", "pin"}:
            await q.answer("الإجراء غير صحيح.", show_alert=True); return
        action = parts[1]
        try: chat_id = int(parts[2])
        except ValueError: await q.answer("معرّف القروب غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية إدارة هذا القروب.", show_alert=True); return
        context.user_data["awaiting_admin_action"] = {"action": action, "chat_id": chat_id}
        labels = {"ban":"رقم المستخدم الذي تريد حظره", "unban":"رقم المستخدم الذي تريد فك حظره", "kick":"رقم المستخدم الذي تريد طرده", "mute":"رقم المستخدم الذي تريد كتمه", "unmute":"رقم المستخدم الذي تريد إلغاء كتمه", "delete":"رقم الرسالة داخل القروب التي تريد حذفها", "pin":"رقم الرسالة داخل القروب التي تريد تثبيتها"}
        action_group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        action_group_title = action_group[0] if action_group and action_group[0] else str(chat_id)
        await q.edit_message_text(
            f"✍️ الإجراء: {labels[action]}\nالقروب: {action_group_title}\n\n"
            "أرسل الرقم فقط في الخاص. لن ينفّذ البوت أي إجراء إلا بعد استلام الرقم، ويمكنك الإلغاء من الزر أدناه.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_action_cancel")], [InlineKeyboardButton("⬅️ قائمة القروب", callback_data=f"admin_group:{chat_id}")]])
        ); return

    if data == "admin_action_cancel":
        context.user_data.pop("awaiting_admin_action", None)
        await q.edit_message_text("تم إلغاء الإجراء.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data == "welcome":
        if not (is_owner(uid) or is_general_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الترحيب."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"👋 {title or cid}", callback_data=f"welcome_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("👋 إعداد الترحيب لكل قروب\nاختر قروبًا أنت عضو فيه لتعيين الرسالة أو إيقافها.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data == "games":
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await q.edit_message_text("⛔ هذه القائمة للمصرّح لهم فقط.")
            return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🎮 {title or cid}", callback_data=f"games_group:{cid}")] for cid,title in groups[:30]]
        rows.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="home")])
        await q.edit_message_text("🎮 الألعاب والتحديات\n\nاختر قروبًا لإدارة الألعاب أو بدء اللعب. النقاط مستقلة لكل قروب.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("games_group:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف القروب غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية الوصول لهذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,)); db.commit()
        cfg = db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = title_row[0] if title_row else str(chat_id)
        rows = [
            [InlineKeyboardButton("🧠 أسئلة ثقافية وذكاء", callback_data=f"game_quiz:{chat_id}"), InlineKeyboardButton("🔤 تحدي الكلمات", callback_data=f"game_word:{chat_id}")],
            [InlineKeyboardButton("🧩 فك الكلمات المبعثرة", callback_data=f"game_scramble:{chat_id}"), InlineKeyboardButton("✊ حجر ورقة مقص", callback_data=f"game_rps:{chat_id}")],
            [InlineKeyboardButton("⭕ إكس أو لاعبين", callback_data=f"game_xo_start:{chat_id}"), InlineKeyboardButton("⚡ تحدي السرعة", callback_data=f"game_speed:{chat_id}")],
            [InlineKeyboardButton("🎯 تحدي اليوم", callback_data=f"game_daily:{chat_id}"), InlineKeyboardButton("🎯 تحدي الخاسر", callback_data=f"game_challenge:{chat_id}")],
            [InlineKeyboardButton("🏆 المتصدرون", callback_data=f"game_scores:{chat_id}"), InlineKeyboardButton("📅 أسبوعي/شهري", callback_data=f"game_periods:{chat_id}")],
        ]
        if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid):
            rows.append([InlineKeyboardButton(("🟢 إيقاف الألعاب" if cfg[0] else "🔴 تشغيل الألعاب"), callback_data=f"game_toggle:{chat_id}"), InlineKeyboardButton(("🟢 إيقاف تحدي الخاسر" if cfg[1] else "🔴 تشغيل تحدي الخاسر"), callback_data=f"game_challenge_toggle:{chat_id}")])
        rows.append([InlineKeyboardButton("⬅️ القروبات", callback_data="games")])
        await q.edit_message_text(f"🎮 الألعاب والتحديات\n👥 القروب: {title}\nحالة الألعاب: {'مفعّلة' if cfg[0] else 'متوقفة'}\nتحدي الخاسر: {'مفعّل' if cfg[1] else 'متوقف'}\n\nاختر لعبة:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("game_toggle:") or data.startswith("game_challenge_toggle:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid)):
            await q.answer("تغيير إعدادات الألعاب غير مسموح لك.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,))
        field = "enabled" if data.startswith("game_toggle:") else "challenge_enabled"
        current = db.execute(f"SELECT {field} FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()[0]
        db.execute(f"UPDATE game_config SET {field}=? WHERE chat_id=?", (0 if current else 1, chat_id)); db.commit()
        await q.answer("تم تحديث إعداد الألعاب.")
        # Return to the group games page.
        cfg = db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title = title_row[0] if title_row else str(chat_id)
        rows = [[InlineKeyboardButton("🧠 أسئلة ثقافية", callback_data=f"game_quiz:{chat_id}"), InlineKeyboardButton("🔤 تحدي الكلمات", callback_data=f"game_word:{chat_id}")], [InlineKeyboardButton("🧩 فك الكلمات", callback_data=f"game_scramble:{chat_id}"), InlineKeyboardButton("✊ حجر ورقة مقص", callback_data=f"game_rps:{chat_id}")], [InlineKeyboardButton("⭕ إكس أو لاعبين", callback_data=f"game_xo_start:{chat_id}"), InlineKeyboardButton("⚡ تحدي السرعة", callback_data=f"game_speed:{chat_id}")], [InlineKeyboardButton("🎯 تحدي اليوم", callback_data=f"game_daily:{chat_id}"), InlineKeyboardButton("🎯 تحدي الخاسر", callback_data=f"game_challenge:{chat_id}")], [InlineKeyboardButton("🏆 المتصدرون", callback_data=f"game_scores:{chat_id}"), InlineKeyboardButton("📅 أسبوعي/شهري", callback_data=f"game_periods:{chat_id}")], [InlineKeyboardButton(("🟢 إيقاف الألعاب" if cfg[0] else "🔴 تشغيل الألعاب"), callback_data=f"game_toggle:{chat_id}"), InlineKeyboardButton(("🟢 إيقاف تحدي الخاسر" if cfg[1] else "🔴 تشغيل تحدي الخاسر"), callback_data=f"game_challenge_toggle:{chat_id}")], [InlineKeyboardButton("⬅️ القروبات", callback_data="games")]]
        await q.edit_message_text(f"🎮 الألعاب والتحديات\n👥 القروب: {title}\nحالة الألعاب: {'مفعّلة' if cfg[0] else 'متوقفة'}\nتحدي الخاسر: {'مفعّل' if cfg[1] else 'متوقف'}\n\nاختر لعبة:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_quiz:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.", show_alert=True); return
        question="ما الكوكب المعروف بالكوكب الأحمر؟"; opts=[("🌍 الأرض",0),("🔴 المريخ",1),("🪐 زحل",0),("🌟 الزهرة",0)]
        rows=[[InlineKeyboardButton(label, callback_data=f"game_answer:{chat_id}:{correct}")] for label,correct in opts]
        rows.append([InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")])
        await context.bot.send_message(chat_id, f"🧠 سؤال ثقافي وذكاء\n\n{question}\n\nاختر الإجابة الصحيحة:", reply_markup=InlineKeyboardMarkup(rows))
        await q.edit_message_text("تم نشر السؤال في القروب 🧠", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_answer:"):
        try: _,chat_raw,correct_raw=data.split(":",2); chat_id=int(chat_raw); correct=int(correct_raw)
        except ValueError: await q.answer("بيانات غير صحيحة.", show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        if correct:
            db.execute("UPDATE game_scores SET points=points+3,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?", (name,chat_id,uid)); record_game_points(chat_id, uid, 3, "quiz"); msg="✅ إجابة صحيحة! ربحت 3 نقاط ⭐"
        else:
            db.execute("UPDATE game_scores SET losses=losses+1,display_name=? WHERE chat_id=? AND user_id=?", (name,chat_id,uid)); msg="❌ إجابة غير صحيحة. لم تُخصم نقاطك."
        db.commit()
        buttons=[[InlineKeyboardButton("🎯 تحدي الخاسر (اختياري)", callback_data=f"game_challenge:{chat_id}")],[InlineKeyboardButton("🎮 العودة للألعاب", callback_data=f"games_group:{chat_id}")]]
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith("game_word:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.", show_alert=True); return
        rows=[[InlineKeyboardButton("👋 مرحبا", callback_data=f"game_word_answer:{chat_id}:1"), InlineKeyboardButton("🏫 مدرسة", callback_data=f"game_word_answer:{chat_id}:0")], [InlineKeyboardButton("🌊 بحر", callback_data=f"game_word_answer:{chat_id}:0"), InlineKeyboardButton("✏️ قلم", callback_data=f"game_word_answer:{chat_id}:0")], [InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]]
        await context.bot.send_message(chat_id, "🔤 تحدي الكلمات\n\nرتّب الحروف لتكوين كلمة: (ر، ح، ب، ا)\nاختر الإجابة الصحيحة:", reply_markup=InlineKeyboardMarkup(rows))
        await q.edit_message_text("تم نشر تحدي الكلمات في القروب 🔤", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_word_answer:"):
        try: _,chat_raw,correct_raw=data.split(":",2); chat_id=int(chat_raw); correct=int(correct_raw)
        except ValueError: await q.answer("بيانات غير صحيحة.", show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        if correct:
            db.execute("UPDATE game_scores SET points=points+2,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id, uid, 2, "word"); msg="✅ إجابة صحيحة! ربحت نقطتين ⭐"
        else:
            db.execute("UPDATE game_scores SET losses=losses+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); msg="❌ إجابة غير صحيحة. حاول مرة أخرى!"
        db.commit()
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔤 حاول مجددًا", callback_data=f"game_word:{chat_id}")],[InlineKeyboardButton("🎮 الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_rps:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.", show_alert=True); return
        import random
        bot_choice=random.choice(["حجر","ورقة","مقص"])
        context.user_data[f"rps:{chat_id}"]=bot_choice
        rows=[[InlineKeyboardButton("✊ حجر", callback_data=f"game_rps_pick:{chat_id}:حجر"), InlineKeyboardButton("✋ ورقة", callback_data=f"game_rps_pick:{chat_id}:ورقة"), InlineKeyboardButton("✌️ مقص", callback_data=f"game_rps_pick:{chat_id}:مقص")], [InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]]
        await q.edit_message_text("✊ حجر ورقة مقص\nاختر حركتك، وسأقارنها باختيار البوت:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_rps_pick:"):
        try: _,chat_raw,choice=data.split(":",2); chat_id=int(chat_raw)
        except ValueError: await q.answer("بيانات غير صحيحة.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        bot_choice=context.user_data.pop(f"rps:{chat_id}", None)
        if not bot_choice: await q.answer("انتهت الجولة؛ ابدأ لعبة جديدة.", show_alert=True); return
        wins={"حجر":"مقص","ورقة":"حجر","مقص":"ورقة"}
        name=q.from_user.full_name or str(uid)
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        if choice==bot_choice: msg=f"🤝 تعادل! اختيارك {choice} واختيار البوت {bot_choice}."; db.execute("UPDATE game_scores SET display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid))
        elif wins.get(choice)==bot_choice: msg=f"🎉 فزت! اختيارك {choice} واختيار البوت {bot_choice}. ربحت 2 نقطة ⭐"; db.execute("UPDATE game_scores SET points=points+2,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id, uid, 2, "rps")
        else: msg=f"😅 خسرت! اختيارك {choice} واختيار البوت {bot_choice}."; db.execute("UPDATE game_scores SET losses=losses+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid))
        db.commit()
        buttons=[]
        cfg=db.execute("SELECT challenge_enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if choice!=bot_choice and wins.get(choice)!=bot_choice and cfg and cfg[0]: buttons.append([InlineKeyboardButton("🎯 تنفيذ تحدي الخاسر", callback_data=f"game_challenge:{chat_id}")])
        buttons.append([InlineKeyboardButton("🔁 العب مرة أخرى", callback_data=f"game_rps:{chat_id}")]); buttons.append([InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")])
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith("game_challenge:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and (not cfg[0] or not cfg[1]): await q.answer("تحدي الخاسر غير مفعّل.", show_alert=True); return
        import random
        tasks=["اكتب في القروب: أنا بطل التحديات 😎", "أرسل ثلاثة إيموجيات تعبّر عن مزاجك 🎭", "امدح عضوًا في القروب بكلمة لطيفة 🌟", "اكتب حقيقة ممتعة عن نفسك (اختياري) 🧠"]
        task=random.choice(tasks)
        challenge_token=uuid.uuid4().hex[:16]
        db.execute("INSERT INTO game_challenges(token,chat_id,user_id,task,completed,created_at) VALUES(?,?,?,?,0,?)", (challenge_token,chat_id,uid,task,now()))
        db.commit()
        await q.edit_message_text(f"🎯 تحدي الخاسر\n\nالتحدي لك يا {q.from_user.full_name or 'بطل'}:\n{task}\n\nنفّذ التحدي بنفسك في القروب؛ البوت لا ينفذ عقوبات إدارية تلقائيًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم تنفيذ التحدي", callback_data=f"game_challenge_done:{challenge_token}")],[InlineKeyboardButton("⏭️ تخطي التحدي", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_challenge_done:"):
        challenge_token=data.split(":",1)[1]
        challenge=db.execute("SELECT chat_id,user_id,completed FROM game_challenges WHERE token=?", (challenge_token,)).fetchone()
        if not challenge:
            await q.answer("انتهى هذا التحدي أو أنه غير صالح.", show_alert=True); return
        chat_id, challenge_user_id, completed=challenge
        if challenge_user_id != uid:
            await q.answer("هذا التحدي مخصص للعضو الذي استلمه.", show_alert=True); return
        if completed:
            await q.answer("تم احتساب هذا التحدي مسبقًا.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("UPDATE game_challenges SET completed=1 WHERE token=? AND completed=0", (challenge_token,))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.commit(); await q.answer("تم احتساب هذا التحدي مسبقًا.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id, uid, 1, "challenge"); db.commit()
        await q.edit_message_text("👏 أحسنت! تم تسجيل نقطة مشاركة ⭐", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 العودة للألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_scores:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        rows=db.execute("SELECT display_name,points,wins,losses FROM game_scores WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 10",(chat_id,)).fetchall()
        body="\n".join(f"{i}. {name or 'عضو'} — ⭐ {points} | 🏆 {wins} فوز | ❌ {losses} خسارة" for i,(name,points,wins,losses) in enumerate(rows,1)) or "لا توجد نتائج بعد. ابدأ اللعب لتظهر هنا!"
        await q.edit_message_text("🏆 المتصدرون في هذا القروب\n\n"+body, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_periods:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        rows=[[InlineKeyboardButton("📅 المتصدرون هذا الأسبوع", callback_data=f"game_period_scores:{chat_id}:week")],
              [InlineKeyboardButton("🗓️ المتصدرون هذا الشهر", callback_data=f"game_period_scores:{chat_id}:month")],
              [InlineKeyboardButton("🏆 الترتيب العام", callback_data=f"game_scores:{chat_id}")],
              [InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]]
        await q.edit_message_text("🏆 اختر مدة ترتيب النقاط:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_period_scores:"):
        try: _, chat_raw, period = data.split(":",2); chat_id=int(chat_raw)
        except ValueError: await q.answer("بيانات غير صحيحة.", show_alert=True); return
        if period not in ("week","month") or not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(days=7 if period=="week" else 30)).isoformat()
        rows=db.execute("SELECT e.user_id, COALESCE(s.display_name,'عضو'), SUM(e.points) pts FROM game_score_events e LEFT JOIN game_scores s ON s.chat_id=e.chat_id AND s.user_id=e.user_id WHERE e.chat_id=? AND e.created_at>=? GROUP BY e.user_id ORDER BY pts DESC LIMIT 10", (chat_id,cutoff)).fetchall()
        title="الأسبوع" if period=="week" else "آخر 30 يومًا"
        body="\n".join(f"{i}. {name} — ⭐ {points}" for i,(_,name,points) in enumerate(rows,1)) or "لا توجد نقاط مسجلة لهذه المدة بعد."
        await q.edit_message_text(f"🏆 المتصدرون — {title}\n\n{body}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المدد", callback_data=f"game_periods:{chat_id}")],[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_scramble:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.", show_alert=True); return
        token=uuid.uuid4().hex[:12]
        opts=[("ةسردم", "مدرسة"), ("رحب", "بحر"), ("ملق", "قلم"), ("باتك", "كتاب")]
        scrambled,answer=opts[int(token[:2],16)%len(opts)]
        db.execute("INSERT INTO game_rounds(token,chat_id,kind,answer,created_at) VALUES(?,?,?,?,?)",(token,chat_id,"scramble",answer,now())); db.commit()
        buttons=[[InlineKeyboardButton(x, callback_data=f"game_round_answer:{token}:{x}")] for x in ["مدرسة","بحر","قلم","كتاب"]]
        await context.bot.send_message(chat_id, f"🔤 فكّ الكلمة المبعثرة!\n\nالكلمة: {scrambled}\nأول إجابة صحيحة تحصل على نقطتين ⭐", reply_markup=InlineKeyboardMarkup(buttons))
        await q.edit_message_text("تم نشر تحدي الكلمات في القروب المحدد ✅", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_speed:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.", show_alert=True); return
        token=uuid.uuid4().hex[:12]
        db.execute("INSERT INTO game_rounds(token,chat_id,kind,answer,created_at) VALUES(?,?,?,?,?)",(token,chat_id,"speed","الرياض",now())); db.commit()
        buttons=[[InlineKeyboardButton("جدة", callback_data=f"game_round_answer:{token}:جدة"), InlineKeyboardButton("الرياض", callback_data=f"game_round_answer:{token}:الرياض")], [InlineKeyboardButton("أبها", callback_data=f"game_round_answer:{token}:أبها"), InlineKeyboardButton("تبوك", callback_data=f"game_round_answer:{token}:تبوك")]]
        await context.bot.send_message(chat_id,"⚡ تحدي السرعة!\nما عاصمة المملكة العربية السعودية؟\nأول عضو يضغط الإجابة الصحيحة يحصل على 3 نقاط ⭐",reply_markup=InlineKeyboardMarkup(buttons))
        await q.edit_message_text("نُشر تحدي السرعة في القروب ⚡",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_round_answer:"):
        try: _,token,answer=data.split(":",2)
        except ValueError: await q.answer("بيانات غير صحيحة.", show_alert=True); return
        row=db.execute("SELECT chat_id,kind,answer,completed,created_at FROM game_rounds WHERE token=?",(token,)).fetchone()
        if not row: await q.answer("انتهت الجولة أو غير موجودة.",show_alert=True); return
        chat_id,kind,correct,completed,created_at=row
        if completed: await q.answer("سبق أن فاز عضو بهذه الجولة.",show_alert=True); return
        if answer != correct: await q.answer("ليست الإجابة الصحيحة، حاول مجددًا.",show_alert=True); return
        if (datetime.now(timezone.utc)-datetime.fromisoformat(created_at)).total_seconds()>180: await q.answer("انتهى وقت الجولة.",show_alert=True); return
        db.execute("UPDATE game_rounds SET completed=1 WHERE token=? AND completed=0",(token,))
        if db.execute("SELECT changes()").fetchone()[0]!=1: db.commit(); await q.answer("سبق أن فاز عضو بهذه الجولة.",show_alert=True); return
        name=q.from_user.full_name or str(uid); points=3 if kind=="speed" else 2
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+?,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(points,name,chat_id,uid)); record_game_points(chat_id,uid,points,kind); db.commit()
        await q.edit_message_text(f"🏆 فاز {name} في {'تحدي السرعة' if kind=='speed' else 'فك الكلمة'} وربح {points} نقاط ⭐")
        return

    if data.startswith("game_daily:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح.",show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب.",show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.",show_alert=True); return
        day=datetime.now(timezone.utc).date().isoformat()
        existing=db.execute("SELECT completed FROM daily_game_challenges WHERE chat_id=? AND challenge_date=?",(chat_id,day)).fetchone()
        if existing:
            await q.answer("تم نشر تحدي اليوم بالفعل في هذا القروب.",show_alert=True); return
        answer="القمر"; question="ما الشيء الذي يظهر ليلًا ويعكس ضوء الشمس؟"
        db.execute("INSERT INTO daily_game_challenges(chat_id,challenge_date,answer) VALUES(?,?,?)",(chat_id,day,answer)); db.commit()
        buttons=[[InlineKeyboardButton(x,callback_data=f"game_daily_answer:{chat_id}:{day}:{x}")] for x in ["الشمس","القمر","السحاب","النجوم"]]
        try:
            sent=await context.bot.send_message(chat_id,f"🎯 تحدي اليوم\n\n{question}\nأول إجابة صحيحة تحصل على 5 نقاط ⭐",reply_markup=InlineKeyboardMarkup(buttons))
            db.execute("UPDATE daily_game_challenges SET message_id=? WHERE chat_id=? AND challenge_date=?",(sent.message_id,chat_id,day)); db.commit()
        except Exception:
            db.execute("DELETE FROM daily_game_challenges WHERE chat_id=? AND challenge_date=? AND completed=0",(chat_id,day)); db.commit()
            await q.answer("تعذر نشر التحدي. تأكد أن البوت يستطيع إرسال الرسائل.",show_alert=True); return
        await q.edit_message_text("تم نشر تحدي اليوم في القروب 🎯",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_daily_answer:"):
        try: _,chat_raw,day,answer=data.split(":",3); chat_id=int(chat_raw)
        except ValueError: await q.answer("بيانات غير صحيحة.",show_alert=True); return
        row=db.execute("SELECT answer,completed FROM daily_game_challenges WHERE chat_id=? AND challenge_date=?",(chat_id,day)).fetchone()
        if not row or row[1]: await q.answer("انتهى تحدي اليوم أو فاز به عضو آخر.",show_alert=True); return
        if answer != row[0]: await q.answer("إجابة غير صحيحة، حاول مرة أخرى.",show_alert=True); return
        db.execute("UPDATE daily_game_challenges SET completed=1,winner_id=? WHERE chat_id=? AND challenge_date=? AND completed=0",(uid,chat_id,day))
        if db.execute("SELECT changes()").fetchone()[0]!=1: db.commit(); await q.answer("سبق أن فاز عضو آخر.",show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+5,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id,uid,5,"daily"); db.commit()
        try: await context.bot.send_message(chat_id,f"🎉 مبروك {name}! فزت بتحدي اليوم وربحت 5 نقاط ⭐")
        except Exception: pass
        await q.answer("إجابة صحيحة! تم تسجيل نقاطك.",show_alert=True); return

    if data.startswith("game_xo_start:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح.",show_alert=True); return
        if not await can_access_group(context.bot,uid,chat_id): await q.answer("لا تملك صلاحية هذا القروب.",show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and not cfg[0]: await q.answer("الألعاب متوقفة في هذا القروب.",show_alert=True); return
        token=uuid.uuid4().hex[:12]
        db.execute("INSERT INTO game_xo(token,chat_id,player_x,board,turn,status,created_at) VALUES(?,?,?,'         ','X','waiting',?)",(token,chat_id,uid,now())); db.commit()
        try:
            sent=await context.bot.send_message(chat_id,f"⭕ تحدي إكس أو\nاللاعب الأول: {q.from_user.full_name} (❌)\nمن يرغب بالمنافسة يضغط الانضمام:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🙋 انضم كلاعب ⭕",callback_data=f"xo_join:{token}")]]))
            db.execute("UPDATE game_xo SET message_id=? WHERE token=?",(sent.message_id,token)); db.commit()
        except Exception:
            db.execute("DELETE FROM game_xo WHERE token=?",(token,)); db.commit(); await q.answer("تعذر إرسال اللعبة إلى القروب.",show_alert=True); return
        await q.edit_message_text("تم نشر تحدي إكس أو في القروب. ينتظر لاعبًا ثانيًا.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("xo_join:"):
        token=data.split(":",1)[1]; row=db.execute("SELECT chat_id,player_x,player_o,board,turn,status,message_id FROM game_xo WHERE token=?",(token,)).fetchone()
        if not row: await q.answer("اللعبة غير موجودة.",show_alert=True); return
        chat_id,px,po,board,turn,status,message_id=row
        if status!="waiting": await q.answer("اللعبة بدأت أو انتهت.",show_alert=True); return
        if uid==px: await q.answer("تحتاج لاعبًا ثانيًا.",show_alert=True); return
        db.execute("UPDATE game_xo SET player_o=?,status='playing' WHERE token=? AND status='waiting'",(uid,token)); db.commit()
        text=f"⭕ إكس أو — ❌ {px} ضد ⭕ {uid}\nالدور: ❌ اللاعب الأول"
        try: await q.edit_message_text(text,reply_markup=game_board_markup(token,board))
        except Exception: await q.answer("تعذر تحديث لوحة اللعبة.",show_alert=True); return
        return

    if data.startswith("xo_move:"):
        try: _,token,pos_raw=data.split(":",2); pos=int(pos_raw)
        except ValueError: await q.answer("حركة غير صحيحة.",show_alert=True); return
        row=db.execute("SELECT chat_id,player_x,player_o,board,turn,status,message_id FROM game_xo WHERE token=?",(token,)).fetchone()
        if not row: await q.answer("اللعبة غير موجودة.",show_alert=True); return
        chat_id,px,po,board,turn,status,message_id=row
        if status!="playing" or uid not in (px,po): await q.answer("لست لاعبًا في هذه الجولة.",show_alert=True); return
        if (turn=="X" and uid!=px) or (turn=="O" and uid!=po): await q.answer("ليس دورك الآن.",show_alert=True); return
        if pos<0 or pos>8 or board[pos]!=" ": await q.answer("هذا المربع غير متاح.",show_alert=True); return
        symbol=turn; board=board[:pos]+symbol+board[pos+1:]
        wins_x=((0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6))
        winner=symbol if any(all(board[i]==symbol for i in line) for line in wins_x) else None
        draw=not winner and " " not in board
        if winner or draw:
            db.execute("UPDATE game_xo SET board=?,status='done' WHERE token=?",(board,token))
            if winner:
                winner_id=px if winner=="X" else po; name=q.from_user.full_name or str(uid)
                db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,winner_id,name))
                db.execute("UPDATE game_scores SET points=points+3,wins=wins+1,display_name=CASE WHEN user_id=? THEN ? ELSE display_name END WHERE chat_id=? AND user_id=?",(winner_id,name,chat_id,winner_id)); record_game_points(chat_id,winner_id,3,"xo")
                text=f"🏆 فاز اللاعب {'❌' if winner=='X' else '⭕'}! حصل على 3 نقاط ⭐\n❌ {px} ضد ⭕ {po}"
            else: text=f"🤝 تعادل!\n❌ {px} ضد ⭕ {po}"
        else:
            next_turn="O" if turn=="X" else "X"; db.execute("UPDATE game_xo SET board=?,turn=? WHERE token=?",(board,next_turn))
            text=f"⭕ إكس أو — ❌ {px} ضد ⭕ {po}\nالدور: {'❌ اللاعب الأول' if next_turn=='X' else '⭕ اللاعب الثاني'}"
        db.commit()
        try: await q.edit_message_text(text,reply_markup=None if winner or draw else game_board_markup(token,board))
        except Exception: await q.answer("تم تسجيل الحركة، لكن تعذر تحديث اللوحة.",show_alert=True)
        return

    if data == "statistics":
        if not can_use_panel(uid): await q.edit_message_text("⛔ ليس لديك صلاحية الإحصائيات."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"📊 {title or cid}", callback_data=f"group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("📊 اختر قروبًا أنت عضو فيه:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("group:"):
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        chat_id = int(data.split(":")[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية الاطلاع على هذا القروب.", show_alert=True); return
        group = db.execute("SELECT title,added_at FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        total = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=?", (chat_id,)).fetchone()[0]
        deletes = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action LIKE '%delete%'", (chat_id,)).fetchone()[0]
        bans = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action='ban'", (chat_id,)).fetchone()[0]
        mutes = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action='mute'", (chat_id,)).fetchone()[0]
        members = db.execute("SELECT COUNT(*) FROM member_activity WHERE chat_id=?", (chat_id,)).fetchone()[0]
        messages = db.execute("SELECT COALESCE(SUM(message_count),0) FROM member_activity WHERE chat_id=?", (chat_id,)).fetchone()[0]
        active = db.execute("SELECT display_name,message_count,stars,custom_title FROM member_activity WHERE chat_id=? ORDER BY message_count DESC LIMIT 5", (chat_id,)).fetchall()
        s = get_settings(chat_id)
        try: actual_member_count = await context.bot.get_chat_member_count(chat_id)
        except Exception: actual_member_count = None
        welcome = db.execute("SELECT message FROM welcome_messages WHERE chat_id=?", (chat_id,)).fetchone()
        lines = ["📊 إحصائيات القروب", f"👥 الاسم: {group[0] if group else 'غير معروف'}", f"🆔 المعرّف: {chat_id}", f"🗓️ تاريخ التسجيل: {group[1] if group else 'غير معروف'}", "", f"👥 عدد أعضاء القروب: {actual_member_count if actual_member_count is not None else 'تعذر الحصول عليه'}", f"👤 الأعضاء الذين رصد البوت نشاطهم: {members}", f"💬 الرسائل المرصودة: {messages}", f"📋 العمليات المسجلة: {total}", f"🗑️ عمليات الحذف: {deletes}", f"🚫 عمليات الحظر: {bans}", f"🔇 عمليات التقييد المسجلة: {mutes}", f"👋 الترحيب: {'مفعّل' if welcome and welcome[0] else 'غير مفعّل'}", f"🛡️ قفل الروابط: {'مفعّل' if s.get('links') else 'غير مفعّل'}", "", "⭐ أكثر الأعضاء نشاطًا:"]
        lines.extend([f"• {n or 'عضو'} — {count} رسالة — ⭐ {stars}" for n,count,stars,title in active] or ["لا توجد بيانات نشاط كافية بعد."])
        await q.edit_message_text("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ القروبات", callback_data="statistics")],
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
        ]))
        return

    if data.startswith("welcome_group:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True); return
        group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if not group:
            await q.edit_message_text("لم يتم العثور على القروب.")
            return
        current = db.execute("SELECT message FROM welcome_messages WHERE chat_id=?", (chat_id,)).fetchone()
        text = current[0] if current and current[0] else "غير مفعّل؛ لم تُحدّد رسالة ترحيب."
        await q.edit_message_text(
            f"👋 الترحيب — {group[0] or chat_id}\nمعرّف القروب: {chat_id}\n\nالرسالة الحالية:\n{text}\n\nاضغط تعيين رسالة جديدة، ثم أرسلها للبوت في الخاص. استخدم {name} لاسم العضو.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تعيين رسالة جديدة", callback_data=f"welcome_set:{chat_id}")],
                [InlineKeyboardButton("🗑️ إيقاف الترحيب", callback_data=f"welcome_off:{chat_id}")],
                [InlineKeyboardButton("⬅️ القروبات", callback_data="welcome")]
            ])
        )
        return

    if data.startswith("welcome_set:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("ليس لديك صلاحية هذا القروب", show_alert=True); return
        context.user_data["awaiting_welcome_chat"] = chat_id
        await q.edit_message_text("أرسل رسالة الترحيب الجديدة في الخاص مع البوت. استخدم {name} لاسم العضو و {group} لاسم القروب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data=f"welcome_group:{chat_id}")]]))
        return

    if data.startswith("welcome_off:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("ليس لديك صلاحية هذا القروب", show_alert=True); return
        db.execute("DELETE FROM welcome_messages WHERE chat_id=?", (chat_id,))
        db.commit()
        log_action(chat_id, uid, "welcome_off", "تم إيقاف الترحيب")
        await q.edit_message_text("✅ تم إيقاف رسالة الترحيب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القروبات", callback_data="welcome")]]))
        return

    if data == "roles_groups":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await q.answer("⛔ لا تملك صلاحية توزيع الرتب.", show_alert=True)
            return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"👥 {title or cid}", callback_data=f"roles_group:{cid}")] for cid, title in groups[:30]]
        if not rows:
            rows = [[InlineKeyboardButton("لا توجد قروبات متاحة لك", callback_data="noop")]]
        rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
        await q.edit_message_text("👥 توزيع الرتب الإدارية\n\nاختر القروب. بعد ذلك استخدم أوامر الرفع بالرد على رسالة العضو داخل القروب. الرتب داخل البوت ولا تغيّر رتبة تيليجرام.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("roles_group:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("معرّف القروب غير صحيح.", show_alert=True)
            return
        if not await can_access_group(context.bot, uid, chat_id):
            await q.answer("لا تملك صلاحية هذا القروب.", show_alert=True)
            return
        group_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = group_row[0] if group_row else str(chat_id)
        await q.edit_message_text(
            f"👥 إدارة الرتب — {title}\n\n"
            "بالقروب المحدد، رد على رسالة العضو بأحد الأوامر التالية:\n"
            "• رفع مشرف\n• رفع مشرف عام\n• رفع مدير\n• رفع مدير عام\n• تنزيل رتبة\n\n"
            "🛡️ تُمنح الصلاحيات المعرّفة لكل رتبة تلقائيًا داخل البوت.\n"
            "🔒 لا يمكن ترقية المالك الأساسي أو المالك المفوّض، ولا تتغير صلاحيات تيليجرام الأصلية تلقائيًا.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القروبات", callback_data="roles_groups")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "permissions":
        if not is_owner(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الصلاحيات.")
            return
        d = db.execute("SELECT COUNT(*) FROM delegated_owners").fetchone()[0]
        m = db.execute("SELECT COUNT(*) FROM managers").fetchone()[0]
        p = db.execute("SELECT COUNT(*) FROM protection_managers").fetchone()[0]
        text = (
            "👑 الصلاحيات\n\n"
            f"👑 المالك الأساسي: {OWNER_ID}\n"
            f"👑 المالكون المفوضون: {d}\n"
            f"🔨 المدراء العامون: {m}\n"
            f"🛡️ مدراء الحماية: {p}"
        )
        if is_owner(uid):
            text += "\n\nأوامر المالك:\n/addowner\n/delowner\n/addmanager\n/delmanager\n/addprotect\n/delprotect"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 المالكون المفوضون", callback_data="delegated_owner")],
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
        ]))
        return

    if data == "broadcast_start":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await q.answer("هذا القسم للمالك الأساسي أو المالك المفوّض.", show_alert=True)
            return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        allowed = [(cid, title) for cid, title in groups if await can_publish_to_live(context.bot, uid, cid)]
        if not allowed:
            await q.edit_message_text("⛔ لا توجد قروبات ممنوحة لك صلاحية النشر فيها. اطلب من المالك الأساسي منحك الصلاحية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
            return
        # Start with no groups selected to prevent accidental mass publication.
        context.user_data["broadcast_selected_groups"] = []
        rows = []
        for cid, title in allowed[:30]:
            selected = cid in context.user_data["broadcast_selected_groups"]
            mark = "✅" if selected else "⬜️"
            rows.append([InlineKeyboardButton(f"{mark} {title or cid}", callback_data=f"broadcast_toggle:{cid}")])
        if is_owner(uid) or is_delegated_owner(uid):
            rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات المتاحة لي", callback_data="broadcast_select_all")])
        rows.append([InlineKeyboardButton("➡️ متابعة وإرسال الإعلان", callback_data="broadcast_prepare")])
        rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")])
        await q.edit_message_text("📣 نشر إعلان بالقروبات\n\nحدد القروبات التي تريد النشر فيها. المالك المالك المفوّض يمكنه النشر إلى القروبات المسجلة التي يوجد فيها البوت.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("broadcast_toggle:"):
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await q.answer("ليس لديك صلاحية.", show_alert=True)
            return
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("معرّف القروب غير صحيح.", show_alert=True)
            return
        if not await can_publish_to_live(context.bot, uid, chat_id):
            await q.answer("النشر متاح فقط في القروبات المسجلة التي أنت عضو فيها.", show_alert=True)
            return
        selected = set(context.user_data.get("broadcast_selected_groups", []))
        if chat_id in selected: selected.remove(chat_id)
        else: selected.add(chat_id)
        context.user_data["broadcast_selected_groups"] = sorted(selected)
        # Refresh only authorized group options.
        groups = [(cid, title) for cid, title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if await can_publish_to_live(context.bot, uid, cid)]
        rows = [[InlineKeyboardButton(("✅ " if cid in selected else "⬜️ ") + (title or str(cid)), callback_data=f"broadcast_toggle:{cid}")] for cid, title in groups[:30]]
        if is_owner(uid) or is_delegated_owner(uid): rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات المتاحة لي", callback_data="broadcast_select_all")])
        rows += [[InlineKeyboardButton("➡️ متابعة وإرسال الإعلان", callback_data="broadcast_prepare")], [InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "broadcast_select_all":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await q.answer("ليس لديك صلاحية النشر.", show_alert=True)
            return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        allowed = [(cid, title) for cid, title in groups if await can_publish_to_live(context.bot, uid, cid)]
        context.user_data["broadcast_selected_groups"] = [cid for cid, _ in allowed]
        rows = [[InlineKeyboardButton("✅ " + (title or str(cid)), callback_data=f"broadcast_toggle:{cid}")] for cid, title in allowed[:30]]
        rows += [[InlineKeyboardButton("➡️ متابعة وإرسال الإعلان", callback_data="broadcast_prepare")], [InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "broadcast_prepare":
        selected = context.user_data.get("broadcast_selected_groups", [])
        selected = [cid for cid in selected if await can_publish_to_live(context.bot, uid, cid)]
        context.user_data["broadcast_selected_groups"] = selected
        if not selected:
            await q.answer("حدد قروبًا واحدًا على الأقل تملك صلاحية النشر فيه.", show_alert=True)
            return
        context.user_data["awaiting_broadcast"] = True
        await q.edit_message_text(
            "📣 أرسل الآن الإعلان في الخاص مع البوت (نص أو صورة أو فيديو أو ملف مع تعليق).\n"
            f"سيتم النشر في {len(selected)} قروب محدد فقط، بعد المعاينة والتأكيد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "broadcast_cancel":
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("broadcast_draft", None)
        context.user_data.pop("broadcast_selected_groups", None)
        await q.edit_message_text("تم إلغاء تجهيز الإعلان.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "broadcast_confirm":
        draft = context.user_data.get("broadcast_draft")
        selected = context.user_data.get("broadcast_selected_groups", [])
        selected = [cid for cid in selected if await can_publish_to_live(context.bot, uid, cid)]
        if not (is_owner(uid) or is_delegated_owner(uid)) or not draft or not selected:
            await q.edit_message_text("⛔ انتهت صلاحية الإعلان أو لا توجد قروبات مصرح بها. ابدأ من جديد.")
            return
        sent, failed = 0, 0
        for chat_id in selected:
            try:
                await context.bot.copy_message(chat_id=chat_id, from_chat_id=draft["chat_id"], message_id=draft["message_id"])
                sent += 1
            except Exception as e:
                failed += 1
                log.warning("Broadcast failed for %s: %s", chat_id, e)
        context.user_data.pop("broadcast_draft", None)
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("broadcast_selected_groups", None)
        await q.edit_message_text(
            f"✅ انتهى النشر.\n\nوصل إلى: {sent} قروب\nتعذر النشر في: {failed} قروب\n\nملاحظة: يجب أن يكون البوت موجودًا في القروب ولديه صلاحية إرسال الرسائل.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 نشر إعلان آخر", callback_data="broadcast_start")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "archive":
        if not is_owner(uid):
            await q.edit_message_text("⛔ سجل الرسائل خاص بالمالك الأساسي فقط.")
            return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        rows = [[InlineKeyboardButton(f"📨 {title or chat_id}", callback_data=f"archive_group:{chat_id}")] for chat_id, title in groups[:30]]
        if not rows:
            rows = [[InlineKeyboardButton("لا توجد مجموعات مسجلة", callback_data="noop")]]
        rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
        await q.edit_message_text("📨 سجل الرسائل الخاص\nاختر مجموعة لعرض آخر الرسائل المسجلة. الأرشفة متوقفة افتراضيًا، وتُفعّل داخل المجموعة بالأمر /archive_on بعد إعلان ذلك للأعضاء.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("archive_group:"):
        if not is_owner(uid):
            await q.answer("هذا السجل للمالك الأساسي فقط.", show_alert=True)
            return
        chat_id = int(data.split(":", 1)[1])
        rows = db.execute("SELECT chat_title,display_name,username,message_text,created_at FROM message_archive WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            text = "لا توجد رسائل مسجلة لهذه المجموعة. تأكد من تفعيل الأرشفة داخلها بالأمر /archive_on بعد إعلام الأعضاء."
        else:
            lines = ["📨 أحدث الرسائل المسجلة — للمالك الأساسي فقط"]
            for title, name, username, body, created in rows:
                lines.append(f"\n📍 {title or chat_id}\n👤 {name or 'عضو'} {('@'+username) if username else ''}\n🕒 {created}\n💬 {(body or '[رسالة غير نصية]')[:250]}")
            text = "\n".join(lines)[:3900]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ سجل الرسائل", callback_data="archive")],[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "delegated_owner":
        if not is_owner(uid):
            await q.edit_message_text("⛔ هذا القسم للمالك الأساسي فقط.")
            return
        rows = db.execute("SELECT user_id,display_name,username FROM delegated_owners ORDER BY user_id").fetchall()
        text = "👑 المالكون المفوّضون وصلاحيات النشر\n\n"
        text += "\n".join(f"• {name or 'اسم غير معروف'}" + (f" (@{username})" if username else "") + f" — ID: {user_id}" for user_id,name,username in rows) if rows else "لا يوجد مالكون مفوّضون."
        text += "\n\nاختر مالكًا مفوضًا لإدارة صلاحياته. الإضافة والعزل تتمان من هذه اللوحة في الخاص."
        buttons = [[InlineKeyboardButton(f"⚙️ {name or user_id} — الصلاحيات", callback_data=f"delegated_manage:{user_id}")] for user_id,name,_ in rows[:30]]
        buttons.insert(0, [InlineKeyboardButton("➕ إضافة مالك مفوّض", callback_data="delegated_add_start")])
        buttons += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "delegated_add_start":
        if not is_owner(uid):
            await q.answer("إضافة المالك المفوّض للمالك الأساسي فقط.", show_alert=True)
            return
        if q.message.chat.type != ChatType.PRIVATE:
            await q.answer("افتح اللوحة في الخاص مع البوت.", show_alert=True)
            return
        context.user_data["awaiting_delegated_owner_id"] = True
        await q.edit_message_text(
            "➕ إضافة مالك مفوّض\n\nأرسل رقم المستخدم (User ID) في هذه المحادثة الخاصة.\nلن تُمنح الصلاحية إلا بعد إدخال الرقم هنا، ولا ترسل رمز البوت أو التوكن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء والعودة", callback_data="delegated_owner")]])
        )
        return

    if data.startswith("delegated_manage:"):
        if not is_owner(uid):
            await q.answer("إدارة صلاحيات المالك المفوض للمالك الأساسي فقط.", show_alert=True)
            return
        try: target_uid = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("رقم المستخدم غير صحيح.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await q.edit_message_text("هذا المالك المفوض لم يعد مسجلًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المالكون المفوضون", callback_data="delegated_owner")]])); return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        all_granted = bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0", (target_uid,)).fetchone())
        buttons = [[InlineKeyboardButton(("🌐 سحب صلاحية جميع القروبات" if all_granted else "🌐 منح صلاحية جميع القروبات"), callback_data=f"delegated_all:{target_uid}:{0 if all_granted else 1}")]]
        for chat_id,title in groups[:30]:
            granted = all_granted or bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=?", (target_uid,chat_id)).fetchone())
            buttons.append([InlineKeyboardButton(("✅ " if granted else "⬜️ ") + (title or str(chat_id)), callback_data=f"pubperm_toggle:{target_uid}:{chat_id}")])
        buttons += [[InlineKeyboardButton("🛑 عزل هذا المالك المفوض", callback_data=f"delegated_remove_confirm:{target_uid}")], [InlineKeyboardButton("⬅️ قائمة المالكين", callback_data="delegated_owner")]]
        await q.edit_message_text(f"👑 إدارة صلاحيات النشر\nالمالك المفوض: {user_display_name(target_uid)}\n\n✅ = لديه صلاحية النشر في القروب\n⬜️ = لا يملك صلاحية النشر", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("pubperm_toggle:"):
        if not is_owner(uid):
            await q.answer("هذا الإجراء للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, chat_raw = data.split(":", 2); target_uid=int(target_raw); chat_id=int(chat_raw)
        except ValueError:
            await q.answer("بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await q.answer("المالك المفوض غير مسجل.", show_alert=True); return
        group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if not group:
            await q.answer("القروب غير مسجل.", show_alert=True); return
        all_granted = db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0", (target_uid,)).fetchone()
        exists = db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=?", (target_uid,chat_id)).fetchone()
        if all_granted:
            # Convert wildcard to explicit grants for every registered group except this one.
            db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
            for other_chat_id, in db.execute("SELECT chat_id FROM watched_groups WHERE chat_id!=?", (chat_id,)).fetchall():
                db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,?,?,?)", (target_uid,other_chat_id,uid,now()))
            action = "تم سحب صلاحية النشر"
        elif exists:
            db.execute("DELETE FROM publish_permissions WHERE user_id=? AND chat_id=?", (target_uid,chat_id))
            action = "تم سحب صلاحية النشر"
        else:
            db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,?,?,?)", (target_uid,chat_id,uid,now()))
            action = "تم منح صلاحية النشر"
        db.commit()
        await q.answer(action)
        # Re-render permissions page.
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        all_granted = bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0", (target_uid,)).fetchone())
        buttons = [[InlineKeyboardButton(("🌐 سحب صلاحية جميع القروبات" if all_granted else "🌐 منح صلاحية جميع القروبات"), callback_data=f"delegated_all:{target_uid}:{0 if all_granted else 1}")]]
        for cid,title in groups[:30]:
            granted = all_granted or bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=?", (target_uid,cid)).fetchone())
            buttons.append([InlineKeyboardButton(("✅ " if granted else "⬜️ ") + (title or str(cid)), callback_data=f"pubperm_toggle:{target_uid}:{cid}")])
        buttons += [[InlineKeyboardButton("🛑 عزل هذا المالك المفوض", callback_data=f"delegated_remove_confirm:{target_uid}")], [InlineKeyboardButton("⬅️ قائمة المالكين", callback_data="delegated_owner")]]
        await q.edit_message_text(f"👑 إدارة صلاحيات النشر\nالمالك المفوض: {user_display_name(target_uid)}\n\n{action} للقروب: {group[0] or chat_id}\n✅ = لديه صلاحية النشر\n⬜️ = لا يملكها", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("delegated_all:"):
        if not is_owner(uid):
            await q.answer("هذا الإجراء للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, enable_raw = data.split(":", 2); target_uid=int(target_raw); enable=int(enable_raw)
        except ValueError:
            await q.answer("بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await q.answer("المالك المفوض غير مسجل.", show_alert=True); return
        if enable:
            db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,0,?,?)", (target_uid,uid,now()))
        else:
            db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
        db.commit()
        await q.answer("تم تحديث الصلاحية.")
        # Reuse the management view by re-entering it.
        await q.edit_message_text("تم تحديث الصلاحيات. افتح إدارة هذا المالك المفوض مجددًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↻ تحديث الصلاحيات", callback_data=f"delegated_manage:{target_uid}")], [InlineKeyboardButton("⬅️ المالكون المفوضون", callback_data="delegated_owner")]]))
        return

    if data.startswith("delegated_remove_confirm:"):
        if not is_owner(uid):
            await q.answer("عزل المالك المفوض للمالك الأساسي فقط.", show_alert=True); return
        try: target_uid=int(data.split(":",1)[1])
        except ValueError:
            await q.answer("رقم المستخدم غير صحيح.", show_alert=True); return
        await q.edit_message_text(f"⚠️ هل تريد عزل {user_display_name(target_uid)} وإلغاء كل صلاحياته؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نعم، عزله", callback_data=f"delegated_remove:{target_uid}"), InlineKeyboardButton("لا، رجوع", callback_data=f"delegated_manage:{target_uid}")]]))
        return

    if data.startswith("delegated_remove:"):
        if not is_owner(uid):
            await q.answer("عزل المالك المفوض للمالك الأساسي فقط.", show_alert=True); return
        try: target_uid=int(data.split(":",1)[1])
        except ValueError:
            await q.answer("رقم المستخدم غير صحيح.", show_alert=True); return
        name = user_display_name(target_uid)
        db.execute("DELETE FROM delegated_owners WHERE user_id=?", (target_uid,))
        db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
        db.commit()
        await q.edit_message_text(f"✅ تم عزل {name} وإلغاء جميع صلاحياته وتفويضات النشر.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المالكون المفوضون", callback_data="delegated_owner")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "managers":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
        text = "👥 المدراء العامون:\n\n" + ("\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مدراء.")
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "alerts":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        groups = await accessible_groups(context.bot, uid)
        text = "📢 التنبيهات والمستجدات\n\n"
        text += "\n".join(f"• {title or chat_id}" for chat_id, title in groups) if groups else "لا توجد قروبات متابعة بعد."
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "logs":
        if not can_use_panel(uid): await q.edit_message_text("⛔ ليس لديك صلاحية."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"📋 {title or cid}", callback_data=f"logs_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("📋 السجلات والدخول والخروج\nاختر قروبًا لعرض آخر العمليات والأحداث التي أبلغ تيليجرام البوت بها.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("logs_group:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await q.answer("معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب", show_alert=True); return
        rows = db.execute("SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 25", (chat_id,)).fetchall()
        title = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        body = "\n".join(f"• {created} — {action}: {details or ''}" for action,details,created in rows) or "لا توجد عمليات مسجلة بعد."
        await q.edit_message_text(f"📋 سجل القروب: {title[0] if title else chat_id}\n\n{body}"[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ السجلات", callback_data="logs")],[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data == "settings":
        if not (is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإعدادات."); return
        if q.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            chat_id = q.message.chat.id
            if not await can_access_group(context.bot, uid, chat_id): await q.answer("لا تملك صلاحية هذا القروب", show_alert=True); return
            await q.edit_message_text(settings_text(0), reply_markup=settings_page_markup(chat_id, 0)); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"⚙️ {title or cid}", callback_data=f"settings_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("⚙️ اختر القروب الذي تريد إدارة إعداداته:", reply_markup=InlineKeyboardMarkup(rows)); return

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
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables.")
    app = Application.builder().token(TOKEN).build()

    command_handlers = {
        "start": start, "help": help_cmd, "panel": panel, "report": report_cmd,
        "addowner": addowner, "delowner": delowner, "owners": owners_cmd,
        "grantpublish": grantpublish, "revokepublish": revokepublish,
        "addprotect": addprotect, "delprotect": delprotect, "permissions": permissions_cmd,
        "addmanager": addmanager, "delmanager": delmanager, "managers": managers,
        "id": id_cmd, "idgroup": idgroup, "ban": ban, "unban": unban,
        "kick": kick, "mute": mute, "unmute": unmute, "del": del_cmd, "pin": pin,
        "alerts": alerts, "logs": logs_cmd, "settings": settings_cmd, "locks": locks,
        "archive": archive_cmd, "archive_on": archive_on, "archive_off": archive_off,
        "autoreplies": auto_replies_list, "autoreplies_on": auto_replies_on,
        "autoreplies_off": auto_replies_off, "addreply": add_auto_reply, "delreply": delete_auto_reply
    }
    for name, fn in command_handlers.items():
        app.add_handler(CommandHandler(name, fn))

    for cmd in LOCK_MAP:
        app.add_handler(CommandHandler(cmd, set_lock))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, broadcast_draft_message), group=-1)
    app.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members), group=1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, ArabicTextCommand), group=2)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_filter), group=3)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_filter), group=4)
    async def error_handler(update, context):
        # python-telegram-bot awaits error callbacks; keep this handler async.
        log.error("Update error: %s", context.error, exc_info=context.error)

    app.add_error_handler(error_handler)

    threading.Thread(target=start_health_server, daemon=True).start()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
