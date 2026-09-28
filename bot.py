
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
PORT = int(os.getenv("PORT", "10000"))
DB_FILE = os.getenv("DB_FILE", "bot.db")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing")

LOCK = threading.RLock()

PROTECTION = {
    "links": "الروابط", "photo": "الصور", "video": "الفيديو",
    "audio": "الصوت", "file": "الملفات", "stickers": "الملصقات",
    "gif": "GIF", "tag": "المنشن", "bots": "البوتات",
    "forward": "إعادة التوجيه", "games": "الألعاب",
    "repeat": "التكرار"
}

ARABIC = {
    "منع الروابط": ("links", 1), "السماح بالروابط": ("links", 0),
    "منع الصور": ("photo", 1), "السماح بالصور": ("photo", 0),
    "منع الفيديو": ("video", 1), "السماح بالفيديو": ("video", 0),
    "منع الصوت": ("audio", 1), "السماح بالصوت": ("audio", 0),
    "منع الملفات": ("file", 1), "السماح بالملفات": ("file", 0),
    "منع الملصقات": ("stickers", 1), "السماح بالملصقات": ("stickers", 0),
    "منع gif": ("gif", 1), "السماح gif": ("gif", 0),
    "منع المنشن": ("tag", 1), "السماح بالمنشن": ("tag", 0),
    "منع البوتات": ("bots", 1), "السماح بالبـوتات": ("bots", 0),
    "منع إعادة التوجيه": ("forward", 1), "السماح بإعادة التوجيه": ("forward", 0),
    "منع الألعاب": ("games", 1), "السماح بالألعاب": ("games", 0),
    "منع التكرار": ("repeat", 1), "السماح بالتكرار": ("repeat", 0)
}

def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def q(sql, args=(), fetch=False):
    with LOCK:
        c = conn()
        cur = c.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        c.commit()
        c.close()
        return rows

