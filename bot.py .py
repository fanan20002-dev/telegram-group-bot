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
db.execute("""
CREATE TABLE IF NOT EXISTS delegated_owners(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS protection_managers(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
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

def is_delegated_owner(uid):
    return is_owner(uid) or bool(db.execute(
        "SELECT 1 FROM delegated_owners WHERE user_id=?", (uid,)
    ).fetchone())

def is_general_manager(uid):
    return is_delegated_owner(uid) or bool(db.execute(
        "SELECT 1 FROM managers WHERE user_id=?", (uid,)
    ).fetchone())

def is_protection_manager(uid):
    return is_delegated_owner(uid) or bool(db.execute(
        "SELECT 1 FROM protection_managers WHERE user_id=?", (uid,)
    ).fetchone())

def is_manager(uid):
    return is_general_manager(uid)

def can_use_panel(uid):
    return is_general_manager(uid) or is_protection_manager(uid)

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

async def require_admin(update):
    if not update.effective_user or not update.effective_chat:
        return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل القروب فقط.")
        return False
    if not is_general_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية الإدارة.")
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
    rows = [
        [InlineKeyboardButton("🛡️ الحماية", callback_data="security"),
         InlineKeyboardButton("🔨 الإدارة", callback_data="administration")],
        [InlineKeyboardButton("👋 الترحيب", callback_data="welcome"),
         InlineKeyboardButton("🎮 الألعاب", callback_data="games")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="statistics"),
         InlineKeyboardButton("👑 الصلاحيات", callback_data="permissions")],
        [InlineKeyboardButton("📋 السجلات", callback_data="logs"),
         InlineKeyboardButton("📢 التنبيهات", callback_data="alerts")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]
    ]
    if is_owner(uid):
        rows.append([InlineKeyboardButton("👑 المالك المفوّض", callback_data="delegated_owner")])
    return InlineKeyboardMarkup(rows)

def settings_page_markup(chat_id, page=0):
    items = [
        ("الروابط", "links"), ("الكلايش", "username"), ("الكيبورد", "keyboard"),
        ("الأغاني", "audio"), ("المتحركة", "gif"), ("الملفات", "files"),
        ("الدردشة", "repeat"), ("الفيديو", "videos"), ("الصور", "photos"),
        ("المعرفات", "username"), ("التاك", "tag"), ("البوتات", "bots"),
        ("الألعاب", "games"), ("الملصقات", "stickers"), ("التعديل", "edit"),
        ("رسائل الدخول", "entry"), ("الإضافة", "add_lock"),
        ("الإشعارات", "notifications"), ("الماركداون", "markdown"),
        ("الدخول", "join_lock")
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
            InlineKeyboardButton(state, callback_data=f"toggle:{field}:{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("السابق", callback_data=f"settings_page:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("التالي", callback_data=f"settings_page:{page+1}"))
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

async def start(update, context):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"مرحبًا 👋\nرقم حسابك: {update.effective_user.id}\n\n"
            "استخدم /panel لفتح لوحة التحكم."
        )
    else:
        await update.effective_message.reply_text("تم تشغيل البوت ✅\nاستخدم /panel للوحة التحكم.")

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "📚 دليل البوت\n\n"
        "👑 /panel — لوحة التحكم\n"
        "👑 /addowner — مالك مفوّض (للمالك الأساسي)\n"
        "🔨 /addmanager — مدير عام\n"
        "🛡️ /addprotect — مدير حماية\n"
        "👥 /managers — المدراء العامون\n"
        "📊 الإحصائيات والتنبيهات من لوحة التحكم\n\n"
        "أوامر الحماية العربية تعمل كرسائل عادية، مثل:\n"
        "منع الروابط\nمنع الصور\nمنع الفيديو\nمنع الملفات\nالسماح بالروابط"
    )

async def panel(update, context):
    if not can_use_panel(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية لوحة الإدارة.")
        return
    if update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        register_group(update.effective_chat)
    await update.effective_message.reply_text(
        "👑 لوحة التحكم الرئيسية\n\n"
        f"الصلاحية: {role_name(update.effective_user.id)}\n\n"
        "اختر القسم المطلوب:",
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
    db.execute("INSERT OR REPLACE INTO delegated_owners(user_id,added_by,added_at) VALUES(?,?,?)",
               (uid, update.effective_user.id, now()))
    db.commit()
    await update.effective_message.reply_text(
        f"👑 تم منح العضو صلاحية «مالك مفوّض» ({uid}).\n"
        "ملكية البوت الأساسية تبقى عندك."
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
    db.execute("DELETE FROM delegated_owners WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم إلغاء المالك المفوّض: {uid}")

async def owners_cmd(update, context):
    if not can_use_panel(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    rows = db.execute("SELECT user_id FROM delegated_owners ORDER BY user_id").fetchall()
    text = "👑 المالكون المفوّضون:\n\n"
    text += "\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مالكون مفوّضون."
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
    if not can_use_panel(update.effective_user.id):
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
    if not await require_admin(update): return
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
    if not await require_admin(update): return
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
    if not await require_admin(update): return
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
    if not await require_admin(update): return
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
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("استخدم /del بالرد على الرسالة المراد حذفها.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الحذف: {e}")

async def pin(update, context):
    if not await require_admin(update): return
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
    if not await require_admin(update): return
    if field is None:
        cmd = (update.message.text or "").split()[0].lower().lstrip("/")
        field = LOCK_MAP.get(cmd)
        value = 0 if cmd.startswith("unlock") else 1
    set_setting(update.effective_chat.id, field, value)
    state = "🔒 تم المنع." if value else "🔓 تم السماح."
    await update.effective_message.reply_text(state)
    log_action(update.effective_chat.id, update.effective_user.id, "setting", f"{field}={value}")

async def locks(update, context):
    if not can_manage_security(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية الحماية.")
        return
    register_group(update.effective_chat)
    await update.effective_message.reply_text(
        settings_text(0),
        reply_markup=settings_page_markup(update.effective_chat.id, 0)
    )

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
    register_group(update.effective_chat)
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
    uid = q.from_user.id
    data = q.data or ""

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

    if data.startswith("settings_page:"):
        if not can_manage_security(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإعدادات.")
            return
        page = int(data.split(":")[1])
        chat_id = q.message.chat.id
        await q.edit_message_text(
            settings_text(page),
            reply_markup=settings_page_markup(chat_id, page)
        )
        return

    if data.startswith("toggle:"):
        if not can_manage_security(uid):
            await q.answer("ليس لديك صلاحية تغيير الإعداد.", show_alert=True)
            return
        _, field, page = data.split(":")
        if field not in SETTING_FIELDS:
            return
        chat_id = q.message.chat.id
        s = get_settings(chat_id)
        new_value = 0 if s[field] else 1
        set_setting(chat_id, field, new_value)
        log_action(chat_id, uid, "setting", f"{field}={new_value}")
        await q.edit_message_text(
            settings_text(int(page)),
            reply_markup=settings_page_markup(chat_id, int(page))
        )
        return

    if data == "security":
        if not can_manage_security(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الحماية.")
            return
        await q.edit_message_text(
            "🛡️ الحماية\n\n"
            "جميع إعدادات المنع والسماح موجودة في «⚙️ الإعدادات».\n\n"
            "أمثلة عربية:\nمنع الروابط\nمنع الصور\nمنع الفيديو\nمنع الملفات",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ إعدادات", callback_data="settings")],
                [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
            ])
        )
        return

    if data == "administration":
        if not is_general_manager(uid):
            await q.edit_message_text("⛔ هذا القسم للمدير العام والمالك.")
            return
        await q.edit_message_text(
            "🔨 الإدارة\n\n"
            "/ban — حظر\n/unban — فك الحظر\n/kick — طرد\n"
            "/mute — كتم\n/unmute — فك الكتم\n/del — حذف\n/pin — تثبيت",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "welcome":
        if not is_general_manager(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الترحيب.")
            return
        await q.edit_message_text(
            "👋 الترحيب\n\nيمكن تخصيص رسالة ترحيب لكل قروب من إعدادات الرسائل.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "games":
        if not is_general_manager(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الألعاب.")
            return
        await q.edit_message_text(
            "🎮 الألعاب\n\nإدارة الألعاب ومنعها متاحة من إعدادات المجموعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "statistics":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإحصائيات.")
            return
        await q.edit_message_text("📊 اختر القروب:", reply_markup=group_list_markup())
        return

    if data.startswith("group:"):
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        chat_id = int(data.split(":")[1])
        total = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=?", (chat_id,)).fetchone()[0]
        deletes = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action='delete'", (chat_id,)).fetchone()[0]
        bans = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action='ban'", (chat_id,)).fetchone()[0]
        await q.edit_message_text(
            "📊 إحصائيات القروب\n\n"
            f"العمليات المسجلة: {total}\nالحذف: {deletes}\nالحظر: {bans}\n"
            f"رقم القروب: {chat_id}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ القروبات", callback_data="statistics")],
                [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
            ])
        )
        return

    if data == "permissions":
        if not can_use_panel(uid):
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

    if data == "delegated_owner":
        if not is_owner(uid):
            await q.edit_message_text("⛔ هذا القسم للمالك الأساسي فقط.")
            return
        rows = db.execute("SELECT user_id FROM delegated_owners ORDER BY user_id").fetchall()
        text = "👑 المالكون المفوضون\n\n"
        text += "\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مالكون مفوضون."
        text += "\n\nإضافة: /addowner\nحذف: /delowner"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
        ]))
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
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        text = "📢 التنبيهات والمستجدات\n\n"
        text += "\n".join(f"• {title or chat_id}" for chat_id, title in groups) if groups else "لا توجد قروبات متابعة بعد."
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data == "logs":
        if not can_use_panel(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية.")
            return
        await q.edit_message_text(
            "📋 السجلات\n\nاستخدم /logs داخل القروب لعرض آخر العمليات.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
        )
        return

    if data == "settings":
        if not can_manage_security(uid):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإعدادات.")
            return
        if q.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await q.edit_message_text("⚙️ افتح /panel داخل القروب ثم ادخل الإعدادات.")
            return
        await q.edit_message_text(
            settings_text(0),
            reply_markup=settings_page_markup(q.message.chat.id, 0)
        )
        return

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
        raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables.")
    app = Application.builder().token(TOKEN).build()

    command_handlers = {
        "start": start, "help": help_cmd, "panel": panel,
        "addowner": addowner, "delowner": delowner, "owners": owners_cmd,
        "addprotect": addprotect, "delprotect": delprotect, "permissions": permissions_cmd,
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
