import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import asyncio
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
BOOTSTRAP_CODE = os.environ.get("BOOTSTRAP_CODE", "").strip()

DB_PATH = os.environ.get("DB_PATH", "bot.db")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("groupbot")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS managers(
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS settings(
    chat_id INTEGER PRIMARY KEY,
    links INTEGER DEFAULT 0,
    photos INTEGER DEFAULT 0,
    videos INTEGER DEFAULT 0,
    files INTEGER DEFAULT 0,
    audio INTEGER DEFAULT 0,
    welcome INTEGER DEFAULT 0,
    alerts INTEGER DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS watched_groups(
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT
)""")
db.commit()

def now():
    return datetime.now(timezone.utc).isoformat()

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID and OWNER_ID != 0

def is_manager(uid: int) -> bool:
    if is_owner(uid):
        return True
    row = db.execute("SELECT 1 FROM managers WHERE user_id=?", (uid,)).fetchone()
    return bool(row)

def is_group_admin(update: Update) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    return is_manager(update.effective_user.id)

def get_settings(chat_id):
    row = db.execute("SELECT links,photos,videos,files,audio,welcome,alerts FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        db.execute("INSERT INTO settings(chat_id) VALUES(?)", (chat_id,))
        db.commit()
        return (0,0,0,0,0,0,1)
    return row

def set_setting(chat_id, field, value):
    allowed = {"links","photos","videos","files","audio","welcome","alerts"}
    if field not in allowed:
        return
    db.execute(f"UPDATE settings SET {field}=? WHERE chat_id=?", (int(value), chat_id))
    db.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            f"مرحبًا 👋\nرقم حسابك: {update.effective_user.id}\n\n"
            "استخدم /panel لفتح لوحة التحكم.\n"
            "إذا كنت المالك، ضع OWNER_ID في Render أو استخدم /claim مع رمز التفعيل."
        )
    else:
        await update.message.reply_text("تم تشغيل البوت ✅\nاستخدم /panel للوحة التحكم.")

async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOOTSTRAP_CODE:
        await update.message.reply_text("لم يتم تفعيل رمز المطالبة بالملكية.")
        return
    if OWNER_ID != 0:
        await update.message.reply_text("تم تحديد مالك للبوت مسبقًا.")
        return
    code = context.args[0] if context.args else ""
    if code == BOOTSTRAP_CODE:
        await update.message.reply_text(
            "تم قبول رمز التفعيل. الآن ضع رقم حسابك في Render كقيمة OWNER_ID ثم أعد التشغيل."
        )
    else:
        await update.message.reply_text("رمز التفعيل غير صحيح.")

def panel_markup(owner=False):
    rows = [
        [InlineKeyboardButton("🛡️ الحماية", callback_data="security"),
         InlineKeyboardButton("👥 المدراء", callback_data="managers")],
        [InlineKeyboardButton("📢 المستجدات", callback_data="alerts"),
         InlineKeyboardButton("📋 السجل", callback_data="logs")],
        [InlineKeyboardButton("⚙️ إعدادات القروب", callback_data="settings")]
    ]
    if owner:
        rows.append([InlineKeyboardButton("➕ إضافة مدير", callback_data="add_manager_help")])
    return InlineKeyboardMarkup(rows)

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية استخدام لوحة الإدارة.")
        return
    await update.effective_message.reply_text(
        "👑 لوحة تحكم البوت\n\nاختر القسم المطلوب:",
        reply_markup=panel_markup(is_owner(update.effective_user.id))
    )

async def addmanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    if not context.args:
        await update.effective_message.reply_text("الاستخدام: /addmanager رقم_المستخدم")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح.")
        return
    db.execute("INSERT OR REPLACE INTO managers(user_id,added_by,added_at) VALUES(?,?,?)",
               (uid, update.effective_user.id, now()))
    db.commit()
    await update.effective_message.reply_text(f"✅ تمت إضافة المدير: {uid}")

async def delmanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر للمالك فقط.")
        return
    if not context.args:
        await update.effective_message.reply_text("الاستخدام: /delmanager رقم_المستخدم")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح.")
        return
    db.execute("DELETE FROM managers WHERE user_id=?", (uid,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم حذف المدير: {uid}")

async def managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
    text = "👥 المدراء:\n" + ("\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مدراء إضافيون.")
    if OWNER_ID:
        text += f"\n\n👑 المالك: {OWNER_ID}"
    await update.effective_message.reply_text(text)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"🆔 رقمك: {update.effective_user.id}")

async def idgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"🆔 رقم القروب: {update.effective_chat.id}")

async def require_admin(update):
    if not update.effective_user:
        return False
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل القروب.")
        return False
    if not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return False
    return True

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target and context.args:
        try: target = await context.bot.get_chat(int(context.args[0]))
        except Exception: target = None
    if not target:
        await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو أو /ban رقم_المستخدم")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text("✅ تم حظر العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الحظر: {e}")

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("استخدم /kick بالرد على رسالة العضو.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        await update.message.reply_text("✅ تم طرد العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الطرد: {e}")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("استخدم /mute بالرد على رسالة العضو.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text("✅ تم كتم العضو.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الكتم: {e}")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("استخدم /unmute بالرد على رسالة العضو.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await update.message.reply_text("✅ تم فك الكتم.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر فك الكتم: {e}")

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("استخدم /del بالرد على الرسالة المراد حذفها.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الحذف: {e}")

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("استخدم /pin بالرد على الرسالة.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("📌 تم التثبيت.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر التثبيت: {e}")

async def set_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    cmd = update.message.text.split()[0].lower().lstrip("/")
    mapping = {
        "locklinks": "links", "unlocklinks": "links",
        "lockphoto": "photos", "unlockphoto": "photos",
        "lockvideo": "videos", "unlockvideo": "videos",
        "lockaudio": "audio", "unlockaudio": "audio",
        "lockfile": "files", "unlockfile": "files",
    }
    field = mapping.get(cmd)
    if not field:
        return
    value = 0 if cmd.startswith("unlock") else 1
    set_setting(update.effective_chat.id, field, value)
    await update.message.reply_text(("🔒 تم المنع." if value else "🔓 تم السماح."))

async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_manager(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية.")
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("استخدم الأمر داخل القروب.")
        return
    db.execute("INSERT OR REPLACE INTO watched_groups(chat_id,title,added_at) VALUES(?,?,?)",
               (update.effective_chat.id, update.effective_chat.title or "", now()))
    db.commit()
    set_setting(update.effective_chat.id, "alerts", 1)
    await update.effective_message.reply_text("📢 تم تفعيل متابعة مستجدات هذا القروب.")

async def message_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    links, photos, videos, files, audio, welcome, alerts_on = get_settings(chat.id)

    should_delete = False
    reason = ""
    if links and msg.text:
        import re
        if re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", msg.text, re.I):
            should_delete, reason = True, "رابط"
    if photos and msg.photo:
        should_delete, reason = True, "صورة"
    if videos and (msg.video or msg.video_note):
        should_delete, reason = True, "فيديو"
    if files and msg.document:
        should_delete, reason = True, "ملف"
    if audio and (msg.audio or msg.voice):
        should_delete, reason = True, "صوت"

    if should_delete and is_manager(update.effective_user.id) is False:
        try:
            await msg.delete()
        except Exception:
            pass

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_manager(q.from_user.id):
        await q.edit_message_text("⛔ ليس لديك صلاحية.")
        return
    if q.data == "managers":
        rows = db.execute("SELECT user_id FROM managers ORDER BY user_id").fetchall()
        text = "👥 المدراء:\n" + ("\n".join(f"• {r[0]}" for r in rows) if rows else "لا يوجد مدراء.")
        await q.edit_message_text(text)
    elif q.data == "security":
        await q.edit_message_text(
            "🛡️ أوامر الحماية:\n"
            "/locklinks و /unlocklinks\n"
            "/lockphoto و /unlockphoto\n"
            "/lockvideo و /unlockvideo\n"
            "/lockaudio و /unlockaudio\n"
            "/lockfile و /unlockfile"
        )
    elif q.data == "alerts":
        await q.edit_message_text("📢 استخدم /alerts داخل القروب لتفعيل متابعة مستجداته.")
    elif q.data == "settings":
        await q.edit_message_text("⚙️ استخدم أوامر الحماية والإعدادات المتاحة داخل القروب.")
    elif q.data == "add_manager_help":
        await q.edit_message_text("➕ استخدم:\n/addmanager رقم_المستخدم")
    elif q.data == "logs":
        await q.edit_message_text("📋 السجل المتقدم يمكن توسيعه لاحقًا.")

async def error_handler(update, context):
    log.error("Update error: %s", context.error)

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
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables.")
    app = Application.builder().token(TOKEN).build()

    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", start),
        CommandHandler("claim", claim),
        CommandHandler("panel", panel),
        CommandHandler("addmanager", addmanager),
        CommandHandler("delmanager", delmanager),
        CommandHandler("managers", managers),
        CommandHandler("id", id_cmd),
        CommandHandler("idgroup", idgroup),
        CommandHandler("ban", ban),
        CommandHandler("kick", kick),
        CommandHandler("mute", mute),
        CommandHandler("unmute", unmute),
        CommandHandler("del", del_cmd),
        CommandHandler("pin", pin),
        CommandHandler("alerts", alerts),
        CallbackQueryHandler(callback),
    ]
    for h in handlers:
        app.add_handler(h)

    for cmd in ["locklinks","unlocklinks","lockphoto","unlockphoto","lockvideo","unlockvideo",
                "lockaudio","unlockaudio","lockfile","unlockfile"]:
        app.add_handler(CommandHandler(cmd, set_lock))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_filter))
    app.add_error_handler(error_handler)
    log.info("Bot started")
    threading.Thread(target=start_health_server, daemon=True).start()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