def init_db():
    with LOCK:
        c = conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS managers(
            user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT);
        CREATE TABLE IF NOT EXISTS protectors(
            user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT);
        CREATE TABLE IF NOT EXISTS groups(
            chat_id INTEGER PRIMARY KEY, title TEXT,
            welcome_enabled INTEGER DEFAULT 0,
            welcome_text TEXT DEFAULT '👋 أهلاً {name} في {group}');
        CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER, key TEXT, value INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id,key));
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
            user_id INTEGER, action TEXT, target_id INTEGER,
            details TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS stats(
            chat_id INTEGER PRIMARY KEY, messages INTEGER DEFAULT 0,
            joins INTEGER DEFAULT 0, leaves INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
        """)
        c.commit()
        c.close()

def ensure(chat):
    q("INSERT OR IGNORE INTO groups(chat_id,title) VALUES(?,?)",
      (chat.id, chat.title or ""))
    q("INSERT OR IGNORE INTO stats(chat_id) VALUES(?)", (chat.id,))

def inc(chat_id, field):
    if field not in {"messages","joins","leaves","deleted","blocked"}:
        return
    q("INSERT OR IGNORE INTO stats(chat_id) VALUES(?)", (chat_id,))
    q(f"UPDATE stats SET {field}={field}+1 WHERE chat_id=?", (chat_id,))

def log(chat_id, uid, action, target=None, details=""):
    q("""INSERT INTO logs(chat_id,user_id,action,target_id,details,created_at)
         VALUES(?,?,?,?,?,?)""", (chat_id,uid,action,target,details,utc()))

def is_owner(uid): return uid == OWNER_ID
def is_manager(uid):
    return is_owner(uid) or bool(q("SELECT 1 FROM managers WHERE user_id=?", (uid,), True))
def is_protector(uid):
    return is_owner(uid) or bool(q("SELECT 1 FROM protectors WHERE user_id=?", (uid,), True))

async def notify(context, text):
    try:
        await context.bot.send_message(OWNER_ID, text)
    except Exception:
        pass

async def allowed(update, role="manager"):
    uid = update.effective_user.id
    ok = is_manager(uid) if role == "manager" else is_protector(uid)
    if not ok:
        await update.effective_message.reply_text("⛔ لا تملك صلاحية هذا الأمر.")
    return ok

async def get_target(update, context):
    m = update.effective_message
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    if context.args:
        a = context.args[0]
        if a.lstrip("-").isdigit():
            try:
                return (await context.bot.get_chat_member(update.effective_chat.id, int(a))).user
            except Exception:
                return None
    return None

async def start(update, context):
    if update.effective_chat.type != "private":
        ensure(update.effective_chat)
    await update.effective_message.reply_text(
        "✅ تم تشغيل البوت\n\n🎛️ /panel لوحة التحكم\n📚 /help المساعدة")

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "📚 أوامر البوت\n\n"
        "🎛️ /panel\n👑 /addmanager /delmanager /managers\n"
        "🛡️ /addprotect /delprotect /protectors\n"
        "🔐 /locks\n🔨 /ban /unban /kick /mute /unmute /del /pin\n"
        "👋 /welcome /setwelcome /disablewelcome\n"
        "📊 /stats /logs /report\n🎮 /game /games\n⚙️ /settings\n\n"
        "الأوامر الإدارية تعمل بالرد على رسالة العضو.")

async def panel(update, context):
    uid = update.effective_user.id
    if not (is_manager(uid) or is_protector(uid)):
        return await update.effective_message.reply_text("⛔ لا تملك صلاحية اللوحة.")
    b = [
        [InlineKeyboardButton("🛡️ الحماية", callback_data="protect")],
        [InlineKeyboardButton("🔨 الإدارة", callback_data="moderation")],
        [InlineKeyboardButton("👋 الترحيب", callback_data="welcome")],
        [InlineKeyboardButton("🎮 الألعاب", callback_data="games")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")]
    ]
    if is_manager(uid):
        b += [
            [InlineKeyboardButton("👑 الصلاحيات", callback_data="roles")],
            [InlineKeyboardButton("📋 السجلات", callback_data="logs")],
            [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")]
        ]
    await update.effective_message.reply_text(
        "🎛️ لوحة التحكم الرئيسية", reply_markup=InlineKeyboardMarkup(b))

async def addmanager(update, context):
    if not await allowed(update): return
    t = await get_target(update, context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو.")
    if t.id == OWNER_ID: return await update.effective_message.reply_text("ℹ️ هذا هو المالك.")
    q("INSERT OR REPLACE INTO managers VALUES(?,?,?)", (t.id,update.effective_user.id,utc()))
    await update.effective_message.reply_text("✅ تم تعيين مدير عام.")
    await notify(context, f"👑 تعيين مدير عام\nالعضو: {t.full_name}\nID: {t.id}")

async def delmanager(update, context):
    if not await allowed(update): return
    t = await get_target(update, context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو.")
    q("DELETE FROM managers WHERE user_id=?", (t.id,))
    await update.effective_message.reply_text("✅ تم حذف المدير العام.")
    await notify(context, f"👑 حذف مدير عام\nالعضو: {t.full_name}\nID: {t.id}")

async def managers(update, context):
    if not await allowed(update): return
    rows = q("SELECT user_id FROM managers", fetch=True)
    await update.effective_message.reply_text(
        "👑 المديرون:\n" + ("\n".join("• "+str(x["user_id"]) for x in rows) if rows else "لا يوجد"))

async def addprotect(update, context):
    if not is_owner(update.effective_user.id):
        return await update.effective_message.reply_text("⛔ المالك فقط.")
    t = await get_target(update, context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو.")
    q("INSERT OR REPLACE INTO protectors VALUES(?,?,?)", (t.id,update.effective_user.id,utc()))
    await update.effective_message.reply_text("🛡️ تم تعيين مدير حماية.")
    await notify(context, f"🛡️ تعيين مدير حماية\nالعضو: {t.full_name}\nID: {t.id}")

async def delprotect(update, context):
    if not is_owner(update.effective_user.id):
        return await update.effective_message.reply_text("⛔ المالك فقط.")
    t = await get_target(update, context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم الأمر بالرد على العضو.")
    q("DELETE FROM protectors WHERE user_id=?", (t.id,))
    await update.effective_message.reply_text("✅ تم حذف مدير الحماية.")
    await notify(context, f"🛡️ حذف مدير حماية\nالعضو: {t.full_name}\nID: {t.id}")

async def protectors(update, context):
    if not (is_owner(update.effective_user.id) or is_manager(update.effective_user.id)):
        return await update.effective_message.reply_text("⛔ غير مسموح.")
    rows=q("SELECT user_id FROM protectors",fetch=True)
    await update.effective_message.reply_text(
        "🛡️ مديرو الحماية:\n"+("\n".join("• "+str(x["user_id"]) for x in rows) if rows else "لا يوجد"))

async def lock(update, context, key, value):
    if not await allowed(update,"protection"): return
    q("""INSERT INTO settings(chat_id,key,value) VALUES(?,?,?)
         ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value""",
      (update.effective_chat.id,key,value))
    await update.effective_message.reply_text("🔒 تم المنع." if value else "🔓 تم السماح.")
    log(update.effective_chat.id,update.effective_user.id,"تغيير حماية",details=key)

def lockcmd(key, value):
    async def f(update, context): await lock(update, context, key, value)
    return f

async def locks(update, context):
    if not await allowed(update,"protection"): return
    out=["🔐 حالة الحماية:"]
    for k,n in PROTECTION.items():
        r=q("SELECT value FROM settings WHERE chat_id=? AND key=?", (update.effective_chat.id,k), True)
        out.append(f"• {n}: {'🔒' if r and r[0]['value'] else '🔓'}")
    await update.effective_message.reply_text("\n".join(out))

async def ban(update, context):
    if not await allowed(update): return
    t=await get_target(update,context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم /ban بالرد.")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id,t.id)
        await update.effective_message.reply_text("🚫 تم الحظر.")
        log(update.effective_chat.id,update.effective_user.id,"حظر",t.id)
        await notify(context,f"🚫 حظر عضو\nالمجموعة: {update.effective_chat.title}\nالعضو: {t.full_name}\nID: {t.id}")
    except Exception: await update.effective_message.reply_text("❌ تعذر الحظر. تأكد أن البوت مشرف.")

async def unban(update, context):
    if not await allowed(update): return
    t=await get_target(update,context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم /unban بالرد.")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id,t.id,only_if_banned=True)
        await update.effective_message.reply_text("✅ تم إلغاء الحظر.")
    except Exception: await update.effective_message.reply_text("❌ تعذر إلغاء الحظر.")

async def kick(update, context):
    if not await allowed(update): return
    t=await get_target(update,context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم /kick بالرد.")
    try:
        cid=update.effective_chat.id
        await context.bot.ban_chat_member(cid,t.id)
        await context.bot.unban_chat_member(cid,t.id)
        await update.effective_message.reply_text("👢 تم الطرد.")
    except Exception: await update.effective_message.reply_text("❌ تعذر الطرد.")

async def mute(update, context):
    if not await allowed(update): return
    t=await get_target(update,context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم /mute بالرد.")
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,t.id,
            permissions=ChatPermissions(can_send_messages=False))
        await update.effective_message.reply_text("🔇 تم الكتم.")
    except Exception: await update.effective_message.reply_text("❌ تعذر الكتم.")

async def unmute(update, context):
    if not await allowed(update): return
    t=await get_target(update,context)
    if not t: return await update.effective_message.reply_text("⚠️ استخدم /unmute بالرد.")
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,t.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        await update.effective_message.reply_text("🔊 تم فك الكتم.")
    except Exception: await update.effective_message.reply_text("❌ تعذر فك الكتم.")

async def delete_cmd(update, context):
    if not await allowed(update): return
    try: await update.effective_message.delete()
    except Exception: await update.effective_message.reply_text("❌ تعذر حذف الرسالة.")

async def pin(update, context):
    if not await allowed(update): return
    if not update.effective_message.reply_to_message:
        return await update.effective_message.reply_text("⚠️ استخدم /pin بالرد.")
    try:
        await update.effective_message.reply_to_message.pin(disable_notification=True)
        await update.effective_message.reply_text("📌 تم التثبيت.")
    except Exception: await update.effective_message.reply_text("❌ تعذر التثبيت.")

async def stats(update, context):
    if not await allowed(update,"protection"): return
    r=q("SELECT * FROM stats WHERE chat_id=?", (update.effective_chat.id,), True)
    x=r[0] if r else {"messages":0,"joins":0,"leaves":0,"deleted":0,"blocked":0}
    await update.effective_message.reply_text(
        f"📊 إحصائيات المجموعة\n\n💬 الرسائل: {x['messages']}\n"
        f"👋 الدخول: {x['joins']}\n🚪 الخروج: {x['leaves']}\n"
        f"🗑️ المحذوف: {x['deleted']}\n🚫 الممنوع: {x['blocked']}")

async def logs(update, context):
    if not await allowed(update): return
    rows=q("SELECT action,user_id,target_id,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 30",
           (update.effective_chat.id,), True)
    await update.effective_message.reply_text(
        "📋 آخر العمليات:\n"+("\n".join(
        f"• {r['action']} | المنفذ {r['user_id']} | الهدف {r['target_id'] or '-'}"
        for r in rows) if rows else "لا توجد سجلات."))

async def welcome(update, context):
    if not await allowed(update): return
    q("UPDATE groups SET welcome_enabled=1 WHERE chat_id=?", (update.effective_chat.id,))
    await update.effective_message.reply_text("👋 تم تفعيل الترحيب.")

async def disablewelcome(update, context):
    if not await allowed(update): return
    q("UPDATE groups SET welcome_enabled=0 WHERE chat_id=?", (update.effective_chat.id,))
    await update.effective_message.reply_text("🔕 تم إيقاف الترحيب.")

async def setwelcome(update, context):
    if not await allowed(update): return
    text=" ".join(context.args).strip()
    if not text:
        return await update.effective_message.reply_text("استخدم /setwelcome أهلاً {name} في {group}")
    q("UPDATE groups SET welcome_text=? WHERE chat_id=?", (text,update.effective_chat.id))
    await update.effective_message.reply_text("✅ تم حفظ رسالة الترحيب.")

async def settings(update, context):
    if not await allowed(update): return
    await update.effective_message.reply_text(
        "⚙️ الإعدادات\n👑 المالك: جميع الصلاحيات\n"
        "👨‍💼 المدير العام: الإدارة والسجلات\n🛡️ مدير الحماية: المنع والحماية والإحصائيات")

async def game(update, context):
    if not await allowed(update,"protection"): return
    import random
    await update.effective_message.reply_text(
        random.choice(["🪙 النتيجة: وجه","🪙 النتيجة: كتابة",
                       f"🎲 النرد: {random.randint(1,6)}",
                       f"🎯 الرقم: {random.randint(1,100)}"]))

async def games(update, context):
    await update.effective_message.reply_text(
        "🎮 الألعاب\n/game — لعبة عشوائية\n/games — قائمة الألعاب")

async def member_event(update, context):
    u=update.chat_member
    chat=u.chat
    ensure(chat)
    old=u.old_chat_member.status
    new=u.new_chat_member.status
    user=u.new_chat_member.user
    entered = old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new in (
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    left = old in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and new in (
        ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    if entered:
        inc(chat.id,"joins")
        row=q("SELECT welcome_enabled,welcome_text FROM groups WHERE chat_id=?", (chat.id,), True)
        if row and row[0]["welcome_enabled"]:
            text=row[0]["welcome_text"].replace("{name}",user.full_name).replace("{group}",chat.title or "")
            try: await context.bot.send_message(chat.id,text)
            except Exception: pass
        await notify(context,f"👋 دخول عضو\nالمجموعة: {chat.title}\nالعضو: {user.full_name}\nID: {user.id}")
    elif left:
        inc(chat.id,"leaves")
        await notify(context,f"🚪 خروج/إزالة عضو\nالمجموعة: {chat.title}\nالعضو: {user.full_name}\nID: {user.id}")

async def text_alias(update, context):
    t=(update.effective_message.text or "").strip()
    if t=="لوحة التحكم": return await panel(update,context)
    if t=="المساعدة": return await help_cmd(update,context)
    if t=="المدراء": return await managers(update,context)
    if t=="مديرو الحماية": return await protectors(update,context)
    if t=="إحصائيات": return await stats(update,context)
    if t in ARABIC:
        k,v=ARABIC[t]
        return await lock(update,context,k,v)

async def moderation(update, context):
    m=update.effective_message
    if not m or m.chat.type=="private": return
    ensure(m.chat)
    inc(m.chat.id,"messages")
    uid=m.from_user.id if m.from_user else 0
    if is_owner(uid) or is_manager(uid) or is_protector(uid): return
    text=m.text or m.caption or ""
    checks=[
        ("links", bool(re.search(r"(https?://|www\.|t\.me/)",text,re.I))),
        ("tag", bool(re.search(r"@\w+",text))),
        ("photo", bool(m.photo)),("video", bool(m.video)),
        ("audio", bool(m.audio or m.voice)),("file", bool(m.document)),
        ("stickers", bool(m.sticker)),("gif", bool(m.animation)),
        ("forward", bool(m.forward_origin))
    ]
    for key,found in checks:
        if not found: continue
        r=q("SELECT value FROM settings WHERE chat_id=? AND key=?", (m.chat.id,key), True)
        if r and r[0]["value"]:
            try:
                await m.delete()
                inc(m.chat.id,"deleted"); inc(m.chat.id,"blocked")
                log(m.chat.id,uid,"منع "+key,uid)
            except Exception: pass
            return

async def callback(update, context):
    c=update.callback_query
    await c.answer()
    uid=c.from_user.id
    cid=c.message.chat.id
    if c.data=="protect":
        if not is_protector(uid): return await c.edit_message_text("⛔ غير مسموح.")
        b=[]
        for k,n in PROTECTION.items():
            r=q("SELECT value FROM settings WHERE chat_id=? AND key=?", (cid,k), True)
            b.append([InlineKeyboardButton(
                f"{'🔒' if r and r[0]['value'] else '🔓'} {n}", callback_data="toggle:"+k)])
        return await c.edit_message_text("🛡️ إدارة الحماية", reply_markup=InlineKeyboardMarkup(b))
    if c.data.startswith("toggle:"):
        if not is_protector(uid): return await c.answer("غير مسموح",show_alert=True)
        k=c.data.split(":",1)[1]
        r=q("SELECT value FROM settings WHERE chat_id=? AND key=?", (cid,k), True)
        v=0 if r and r[0]["value"] else 1
        q("""INSERT INTO settings(chat_id,key,value) VALUES(?,?,?)
             ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value""",(cid,k,v))
        return await c.edit_message_text(f"🛡️ {PROTECTION[k]}: {'🔒 ممنوع' if v else '🔓 مسموح'}")
    if c.data=="moderation":
        if not is_manager(uid): return await c.edit_message_text("⛔ غير مسموح.")
        return await c.edit_message_text("🔨 /ban /unban /kick /mute /unmute /del /pin")
    if c.data=="welcome":
        if not is_manager(uid): return await c.edit_message_text("⛔ غير مسموح.")
        return await c.edit_message_text("👋 /welcome\n/setwelcome النص\n/disablewelcome")
    if c.data=="games":
        return await c.edit_message_text("🎮 /game — لعبة عشوائية\n/games — قائمة الألعاب")
    if c.data=="stats":
        if not is_protector(uid): return await c.edit_message_text("⛔ غير مسموح.")
        r=q("SELECT * FROM stats WHERE chat_id=?", (cid,), True)
        x=r[0] if r else {"messages":0,"joins":0,"leaves":0,"deleted":0,"blocked":0}
        return await c.edit_message_text(
            f"📊 الرسائل: {x['messages']}\n👋 الدخول: {x['joins']}\n"
            f"🚪 الخروج: {x['leaves']}\n🗑️ المحذوف: {x['deleted']}\n🚫 الممنوع: {x['blocked']}")
    if c.data=="roles":
        if not is_manager(uid): return await c.edit_message_text("⛔ غير مسموح.")
        return await c.edit_message_text(
            "👑 الصلاحيات\n/addmanager /delmanager /managers\n\n"
            "🛡️ مدير الحماية\n/addprotect /delprotect /protectors")
    if c.data=="logs":
        if not is_manager(uid): return await c.edit_message_text("⛔ غير مسموح.")
        rows=q("SELECT action,user_id,target_id FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 10",(cid,),True)
        return await c.edit_message_text("📋 السجلات:\n"+(
            "\n".join(f"• {x['action']} | {x['user_id']} | {x['target_id'] or '-'}" for x in rows)
            if rows else "لا توجد سجلات."))
    if c.data=="settings":
        if not is_manager(uid): return await c.edit_message_text("⛔ غير مسموح.")
        return await c.edit_message_text("⚙️ الإعدادات محفوظة.")

def health():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self,*args): pass
    HTTPServer(("0.0.0.0",PORT),Handler).serve_forever()

def build():
    init_db()
    app=Application.builder().token(TOKEN).build()
    cmds={
        "start":start,"help":help_cmd,"panel":panel,
        "addmanager":addmanager,"delmanager":delmanager,"managers":managers,
        "addprotect":addprotect,"delprotect":delprotect,"protectors":protectors,
        "locks":locks,"ban":ban,"unban":unban,"kick":kick,"mute":mute,
        "unmute":unmute,"del":delete_cmd,"pin":pin,"stats":stats,"logs":logs,
        "report":stats,"welcome":welcome,"disablewelcome":disablewelcome,
        "setwelcome":setwelcome,"settings":settings,"game":game,"games":games
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name,fn))
    for k in PROTECTION:
        app.add_handler(CommandHandler("lock"+k,lockcmd(k,1)))
        app.add_handler(CommandHandler("unlock"+k,lockcmd(k,0)))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(ChatMemberHandler(member_event,ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_alias),group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,moderation),group=1)
    return app

if __name__=="__main__":
    threading.Thread(target=health,daemon=True).start()
    app=build()
    print("BOT_READY")
    app.run_polling(drop_pending_updates=True,allowed_updates=Update.ALL_TYPES)
