import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "bot.db")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing")

DB_LOCK = threading.Lock()

LOCKS = {
    "links": "الروابط",
    "photo": "الصور",
    "video": "الفيديو",
    "audio": "الصوت",
    "file": "الملفات",
    "stickers": "الملصقات",
    "gif": "GIF",
    "username": "المعرفات",
    "tag": "المنشن",
    "bots": "البوتات",
    "keyboard": "لوحة المفاتيح",
    "games": "الألعاب",
    "repeat": "التكرار",
    "join_lock": "إشعارات الدخول",
    "entry": "الدخول",
    "add_lock": "إضافة الأعضاء",
    "edit": "تعديل الرسائل",
    "forward": "إعادة التوجيه",
}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with DB_LOCK:
        conn = db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS managers (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS protection_managers (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            welcome TEXT DEFAULT '',
            welcome_enabled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, key)
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            action TEXT,
            target_id INTEGER,
            details TEXT,
            created_at TEXT
        );
        """)
        conn.commit()
        conn.close()

def execute(sql, params=(), fetch=False, many=False):
    with DB_LOCK:
        conn = db()
        cur = conn.cursor()
        if many:
            cur.executemany(sql, params)
        else:
            cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        conn.close()
        return rows

def log_action(chat_id, user_id, action, target_id=None, details=""):
    execute(
        "INSERT INTO logs(chat_id,user_id,action,target_id,details,created_at) VALUES(?,?,?,?,?,?)",
        (chat_id, user_id, action, target_id, details, now())
    )

def ensure_group(chat):
    execute(
        "INSERT OR IGNORE INTO groups(chat_id,title) VALUES(?,?)",
        (chat.id, chat.title or "")
    )

def is_owner(uid):
    return uid == OWNER_ID

def is_manager(uid):
    if is_owner(uid):
        return True
    rows = execute("SELECT 1 FROM managers WHERE user_id=?", (uid,), True)
    return bool(rows)

def is_protection_manager(uid):
    if is_owner(uid):
        return True
    rows = execute("SELECT 1 FROM protection_managers WHERE user_id=?", (uid,), True)
    return bool(rows)

async def is_group_admin(update, context):
    if not update.effective_chat or update.effective_chat.type == "private":
        return False
    uid = update.effective_user.id
    if is_owner(uid) or is_manager(uid):
        return True
    try:
        m = await context.bot.get_chat_member(update.effective_chat.id, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def require_manager(update, context):
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك والمدير العام فقط.")
        return False
    return True

async def require_protection(update, context):
    if not is_protection_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك ومدير الحماية فقط.")
        return False
    return True

def set_setting(chat_id, key, value):
    execute(
        "INSERT INTO settings(chat_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
        (chat_id, key, int(value))
    )

def get_setting(chat_id, key):
    rows = execute(
        "SELECT value FROM settings WHERE chat_id=? AND key=?",
        (chat_id, key), True
    )
    return bool(rows and rows[0]["value"])

def target_from_message(update):
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None

async def resolve_target(update, context):
    target = target_from_message(update)
    if target:
        return target
    if context.args:
        arg = context.args[0]
        if arg.startswith("@"):
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, arg)
                return member.user
            except Exception:
                return None
        if arg.lstrip("-").isdigit():
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, int(arg))
                return member.user
            except Exception:
                return None
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "✅ تم تشغيل البوت\n\n"
        "استخدم /panel للوحة التحكم.\n"
        "اكتب «المساعدة» لعرض الأوامر العربية."
    )

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "📚 أوامر البوت\n\n"
        "👑 الإدارة العامة:\n"
        "/panel — لوحة التحكم\n"
        "/addmanager — إضافة مدير عام (بالرد على العضو)\n"
        "/delmanager — حذف مدير عام\n"
        "/managers — قائمة المديرين\n\n"
        "🛡️ مديرو الحماية:\n"
        "/addprotect — إضافة مدير حماية (بالرد على العضو)\n"
        "/delprotect — حذف مدير حماية\n"
        "/protectors — قائمة مديري الحماية\n\n"
        "⚙️ الحماية:\n"
        "/locks — حالة الحماية\n"
        "/settings — الإعدادات\n"
        "منع الروابط / منع الصور / منع الفيديو / منع الملفات\n\n"
        "🔨 الإدارة:\n"
        "/ban /unban /kick /mute /unmute /del /pin\n\n"
        "📊 المتابعة:\n"
        "/logs /alerts"
    )

async def panel(update, context):
    uid = update.effective_user.id
    if not (is_owner(uid) or is_manager(uid) or is_protection_manager(uid)):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية فتح اللوحة.")
        return

    buttons = [
        [InlineKeyboardButton("🛡️ الحماية", callback_data="panel_protection")],
        [InlineKeyboardButton("🔨 الإدارة", callback_data="panel_moderation")],
        [InlineKeyboardButton("📋 حالة الحماية", callback_data="panel_locks")],
    ]
    if is_owner(uid) or is_manager(uid):
        buttons.append([InlineKeyboardButton("👑 المديرون", callback_data="panel_managers")])
        buttons.append([InlineKeyboardButton("📊 السجلات", callback_data="panel_logs")])
        buttons.append([InlineKeyboardButton("⚙️ الإعدادات", callback_data="panel_settings")])

    await update.effective_message.reply_text(
        "🎛️ لوحة تحكم البوت\n\nاختر القسم:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def add_manager(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو:\n/addmanager")
        return
    if target.id == OWNER_ID:
        await update.effective_message.reply_text("ℹ️ هذا هو المالك.")
        return
    execute(
        "INSERT OR REPLACE INTO managers(user_id,added_by,added_at) VALUES(?,?,?)",
        (target.id, update.effective_user.id, now())
    )
    log_action(update.effective_chat.id, update.effective_user.id, "add_manager", target.id)
    await update.effective_message.reply_text(f"✅ تم تعيين {target.mention_html()} مديرًا عامًا.", parse_mode="HTML")

async def del_manager(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو:\n/delmanager")
        return
    execute("DELETE FROM managers WHERE user_id=?", (target.id,))
    await update.effective_message.reply_text("✅ تم حذف المدير العام.")

async def managers(update, context):
    if not await require_manager(update, context):
        return
    rows = execute("SELECT user_id,added_at FROM managers ORDER BY added_at", fetch=True)
    if not rows:
        await update.effective_message.reply_text("لا يوجد مديرون عامون حاليًا.")
        return
    lines = ["👑 المديرون العامون:"]
    for r in rows:
        lines.append(f"• <code>{r['user_id']}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def add_protect(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ إضافة مديري الحماية للمالك فقط.")
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text(
            "⚠️ استخدم الأمر بالرد على العضو:\n/addprotect"
        )
        return
    if target.id == OWNER_ID:
        await update.effective_message.reply_text("ℹ️ المالك لديه صلاحيات الحماية تلقائيًا.")
        return
    execute(
        "INSERT OR REPLACE INTO protection_managers(user_id,added_by,added_at) VALUES(?,?,?)",
        (target.id, update.effective_user.id, now())
    )
    log_action(update.effective_chat.id, update.effective_user.id, "add_protection_manager", target.id)
    await update.effective_message.reply_text(
        f"🛡️ تم تعيين {target.mention_html()} مدير حماية.\n"
        "يمكنه التحكم بإعدادات الحماية فقط.",
        parse_mode="HTML"
    )

async def del_protect(update, context):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ حذف مديري الحماية للمالك فقط.")
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو:\n/delprotect")
        return
    execute("DELETE FROM protection_managers WHERE user_id=?", (target.id,))
    await update.effective_message.reply_text("✅ تم حذف مدير الحماية.")

async def protectors(update, context):
    if not (is_owner(update.effective_user.id) or is_manager(update.effective_user.id)):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك والمدير العام.")
        return
    rows = execute("SELECT user_id FROM protection_managers ORDER BY added_at", fetch=True)
    if not rows:
        await update.effective_message.reply_text("لا يوجد مديرو حماية حاليًا.")
        return
    text = "🛡️ مديرو الحماية:\n" + "\n".join(f"• <code>{r['user_id']}</code>" for r in rows)
    await update.effective_message.reply_text(text, parse_mode="HTML")

async def set_lock(update, context, key, enabled):
    if not await require_protection(update, context):
        return
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    set_setting(update.effective_chat.id, key, enabled)
    state = "🔒 تم المنع." if enabled else "🔓 تم السماح."
    log_action(update.effective_chat.id, update.effective_user.id, f"{key}:{enabled}")
    await update.effective_message.reply_text(state)

def make_lock_handler(key, enabled):
    async def handler(update, context):
        await set_lock(update, context, key, enabled)
    return handler

async def locks(update, context):
    if not await require_protection(update, context):
        return
    chat_id = update.effective_chat.id
    lines = ["🔐 حالة الحماية:"]
    for key, label in LOCKS.items():
        lines.append(f"• {label}: {'🔒' if get_setting(chat_id,key) else '🔓'}")
    await update.effective_message.reply_text("\n".join(lines))

async def settings_cmd(update, context):
    if not (is_owner(update.effective_user.id) or is_manager(update.effective_user.id)):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك والمدير العام.")
        return
    await update.effective_message.reply_text(
        "⚙️ الإعدادات\n"
        "يمكنك استخدام /locks لمشاهدة حالة الحماية.\n"
        "مدير الحماية مسؤول عن أوامر المنع والسماح."
    )

async def ban(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم /ban بالرد على العضو.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        log_action(update.effective_chat.id, update.effective_user.id, "ban", target.id)
        await update.effective_message.reply_text("🚫 تم حظر العضو.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر الحظر: {e}")

async def unban(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم /unban بالرد على العضو.")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        await update.effective_message.reply_text("✅ تم إلغاء الحظر.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر إلغاء الحظر: {e}")

async def kick(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم /kick بالرد على العضو.")
        return
    try:
        chat_id = update.effective_chat.id
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)
        log_action(chat_id, update.effective_user.id, "kick", target.id)
        await update.effective_message.reply_text("👢 تم طرد العضو.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر الطرد: {e}")

async def mute(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم /mute بالرد على العضو.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False)
        )
        log_action(update.effective_chat.id, update.effective_user.id, "mute", target.id)
        await update.effective_message.reply_text("🔇 تم كتم العضو.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر الكتم: {e}")

async def unmute(update, context):
    if not await require_manager(update, context):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.effective_message.reply_text("⚠️ استخدم /unmute بالرد على العضو.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True
            )
        )
        await update.effective_message.reply_text("🔊 تم فك الكتم.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر فك الكتم: {e}")

async def delete_msg(update, context):
    if not await require_manager(update, context):
        return
    try:
        await update.effective_message.delete()
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر حذف الرسالة: {e}")

async def pin(update, context):
    if not await require_manager(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("⚠️ استخدم /pin بالرد على الرسالة المراد تثبيتها.")
        return
    try:
        await msg.reply_to_message.pin(disable_notification=True)
        await msg.reply_text("📌 تم تثبيت الرسالة.")
    except Exception as e:
        await msg.reply_text(f"❌ تعذر التثبيت: {e}")

async def alerts(update, context):
    if not (is_owner(update.effective_user.id) or is_manager(update.effective_user.id)):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك والمدير العام.")
        return
    await update.effective_message.reply_text(
        "🔔 التنبيهات مفعلة للسجلات الأساسية داخل قاعدة بيانات البوت."
    )

async def logs_cmd(update, context):
    if not (is_owner(update.effective_user.id) or is_manager(update.effective_user.id)):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك والمدير العام.")
        return
    rows = execute(
        "SELECT action,user_id,target_id,created_at FROM logs "
        "WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (update.effective_chat.id,), True
    )
    if not rows:
        await update.effective_message.reply_text("📋 لا توجد سجلات حتى الآن.")
        return
    lines = ["📋 آخر العمليات:"]
    for r in rows:
        lines.append(
            f"• {r['action']} | المنفذ: {r['user_id']} | الهدف: {r['target_id'] or '-'}"
        )
    await update.effective_message.reply_text("\n".join(lines))

ARABIC_LOCKS = {
    "منع الروابط": ("links", True), "السماح بالروابط": ("links", False),
    "منع الصور": ("photo", True), "السماح بالصور": ("photo", False),
    "منع الفيديو": ("video", True), "السماح بالفيديو": ("video", False),
    "منع الصوت": ("audio", True), "السماح بالصوت": ("audio", False),
    "منع الملفات": ("file", True), "السماح بالملفات": ("file", False),
    "منع الملصقات": ("stickers", True), "السماح بالملصقات": ("stickers", False),
    "منع gif": ("gif", True), "السماح gif": ("gif", False),
    "منع المنشن": ("tag", True), "السماح بالمنشن": ("tag", False),
    "منع البوتات": ("bots", True), "السماح بالبـوتات": ("bots", False),
    "منع التكرار": ("repeat", True), "السماح بالتكرار": ("repeat", False),
    "منع الإضافة": ("add_lock", True), "السماح بالإضافة": ("add_lock", False),
    "منع التعديل": ("edit", True), "السماح بالتعديل": ("edit", False),
    "منع إعادة التوجيه": ("forward", True), "السماح بإعادة التوجيه": ("forward", False),
}

async def arabic_text_command(update, context):
    text = (update.effective_message.text or "").strip()
    key_action = ARABIC_LOCKS.get(text)
    if not key_action:
        aliases = {
            "لوحة التحكم": panel,
            "المساعدة": help_cmd,
            "المدراء": managers,
            "مديرو الحماية": protectors,
        }
        fn = aliases.get(text)
        if fn:
            await fn(update, context)
        return
    key, enabled = key_action
    await set_lock(update, context, key, enabled)

async def moderation_filter(update, context):
    msg = update.effective_message
    if not msg or not msg.chat or msg.chat.type == "private":
        return
    uid = msg.from_user.id if msg.from_user else 0
    if is_owner(uid) or is_manager(uid) or is_protection_manager(uid):
        return

    text = msg.text or msg.caption or ""
    chat_id = msg.chat.id

    checks = [
        ("links", bool(re.search(r"(https?://|www\.|t\.me/)", text, re.I))),
        ("tag", bool(re.search(r"@\w+", text))),
        ("photo", bool(msg.photo)),
        ("video", bool(msg.video)),
        ("audio", bool(msg.audio or msg.voice)),
        ("file", bool(msg.document)),
        ("stickers", bool(msg.sticker)),
        ("gif", bool(msg.animation)),
        ("forward", bool(msg.forward_origin)),
    ]
    for key, found in checks:
        if found and get_setting(chat_id, key):
            try:
                await msg.delete()
                log_action(chat_id, uid, f"blocked_{key}", uid)
            except Exception:
                pass
            return

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "panel_protection":
        if not is_protection_manager(uid):
            await q.edit_message_text("⛔ هذه الصفحة لمدير الحماية والمالك.")
            return
        buttons = []
        for key, label in LOCKS.items():
            state = "🔒" if get_setting(q.message.chat.id, key) else "🔓"
            buttons.append([
                InlineKeyboardButton(
                    f"{state} {label}",
                    callback_data=f"toggle:{key}"
                )
            ])
        await q.edit_message_text(
            "🛡️ إدارة الحماية\nاضغط على أي نوع لتغيير حالته.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif data.startswith("toggle:"):
        if not is_protection_manager(uid):
            await q.edit_message_text("⛔ لا تملك صلاحية الحماية.")
            return
        key = data.split(":", 1)[1]
        value = not get_setting(q.message.chat.id, key)
        set_setting(q.message.chat.id, key, value)
        await q.answer("تم تحديث الحالة")
        await callback_panel_protection_refresh(q, context)
    elif data == "panel_moderation":
        if not is_manager(uid):
            await q.edit_message_text("⛔ هذه الصفحة للمدير العام والمالك.")
            return
        await q.edit_message_text(
            "🔨 أوامر الإدارة:\n"
            "/ban — حظر\n/unban — إلغاء الحظر\n"
            "/kick — طرد\n/mute — كتم\n/unmute — فك الكتم\n"
            "/del — حذف الرسالة\n/pin — تثبيت"
        )
    elif data == "panel_managers":
        if not is_manager(uid):
            await q.edit_message_text("⛔ غير مسموح.")
            return
        await q.edit_message_text(
            "👑 إدارة الصلاحيات:\n"
            "/addmanager — مدير عام\n"
            "/delmanager — حذف مدير عام\n"
            "/managers — قائمة المديرين\n\n"
            "🛡️ مدير الحماية:\n"
            "/addprotect — إضافة مدير حماية\n"
            "/delprotect — حذف مدير حماية\n"
            "/protectors — قائمة مديري الحماية"
        )
    elif data == "panel_locks":
        if not is_protection_manager(uid):
            await q.edit_message_text("⛔ غير مسموح.")
            return
        lines = ["📋 حالة الحماية:"]
        for key, label in LOCKS.items():
            lines.append(f"• {label}: {'🔒' if get_setting(q.message.chat.id,key) else '🔓'}")
        await q.edit_message_text("\n".join(lines))
    elif data == "panel_logs":
        if not is_manager(uid):
            await q.edit_message_text("⛔ غير مسموح.")
            return
        rows = execute(
            "SELECT action,user_id,target_id FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 10",
            (q.message.chat.id,), True
        )
        text = "📊 السجلات:\n" + (
            "\n".join(f"• {r['action']} | {r['user_id']} | {r['target_id'] or '-'}" for r in rows)
            if rows else "لا توجد سجلات."
        )
        await q.edit_message_text(text)
    elif data == "panel_settings":
        if not is_manager(uid):
            await q.edit_message_text("⛔ غير مسموح.")
            return
        await q.edit_message_text("⚙️ الإعدادات الأساسية محفوظة في قاعدة بيانات البوت.")

async def callback_panel_protection_refresh(q, context):
    buttons = []
    for key, label in LOCKS.items():
        state = "🔒" if get_setting(q.message.chat.id, key) else "🔓"
        buttons.append([InlineKeyboardButton(f"{state} {label}", callback_data=f"toggle:{key}")])
    await q.edit_message_text(
        "🛡️ إدارة الحماية\nاضغط على أي نوع لتغيير حالته.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

def build_app():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("addmanager", add_manager))
    app.add_handler(CommandHandler("delmanager", del_manager))
    app.add_handler(CommandHandler("managers", managers))
    app.add_handler(CommandHandler("addprotect", add_protect))
    app.add_handler(CommandHandler("delprotect", del_protect))
    app.add_handler(CommandHandler("protectors", protectors))
    app.add_handler(CommandHandler("locks", locks))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("del", delete_msg))
    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("logs", logs_cmd))

    # Slash lock commands
    for key in LOCKS:
        app.add_handler(CommandHandler(f"lock{key.replace('_','')}", make_lock_handler(key, True)))
        app.add_handler(CommandHandler(f"unlock{key.replace('_','')}", make_lock_handler(key, False)))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, arabic_text_command), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderation_filter), group=1)
    return app

if __name__ == "__main__":
    app = build_app()
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)
