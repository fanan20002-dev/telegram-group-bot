import os
import re
import asyncio
import sqlite3
import threading
import logging
import tempfile
import uuid
import time
import random
import json
import calendar as pycalendar
import html
import base64
import io
import httpx
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ChatMemberHandler, ChatJoinRequestHandler
)

# Centralized configuration; aliases preserve the existing names used throughout R11.
from config import (
    BOOTSTRAP_CODE, DB_PATH, OPENAI_API_KEY, OPENAI_MODEL, OWNER_ID,
    RENDER_API_KEY, RENDER_SERVICE_ID, WEBAPP_URL, BOT_TOKEN as TOKEN, settings,
)
from services.moderation_policy import decide_warning_action
from services.join_approval import create_managed_join_request_link as _service_create_managed_join_request_link
from services.group_registry import get_live_linked_channels as _service_get_live_linked_channels
from services.publishing import can_publish_to_live as _service_can_publish_to_live
from services.permissions import (
    can_use_capability as _service_can_use_capability,
    capability_group_ids as _service_capability_group_ids,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("groupbot")
# httpx/httpcore can log full request URLs at INFO level; Telegram bot API URLs
# may contain the bot token. Keep these libraries at WARNING to avoid leaking it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def _resolve_database_path():
    """Prefer Render's persistent disk when available; migrate an existing local DB only if target is absent."""
    configured = os.environ.get("DB_PATH", "").strip()
    if configured:
        target = os.path.abspath(os.path.expanduser(configured))
    elif os.path.isdir("/var/data"):
        target = "/var/data/bot.db"
        legacy = os.path.abspath("bot.db")
        if not os.path.exists(target) and os.path.isfile(legacy) and legacy != target:
            try:
                source = sqlite3.connect(legacy, timeout=30)
                destination = sqlite3.connect(target, timeout=30)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                    source.close()
                log.warning("Migrated existing bot database to Render persistent disk: %s", target)
            except Exception:
                log.exception("Could not migrate legacy bot.db to persistent disk; keeping legacy database path")
                target = legacy
    else:
        target = os.path.abspath("bot.db")
        log.warning("No Render persistent disk detected. DB_PATH is unset; data may be lost on redeploy. Configure a Persistent Disk mounted at /var/data and DB_PATH=/var/data/bot.db after backing up the current database.")
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    # A startup snapshot protects against accidental schema/code changes. Keep only three recent snapshots.
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        try:
            backup_dir = os.path.join(parent, ".bot_backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"bot_prestart_{stamp}.db")
            src = sqlite3.connect(target, timeout=30)
            dst = sqlite3.connect(backup_path, timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            backups = sorted((os.path.join(backup_dir, n) for n in os.listdir(backup_dir) if n.endswith(".db")), key=os.path.getmtime, reverse=True)
            for stale in backups[3:]:
                try: os.remove(stale)
                except OSError: pass
        except Exception:
            log.exception("Automatic pre-start database backup failed; continuing without modifying existing records")
    return target

DB_PATH = _resolve_database_path()
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
db.execute("PRAGMA busy_timeout=30000")
try:
    db.execute("PRAGMA journal_mode=WAL")
except sqlite3.DatabaseError:
    log.exception("Could not enable SQLite WAL mode")
# Additive assistant tables. Existing group/panel/reentry tables are preserved.
db.execute("""CREATE TABLE IF NOT EXISTS assistant_users(
    user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', display_name TEXT DEFAULT '',
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, ai_requests INTEGER DEFAULT 0,
    reminders_count INTEGER DEFAULT 0, notes_count INTEGER DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS assistant_preferences(
    user_id INTEGER PRIMARY KEY, ai_enabled INTEGER NOT NULL DEFAULT 1,
    privacy_notice_seen INTEGER NOT NULL DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS assistant_notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    note TEXT NOT NULL, created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS coursework_pages(
    user_id INTEGER NOT NULL, page_no INTEGER NOT NULL CHECK(page_no BETWEEN 1 AND 4),
    page_title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL, PRIMARY KEY(user_id, page_no)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS assistant_reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    reminder_text TEXT NOT NULL, remind_at TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS assistant_chat_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
)""")
db.commit()
# Additive Mini App tables: independent from legacy coursework/event tables to avoid destructive migration.
db.execute("""CREATE TABLE IF NOT EXISTS miniapp_coursework_pages(
 user_id INTEGER NOT NULL, page_no INTEGER NOT NULL CHECK(page_no BETWEEN 1 AND 100),
 payload TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,page_no))""")
db.execute("""CREATE TABLE IF NOT EXISTS miniapp_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, event_type TEXT NOT NULL,
 title TEXT NOT NULL, event_at TEXT NOT NULL, hijri_date TEXT DEFAULT '', created_at TEXT NOT NULL)""")
# Persistent study log and reminder delivery state for the Mini App.
db.execute("""CREATE TABLE IF NOT EXISTS miniapp_study_schedule(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, day TEXT NOT NULL, time TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL)""")
db.execute("""CREATE TABLE IF NOT EXISTS miniapp_study_sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, subject TEXT NOT NULL, topic TEXT DEFAULT '',
 activity_type TEXT NOT NULL DEFAULT 'مذاكرة', minutes INTEGER NOT NULL DEFAULT 30, completed INTEGER NOT NULL DEFAULT 0,
 session_date TEXT NOT NULL, created_at TEXT NOT NULL)""")
try: db.execute("ALTER TABLE miniapp_events ADD COLUMN reminder_minutes INTEGER NOT NULL DEFAULT 60")
except sqlite3.OperationalError: pass
try: db.execute("ALTER TABLE miniapp_events ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError: pass
db.commit()
# Usage analytics for the student portal. These tables are additive and do not replace existing data.
db.execute("""CREATE TABLE IF NOT EXISTS portal_usage_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_at TEXT NOT NULL,
    week_start TEXT NOT NULL
)""")
db.execute("CREATE INDEX IF NOT EXISTS idx_portal_usage_week ON portal_usage_events(week_start, user_id)")
# Private notes for the interactive university portal.
db.execute("CREATE TABLE IF NOT EXISTS miniapp_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
db.execute("CREATE INDEX IF NOT EXISTS idx_miniapp_notes_user ON miniapp_notes(user_id,updated_at)")
db.execute("CREATE TABLE IF NOT EXISTS miniapp_custom_pages(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,section TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
db.execute("CREATE INDEX IF NOT EXISTS idx_miniapp_custom_pages_user ON miniapp_custom_pages(user_id,section,updated_at)")
db.execute("CREATE TABLE IF NOT EXISTS miniapp_game_scores(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,session_id TEXT NOT NULL,game_key TEXT NOT NULL,score INTEGER NOT NULL DEFAULT 0,rounds INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(user_id,session_id))")
db.execute("CREATE INDEX IF NOT EXISTS idx_miniapp_game_scores_user ON miniapp_game_scores(user_id,created_at)")
db.commit()
db.execute("""CREATE TABLE IF NOT EXISTS portal_weekly_reports(
    week_start TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
)""")
db.commit()

# Private general inquiries are routed ONLY to the primary bot owner.
db.execute("""CREATE TABLE IF NOT EXISTS general_inquiries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    inquiry_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    replied_at TEXT DEFAULT '',
    reply_text TEXT DEFAULT ''
)""")
try: db.execute("ALTER TABLE general_inquiries ADD COLUMN category TEXT NOT NULL DEFAULT 'عام'")
except sqlite3.OperationalError: pass
db.commit()
# Registered broadcast channels. The bot must be an administrator with posting rights.
db.execute("""CREATE TABLE IF NOT EXISTS linked_channels(
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT DEFAULT '',
    linked_by INTEGER NOT NULL,
    linked_at TEXT NOT NULL
)""")
db.commit()

# Additive staff management system. Existing tables and data are retained.
db.executescript("""
CREATE TABLE IF NOT EXISTS staff_tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL DEFAULT 0,
 assigned_to INTEGER NOT NULL, assigned_by INTEGER NOT NULL, title TEXT NOT NULL,
 details TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open', due_at TEXT DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_shifts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL DEFAULT 0,
 user_id INTEGER NOT NULL, assigned_by INTEGER NOT NULL, starts_at TEXT NOT NULL,
 ends_at TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_appeals(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 moderation_log_id INTEGER DEFAULT 0, appeal_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
 reviewer_id INTEGER DEFAULT 0, review_note TEXT DEFAULT '', created_at TEXT NOT NULL, reviewed_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS staff_badges(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, badge TEXT NOT NULL,
 awarded_by INTEGER NOT NULL, reason TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_inquiries(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL DEFAULT 0,
 body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_permissions(
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, permission TEXT NOT NULL,
 granted_by INTEGER NOT NULL, granted_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id,permission)
);
CREATE INDEX IF NOT EXISTS idx_staff_tasks_user ON staff_tasks(assigned_to,status);
CREATE INDEX IF NOT EXISTS idx_staff_shifts_user ON staff_shifts(user_id,status);
CREATE INDEX IF NOT EXISTS idx_staff_appeals_status ON staff_appeals(status);
CREATE TABLE IF NOT EXISTS group_mutes(
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, muted_by INTEGER NOT NULL,
 muted_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id)
);

""")
db.commit()

# Cursor for owner-facing audit notifications. Additive; does not alter existing log data.
db.execute("""CREATE TABLE IF NOT EXISTS owner_activity_cursor(
    id INTEGER PRIMARY KEY CHECK(id=1), last_log_id INTEGER NOT NULL DEFAULT 0
)""")
db.execute("INSERT OR IGNORE INTO owner_activity_cursor(id,last_log_id) VALUES(1,0)")
db.commit()

# Additional personal-assistant features; additive schema only.
db.execute("""CREATE TABLE IF NOT EXISTS assistant_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    task_text TEXT NOT NULL, created_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS assistant_expenses(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    amount REAL NOT NULL, description TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'SAR',
    created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS student_schedule(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    weekday TEXT NOT NULL, study_time TEXT NOT NULL, subject TEXT NOT NULL,
    created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS student_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL, title TEXT NOT NULL, event_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
)""")
db.commit()
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
CREATE TABLE IF NOT EXISTS reentry_restrictions(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY(chat_id, user_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS member_security_warnings(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    warning_count INTEGER NOT NULL DEFAULT 0, last_reason TEXT DEFAULT '',
    updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id)
)
""")


db.execute("""
CREATE TABLE IF NOT EXISTS watched_groups(
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT
)
""")
# External groups are isolated by chat; all new groups default to no AI and no privileged cross-group tools.
db.execute("""CREATE TABLE IF NOT EXISTS external_group_control(
    chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', is_external INTEGER NOT NULL DEFAULT 1,
    added_by INTEGER NOT NULL DEFAULT 0, added_by_name TEXT DEFAULT '', added_by_username TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, ai_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS external_group_capabilities(
    chat_id INTEGER NOT NULL, capability TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
    granted_by INTEGER NOT NULL DEFAULT 0, granted_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, capability)
)""")
# Owner-controlled AI enablement for every registered group (including internal groups).
db.execute("""CREATE TABLE IF NOT EXISTS ai_group_settings(
    chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT ''
)""")
db.execute("""CREATE TABLE IF NOT EXISTS external_support_tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, group_title TEXT DEFAULT '',
    requester_id INTEGER NOT NULL, requester_name TEXT DEFAULT '', requester_username TEXT DEFAULT '',
    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL,
    answered_by INTEGER NOT NULL DEFAULT 0, answer TEXT DEFAULT '', answered_at TEXT DEFAULT ''
)""")
db.execute("""CREATE TABLE IF NOT EXISTS member_violations(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, display_name TEXT DEFAULT '',
    username TEXT DEFAULT '', warning_count INTEGER NOT NULL DEFAULT 0, abuse_count INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT DEFAULT '', reasons_json TEXT DEFAULT '[]', last_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS automatic_mutes(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, muted_until TEXT NOT NULL,
    reason TEXT DEFAULT '', warning_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,user_id)
)""")
db.execute("CREATE INDEX IF NOT EXISTS idx_automatic_mutes_due ON automatic_mutes(status,muted_until)")
_violation_cols = {r[1] for r in db.execute("PRAGMA table_info(member_violations)").fetchall()}
if "abuse_count" not in _violation_cols:
    db.execute("ALTER TABLE member_violations ADD COLUMN abuse_count INTEGER NOT NULL DEFAULT 0")
db.commit()
db.execute("""CREATE TABLE IF NOT EXISTS global_bans(
    user_id INTEGER PRIMARY KEY, display_name TEXT DEFAULT '', username TEXT DEFAULT '',
    reason TEXT DEFAULT '', scope TEXT NOT NULL DEFAULT 'all', active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL, created_at TEXT NOT NULL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS global_ban_groups(
    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, chat_id)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS security_review_runs(
    week_start TEXT PRIMARY KEY, created_at TEXT NOT NULL, report TEXT NOT NULL
)""")
# Existing groups predate external-group registration; preserve them as owner-managed/internal.
for _chat_id, _title in db.execute("SELECT chat_id, title FROM watched_groups").fetchall():
    db.execute("""INSERT OR IGNORE INTO external_group_control
        (chat_id,title,is_external,added_by,enabled,ai_enabled,created_at,updated_at)
        VALUES(?,?,0,?,1,0,?,?)""", (_chat_id, _title or '', OWNER_ID if OWNER_ID else -1, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
# Default features for external groups are conservative; sensitive capabilities remain off.
db.commit()
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
CREATE TABLE IF NOT EXISTS staff_capabilities(
    user_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    chat_id INTEGER NOT NULL DEFAULT 0,
    granted_by INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(user_id, capability, chat_id)
)
""")

# Exceptions to an "all groups" capability grant. Additive migration; existing grants remain intact.
db.execute("""CREATE TABLE IF NOT EXISTS staff_capability_denials(
    user_id INTEGER NOT NULL, capability TEXT NOT NULL, chat_id INTEGER NOT NULL,
    revoked_by INTEGER NOT NULL, revoked_at TEXT NOT NULL,
    PRIMARY KEY(user_id, capability, chat_id)
)""")

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
    created_at TEXT,
    content_type TEXT DEFAULT '',
    forward_origin TEXT DEFAULT '',
    media_file_id TEXT DEFAULT '',
    message_link TEXT DEFAULT ''
)
""")
# Backward-compatible migration for databases created by earlier bot versions.
_archive_columns = {row[1] for row in db.execute("PRAGMA table_info(message_archive)").fetchall()}
for _column, _definition in (("content_type", "TEXT DEFAULT ''"), ("forward_origin", "TEXT DEFAULT ''"),
                              ("media_file_id", "TEXT DEFAULT ''"), ("message_link", "TEXT DEFAULT ''")):
    if _column not in _archive_columns:
        db.execute(f"ALTER TABLE message_archive ADD COLUMN {_column} {_definition}")
db.commit()
db.execute("""
CREATE TABLE IF NOT EXISTS archive_settings(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS weekly_message_activity(
    chat_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id, week_start, user_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS weekly_membership_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS weekly_stats_sent(
    chat_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, week_start)
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
CREATE TABLE IF NOT EXISTS media_config(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
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
# نظام نقاط الأعضاء والألقاب مستقل عن نقاط الألعاب القديمة حفاظًا على نتائجها.
db.executescript("""
CREATE TABLE IF NOT EXISTS member_reputation(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 0,
    last_auto_award REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS member_title_rules(
    chat_id INTEGER NOT NULL, min_points INTEGER NOT NULL, title TEXT NOT NULL,
    PRIMARY KEY(chat_id,min_points)
);
CREATE TABLE IF NOT EXISTS member_points_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    points INTEGER NOT NULL, event_type TEXT NOT NULL, awarded_by INTEGER NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_member_points_rank ON member_reputation(chat_id,points DESC);
CREATE TABLE IF NOT EXISTS member_daily_activity(
    chat_id INTEGER NOT NULL, activity_date TEXT NOT NULL, user_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0, last_message_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(chat_id,activity_date,user_id)
);
CREATE INDEX IF NOT EXISTS idx_member_daily_activity_rank
    ON member_daily_activity(chat_id,activity_date,message_count DESC);
CREATE TABLE IF NOT EXISTS member_daily_report_settings(
    chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
    min_messages INTEGER NOT NULL DEFAULT 5, updated_by INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS member_daily_reports_sent(
    chat_id INTEGER NOT NULL, activity_date TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,activity_date)
);
""")
db.commit()
# ألعاب طويلة المدى: التقدم محفوظ في SQLite ويستمر بعد إعادة تشغيل البوت.
db.execute("""CREATE TABLE IF NOT EXISTS persistent_game_state(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, game_key TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0, coins INTEGER NOT NULL DEFAULT 0,
    last_claim_epoch INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,user_id,game_key)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS persistent_game_flags(
    chat_id INTEGER NOT NULL, game_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(chat_id,game_key)
)""")
db.commit()
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
# Security-by-default for every newly registered group. Existing groups are migrated once below.
DEFAULTS["links"] = 1
DEFAULTS["photos"] = 1
DEFAULTS["videos"] = 1


# ردود آلية قابلة للتخصيص لكل قروب، محفوظة في SQLite.
db.execute("""CREATE TABLE IF NOT EXISTS auto_reply_settings(
    chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS auto_replies(
    chat_id INTEGER NOT NULL, trigger_text TEXT NOT NULL, reply_text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(chat_id, trigger_text)
)""")
# Additive tables for owner-approved daily motivational content. Existing tables are untouched.
db.execute("""CREATE TABLE IF NOT EXISTS motivation_drafts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_date TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL DEFAULT 'تحفيز',
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT '',
    approved_by INTEGER NOT NULL DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS motivation_groups(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
)""")
db.execute("""CREATE TABLE IF NOT EXISTS motivation_posts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    UNIQUE(draft_id, chat_id)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS bot_promo_delivery(
    chat_id INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sending',
    attempted_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(chat_id, local_date, slot)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS group_feature_settings(
    chat_id INTEGER PRIMARY KEY,
    stats_enabled INTEGER NOT NULL DEFAULT 1,
    join_notices_enabled INTEGER NOT NULL DEFAULT 1,
    leave_notices_enabled INTEGER NOT NULL DEFAULT 1,
    analysis_enabled INTEGER NOT NULL DEFAULT 1,
    join_approval_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
)""")
# Additive migration for databases created by earlier bot versions.
_group_feature_cols = {r[1] for r in db.execute("PRAGMA table_info(group_feature_settings)").fetchall()}
for _col, _definition in (("analysis_enabled", "INTEGER NOT NULL DEFAULT 1"), ("join_approval_enabled", "INTEGER NOT NULL DEFAULT 0")):
    if _col not in _group_feature_cols:
        db.execute(f"ALTER TABLE group_feature_settings ADD COLUMN {_col} {_definition}")
db.execute("""CREATE TABLE IF NOT EXISTS join_request_events(
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, display_name TEXT DEFAULT '', username TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', requested_at TEXT NOT NULL, decided_at TEXT DEFAULT '', decided_by INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id,user_id)
)""")
# Central switch for locking entry across every registered group. Additive migration only.
db.execute("""CREATE TABLE IF NOT EXISTS global_join_approval_settings(
    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '', updated_by INTEGER NOT NULL DEFAULT 0
)""")
db.execute("INSERT OR IGNORE INTO global_join_approval_settings(id,enabled,updated_at,updated_by) VALUES(1,0,'',0)")
db.execute("UPDATE global_join_approval_settings SET enabled=0 WHERE id=1")
db.execute("UPDATE group_feature_settings SET join_approval_enabled=0 WHERE join_approval_enabled<>0")
db.commit()
# Evening adhkar: explicit opt-in per registered group and idempotent daily delivery log.
db.execute("""CREATE TABLE IF NOT EXISTS adhkar_group_settings(
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by INTEGER NOT NULL DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS adhkar_posts_sent(
    chat_id INTEGER NOT NULL,
    post_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempted_at TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(chat_id, post_date)
)""")
db.commit()
# Track invite links created by this bot so it can revoke those it owns when needed.
db.execute("""CREATE TABLE IF NOT EXISTS managed_join_invite_links(
    chat_id INTEGER NOT NULL, invite_link TEXT NOT NULL, link_type TEXT NOT NULL DEFAULT 'join_request',
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, revoked_at TEXT DEFAULT '',
    PRIMARY KEY(chat_id,invite_link)
)""")
db.commit()

def global_join_approval_enabled():
    row = db.execute("SELECT enabled FROM global_join_approval_settings WHERE id=1").fetchone()
    return bool(row and row[0])


def group_feature_enabled(chat_id, feature):
    allowed = {"stats_enabled", "join_notices_enabled", "leave_notices_enabled", "analysis_enabled", "join_approval_enabled"}
    if feature not in allowed:
        return False
    # Entry approval/lock was retired; direct group entry remains enabled.
    if feature == "join_approval_enabled":
        return False
    row = db.execute(f"SELECT {feature} FROM group_feature_settings WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        return bool(row[0])
    # Approval is opt-in for backward compatibility; existing notice/stat features stay enabled.
    if feature == "join_approval_enabled":
        return False
    return True

def set_group_feature(chat_id, feature, enabled):
    allowed = {"stats_enabled", "join_notices_enabled", "leave_notices_enabled", "analysis_enabled", "join_approval_enabled"}
    if feature not in allowed:
        raise ValueError("invalid feature")
    db.execute("INSERT INTO group_feature_settings(chat_id,updated_at) VALUES(?,?) ON CONFLICT(chat_id) DO NOTHING", (chat_id, now()))
    db.execute(f"UPDATE group_feature_settings SET {feature}=?,updated_at=? WHERE chat_id=?", (int(bool(enabled)), now(), chat_id))
    db.commit()


async def create_managed_join_request_link(bot, chat_id):
    """Compatibility wrapper for the isolated join-approval service."""
    return await _service_create_managed_join_request_link(
        bot=bot,
        db=db,
        chat_id=chat_id,
        now_fn=now,
        logger=log,
    )

DEFAULT_AUTO_REPLIES = [
    ("شكراً", "العفو، حياك الله 🌹"),
    ("صباح الخير", "صباح النور والسرور ☀️"),
    ("مساء الخير", "مساء النور والورد 🌙"),
    ("جزاك الله خير", "وإياك، بارك الله فيك 🤍"),
    ("قوانين القروب", "📌 يرجى الالتزام بقوانين القروب واحترام الجميع."),
    ("مساعدة", "🤖 حياك الله! اكتب /help لمعرفة الأوامر المتاحة."),
    ("بوت", "🤖 أنا بوت حماية القروبات 🛡️، موجود لمساعدتكم وحماية القروب. اكتبوا /help لمعرفة الخدمات المتاحة."),
    ("من معاي", "الذكاء الاصطناعي"),
]
_auto_reply_last = {}
_AUTO_REPLY_COOLDOWN_SECONDS = 30

def split_mentions_for_telegram(mentions, header="#all\n", max_chars=3900):
    """Split HTML mention strings into Telegram-safe messages without splitting a mention."""
    chunks = []
    current = header
    for mention in mentions:
        addition = mention if current == header else " ، " + mention
        if len(current) + len(addition) > max_chars and current != header:
            chunks.append(current)
            current = "#all (تابع)\n" + mention
        else:
            current += addition
    if current.strip() and current not in ("#all\n", "#all (تابع)\n"):
        chunks.append(current)
    return chunks


def normalize_auto_reply(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[\u064b-\u065f\u0670ـ]", "", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()

def is_direct_bot_call(message_text, bot_username=""):
    """Only treat the nickname as a trigger when a member is addressing the bot."""
    normalized = normalize_auto_reply(message_text)
    if normalized in {"بوت", "يا بوت", "يا بوت بتم", "بوت بتم", "يا بوت حماية", "بوت حماية"}:
        return True
    if normalized.startswith(("يا بوت ", "بوت ")):
        return True
    username = (bot_username or "").lstrip("@").lower()
    if username and re.search(r"(?<!\w)@" + re.escape(username) + r"(?!\w)", (message_text or "").lower()):
        return True
    return False

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

# بنك الألعاب مدمج داخل bot.py لتسهيل النشر على GitHub/Render دون مجلد games منفصل.
QUIZ_CATEGORIES = {
    "general": ("🌐 معلومات عامة", [
        ("ما الكوكب المعروف بالكوكب الأحمر؟", ["الأرض", "المريخ", "زحل", "الزهرة"], 1, "يبدو المريخ أحمر بسبب أكاسيد الحديد على سطحه."),
        ("كم عدد أضلاع الشكل السداسي؟", ["خمسة", "ستة", "سبعة", "ثمانية"], 1, "للشكل السداسي ستة أضلاع."),
        ("ما أكبر حيوان معروف على الأرض؟", ["الفيل الإفريقي", "الحوت الأزرق", "الزرافة", "القرش الأبيض"], 1, "الحوت الأزرق أكبر الحيوانات المعروفة."),
        ("ما اللغة الرسمية في البرازيل؟", ["الإسبانية", "البرتغالية", "الفرنسية", "الإنجليزية"], 1, "يتحدث سكان البرازيل البرتغالية."),
        ("كم دقيقة في ساعتين؟", ["60", "90", "120", "180"], 2, "الساعتان تساويان 120 دقيقة."),
        ("ما الحيوان المعروف بسفينة الصحراء؟", ["الحصان", "الجمل", "اللاما", "الغزال"], 1, "الجمل متكيف مع البيئات الصحراوية."),
        ("ما المعدن السائل في درجة حرارة الغرفة؟", ["الحديد", "الزئبق", "النحاس", "الألمنيوم"], 1, "الزئبق معدن سائل في الظروف المعتادة."),
        ("كم عدد ألوان قوس قزح التقليدية؟", ["خمسة", "ستة", "سبعة", "ثمانية"], 2, "تقسم ألوان قوس قزح تقليديًا إلى سبعة."),
    ]),
    "science": ("🔬 العلوم", [
        ("ما الغاز الذي تمتصه النباتات من الهواء؟", ["الأكسجين", "ثاني أكسيد الكربون", "النيتروجين", "الهيدروجين"], 1, "تستخدم النباتات ثاني أكسيد الكربون في البناء الضوئي."),
        ("ما أقرب نجم إلى الأرض؟", ["الشعرى اليمانية", "الشمس", "النجم القطبي", "سيريوس ب"], 1, "الشمس أقرب نجم إلى الأرض."),
        ("ما وحدة قياس القوة؟", ["الجول", "الواط", "النيوتن", "الأمبير"], 2, "تقاس القوة بوحدة النيوتن."),
        ("كم عدد حجرات قلب الإنسان؟", ["حجرتان", "ثلاث", "أربع", "خمس"], 2, "يتكون القلب من أذينين وبطينين."),
        ("ما الجزء الذي يحمل المعلومات الوراثية؟", ["DNA", "النشا", "الهيموغلوبين", "الكالسيوم"], 0, "يحمل الحمض النووي DNA معظم المعلومات الوراثية."),
        ("عند أي درجة مئوية يتجمد الماء النقي تقريبًا؟", ["0", "10", "50", "100"], 0, "يتجمد الماء النقي عند صفر مئوية في الضغط المعتاد."),
        ("أي عضو يضخ الدم؟", ["الرئة", "القلب", "الكبد", "المعدة"], 1, "يضخ القلب الدم إلى أنحاء الجسم."),
        ("ما الكوكب الأكبر في المجموعة الشمسية؟", ["الأرض", "المشتري", "نبتون", "عطارد"], 1, "المشتري هو أكبر كواكب المجموعة الشمسية."),
    ]),
    "geography": ("🗺️ الجغرافيا", [
        ("ما عاصمة اليابان؟", ["كيوتو", "طوكيو", "أوساكا", "ناغويا"], 1, "طوكيو عاصمة اليابان."),
        ("ما أكبر محيط في العالم؟", ["الأطلسي", "الهندي", "الهادئ", "المتجمد الشمالي"], 2, "المحيط الهادئ هو الأكبر مساحة."),
        ("في أي قارة تقع الأرجنتين؟", ["آسيا", "أوروبا", "أمريكا الجنوبية", "إفريقيا"], 2, "تقع الأرجنتين في أمريكا الجنوبية."),
        ("ما عاصمة المملكة العربية السعودية؟", ["جدة", "مكة المكرمة", "الرياض", "الدمام"], 2, "الرياض عاصمة المملكة العربية السعودية."),
        ("أي نهر يمر بمدينة القاهرة؟", ["الفرات", "النيل", "دجلة", "الأردن"], 1, "يمر نهر النيل بالقاهرة."),
        ("ما أكبر قارة من حيث المساحة؟", ["إفريقيا", "آسيا", "أوروبا", "أستراليا"], 1, "آسيا أكبر قارات العالم مساحة."),
        ("ما البحر الذي يقع غرب المملكة العربية السعودية؟", ["بحر العرب", "البحر الأحمر", "بحر قزوين", "البحر الأسود"], 1, "يقع البحر الأحمر غرب المملكة."),
        ("ما عاصمة مصر؟", ["الإسكندرية", "القاهرة", "الأقصر", "أسوان"], 1, "القاهرة عاصمة مصر."),
    ]),
    "history": ("🏺 التاريخ", [
        ("في أي دولة تقع الأهرامات الشهيرة في الجيزة؟", ["السودان", "مصر", "العراق", "المغرب"], 1, "تقع أهرامات الجيزة في مصر."),
        ("ما الحضارة القديمة التي نشأت بين دجلة والفرات؟", ["حضارة بلاد الرافدين", "حضارة المايا", "الحضارة الإغريقية", "حضارة الإنكا"], 0, "نشأت حضارات بلاد الرافدين بين نهري دجلة والفرات."),
        ("ما اسم الطريق التجاري التاريخي الذي ربط الشرق بالغرب؟", ["طريق الحرير", "طريق التوابل فقط", "طريق القوافل الشمالية", "طريق الذهب"], 0, "كان طريق الحرير شبكة طرق للتجارة والتبادل الثقافي."),
        ("في أي مدينة يقع الكولوسيوم؟", ["أثينا", "روما", "إسطنبول", "باريس"], 1, "يقع الكولوسيوم في روما بإيطاليا."),
        ("ما اسم الكتابة المصرية القديمة؟", ["المسمارية", "الهيروغليفية", "اللاتينية", "السنسكريتية"], 1, "استُخدمت الكتابة الهيروغليفية في مصر القديمة."),
        ("أين بدأت الألعاب الأولمبية القديمة؟", ["اليونان", "الصين", "مصر", "روما"], 0, "بدأت الألعاب الأولمبية القديمة في اليونان."),
        ("ما الحضارة التي بنت مدينة ماتشو بيتشو؟", ["الإنكا", "الرومان", "الفراعنة", "الفينيقيون"], 0, "ترتبط ماتشو بيتشو بحضارة الإنكا في الأنديز."),
        ("ما المدينة التي كانت مركزًا مهمًا للحضارة الأندلسية؟", ["قرطبة", "لندن", "برلين", "أوسلو"], 0, "كانت قرطبة مركزًا علميًا وثقافيًا بارزًا في الأندلس."),
    ]),
    "islamic": ("📚 الثقافة الإسلامية", [
        ("كم عدد أركان الإسلام؟", ["أربعة", "خمسة", "ستة", "سبعة"], 1, "أركان الإسلام خمسة."),
        ("ما الشهر الذي يصومه المسلمون؟", ["شعبان", "رمضان", "شوال", "محرم"], 1, "يصوم المسلمون شهر رمضان."),
        ("ما قبلة المسلمين في الصلاة؟", ["المسجد الأقصى", "الكعبة المشرفة", "المسجد النبوي", "جبل عرفات"], 1, "الكعبة المشرفة قبلة المسلمين في الصلاة."),
        ("كم عدد الصلوات المفروضة في اليوم والليلة؟", ["ثلاث", "أربع", "خمس", "ست"], 2, "الصلوات المفروضة خمس."),
        ("ما اسم الكتاب الذي أُنزل على النبي محمد ﷺ؟", ["التوراة", "الزبور", "الإنجيل", "القرآن الكريم"], 3, "القرآن الكريم أُنزل على النبي محمد ﷺ."),
        ("في أي مدينة يوجد المسجد النبوي؟", ["مكة المكرمة", "المدينة المنورة", "القدس", "الطائف"], 1, "يقع المسجد النبوي في المدينة المنورة."),
        ("ما اسم الليلة التي هي خير من ألف شهر؟", ["ليلة النصف من شعبان", "ليلة القدر", "ليلة العيد", "ليلة الإسراء"], 1, "ورد فضل ليلة القدر في سورة القدر."),
        ("ما السورة التي تُقرأ في كل ركعة من الصلاة؟", ["الفاتحة", "الإخلاص فقط", "الناس", "الكوثر"], 0, "قراءة الفاتحة ركن في الصلاة عند جمهور العلماء."),
    ]),
    "sports": ("⚽ الرياضة", [
        ("كم لاعبًا يبدأ به فريق كرة القدم داخل الملعب؟", ["9", "10", "11", "12"], 2, "يتكون الفريق من 11 لاعبًا داخل الملعب عادةً."),
        ("كم عدد الحلقات في شعار الألعاب الأولمبية؟", ["أربع", "خمس", "ست", "سبع"], 1, "يتضمن الشعار خمس حلقات متداخلة."),
        ("في أي رياضة تُستخدم الريشة؟", ["التنس", "الريشة الطائرة", "الاسكواش", "البيسبول"], 1, "تُستخدم الريشة في لعبة الريشة الطائرة."),
        ("كم نقطة تُحتسب للرمية الحرة في كرة السلة؟", ["نقطة واحدة", "نقطتان", "ثلاث نقاط", "أربع نقاط"], 0, "تُحتسب الرمية الحرة الناجحة بنقطة واحدة."),
        ("ما الرياضة التي يُستخدم فيها مصطلح ضربة إرسال؟", ["التنس", "المصارعة", "الجري", "رفع الأثقال"], 0, "ضربة الإرسال بداية النقطة في التنس."),
        ("كم شوطًا في مباراة كرة القدم العادية قبل الوقت الإضافي؟", ["شوط واحد", "شوطان", "ثلاثة", "أربعة"], 1, "المباراة تتكون من شوطين أساسيين."),
        ("ما الأداة الأساسية في لعبة الغولف لضرب الكرة؟", ["مضرب الغولف", "مضرب بيسبول", "مجداف", "مضرب تنس الطاولة"], 0, "تُستخدم مضارب مخصصة لضرب كرة الغولف."),
        ("في أي رياضة يوجد المسبح ومسارات السباحة؟", ["السباحة", "الرماية", "الفروسية", "الجودو"], 0, "تقام مسابقات السباحة في أحواض مخصصة."),
    ]),
    "tech": ("💻 التقنية", [
        ("ما وظيفة نظام GPS الأساسية؟", ["تحديد الموقع", "طباعة الأوراق", "تبريد الجهاز", "تحرير الفيديو فقط"], 0, "يُستخدم GPS لتحديد الموقع والملاحة."),
        ("ماذا تعني WWW في عناوين الويب؟", ["الشبكة العالمية", "نظام التشغيل", "ذاكرة عشوائية", "بريد إلكتروني"], 0, "WWW اختصار World Wide Web."),
        ("أي مما يلي كلمة مرور أقوى؟", ["123456", "password", "اسمك فقط", "عبارة طويلة وفريدة"], 3, "العبارة الطويلة والفريدة أصعب في التخمين عادةً."),
        ("ما الجهاز الذي يربط الشبكة المنزلية بالإنترنت غالبًا؟", ["الموجّه Router", "الطابعة", "الماسح الضوئي", "السماعة"], 0, "يوجه الموجّه حركة البيانات بين الشبكات."),
        ("ما المقصود بالنسخ الاحتياطي؟", ["إنشاء نسخة لاستعادة البيانات", "حذف الملفات", "زيادة سطوع الشاشة", "إيقاف الإنترنت"], 0, "يساعد النسخ الاحتياطي على استعادة البيانات عند فقدها."),
        ("أي مما يلي مثال على مصادقة متعددة العوامل؟", ["كلمة مرور فقط", "كلمة مرور ورمز تحقق", "اسم المستخدم فقط", "إعادة تشغيل الجهاز"], 1, "تجمع المصادقة متعددة العوامل بين أكثر من طريقة تحقق."),
        ("ما الامتداد الشائع لملفات الصور؟", [".jpg", ".mp3", ".exe", ".py"], 0, "JPG امتداد شائع للصور."),
        ("ما فائدة تحديث البرامج؟", ["إصلاح أخطاء وتحسين الأمان", "إلغاء كل الملفات دائمًا", "تعطيل الشاشة", "زيادة حجم الجهاز"], 0, "تتضمن التحديثات إصلاحات وتحسينات أمنية ووظيفية."),
    ]),
    "arabic": ("🔤 اللغة العربية", [
        ("ما جمع كلمة مفتاح؟", ["مفاتيح", "مفتاحات", "مفتاحون", "مفتاحين فقط"], 0, "جمع مفتاح هو مفاتيح."),
        ("ما ضد كلمة شجاع؟", ["جريء", "جبان", "قوي", "مقدام"], 1, "ضد شجاع: جبان."),
        ("أي كلمة من التالية فعل؟", ["المدرسة", "يكتب", "القلم", "جميل"], 1, "يكتب فعل مضارع."),
        ("ما نوع كلمة (سريع) في جملة: عدّاء سريع؟", ["اسم", "فعل", "صفة", "حرف جر"], 2, "سريع صفة تصف العدّاء."),
        ("ما جمع كلمة كتاب؟", ["كتابات فقط", "كاتبون", "كتب", "مكتبة"], 2, "جمع كتاب هو كتب."),
        ("أي كلمة تبدأ بهمزة قطع؟", ["استعمال", "ابن في وسط الكلام", "أحمد", "اسم"], 2, "أحمد تبدأ بهمزة قطع."),
        ("ما المثنى من كلمة طالب؟", ["طلاب", "طالبان", "طالبات", "طلبة"], 1, "مثنى طالب: طالبان في حالة الرفع."),
        ("أي جملة استفهامية؟", ["الجو جميل", "افتح الباب", "أين الكتاب؟", "ما أجمل السماء"], 2, "أين الكتاب؟ جملة استفهامية."),
    ]),
    "math": ("➗ الرياضيات", [
        ("كم ناتج 9 × 7؟", ["56", "63", "72", "79"], 1, "9 × 7 = 63."),
        ("ما نصف العدد 86؟", ["42", "43", "46", "48"], 1, "نصف 86 يساوي 43."),
        ("كم ضلعًا للمثلث؟", ["2", "3", "4", "5"], 1, "للمثلث ثلاثة أضلاع."),
        ("كم يساوي 25% من 200؟", ["25", "40", "50", "75"], 2, "25% ربع العدد، وربع 200 يساوي 50."),
        ("ما العدد التالي: 2، 4، 6، 8، ...؟", ["9", "10", "11", "12"], 1, "النمط يزيد بمقدار 2؛ التالي 10."),
        ("كم ثانية في دقيقتين؟", ["60", "100", "120", "180"], 2, "الدقيقتان تساويان 120 ثانية."),
        ("ما محيط مربع طول ضلعه 5؟", ["10", "15", "20", "25"], 2, "محيط المربع يساوي أربعة أضعاف طول الضلع: 20."),
        ("ما ناتج 144 ÷ 12؟", ["10", "11", "12", "14"], 2, "144 ÷ 12 = 12."),
    ]),
    "saudi": ("🇸🇦 المملكة العربية السعودية", [
        ("ما عاصمة المملكة العربية السعودية؟", ["جدة", "الرياض", "الدمام", "أبها"], 1, "الرياض عاصمة المملكة العربية السعودية."),
        ("ما العملة الرسمية في السعودية؟", ["الدينار", "الدرهم", "الريال السعودي", "الجنيه"], 2, "العملة الرسمية هي الريال السعودي."),
        ("ما البحر الذي يقع غرب السعودية؟", ["البحر الأحمر", "بحر قزوين", "البحر الأسود", "بحر البلطيق"], 0, "يقع البحر الأحمر غرب المملكة."),
        ("ما اسم رؤية المملكة لعام 2030؟", ["رؤية السعودية 2030", "خطة 2040", "برنامج 2020", "رؤية الخليج 2050"], 0, "اسم البرنامج الوطني هو رؤية السعودية 2030."),
        ("في أي مدينة يقع المسجد النبوي؟", ["مكة المكرمة", "المدينة المنورة", "الرياض", "الطائف"], 1, "يقع المسجد النبوي في المدينة المنورة."),
        ("ما اسم اليوم الوطني السعودي؟", ["يوم التأسيس فقط", "اليوم الوطني", "يوم العلم فقط", "يوم الوحدة العربية"], 1, "يُحتفل باليوم الوطني السعودي في 23 سبتمبر."),
        ("ما المنطقة التي تقع فيها تبوك؟", ["شمال غرب المملكة", "جنوب شرق المملكة", "وسط اليمن", "شرق عُمان"], 0, "تقع منطقة تبوك في شمال غرب المملكة."),
        ("ما اسم البحر الذي تطل عليه جدة؟", ["الخليج العربي", "البحر الأحمر", "بحر العرب", "البحر المتوسط"], 1, "تقع جدة على ساحل البحر الأحمر."),
    ]),
    "logic": ("🧩 ألغاز المنطق", [
        ("إذا كان اليوم الاثنين، فما اليوم بعد يومين؟", ["الثلاثاء", "الأربعاء", "الخميس", "الجمعة"], 1, "بعد يومين من الاثنين يأتي الأربعاء."),
        ("لديك 3 تفاحات وأخذت واحدة، كم تفاحة معك؟", ["واحدة", "اثنتان", "ثلاث", "صفر"], 0, "أخذت واحدة، إذن معك تفاحة واحدة."),
        ("أي عدد لا ينتمي للمجموعة: 2، 4، 6، 9، 8؟", ["2", "4", "9", "8"], 2, "الأعداد الأخرى زوجية، أما 9 فردي."),
        ("أيهما أثقل: كيلو حديد أم كيلو قطن؟", ["الحديد", "القطن", "متساويان", "لا يمكن معرفة ذلك"], 2, "كلاهما يزن كيلوغرامًا واحدًا."),
        ("إذا كانت كل الورود نباتات، فهل كل النباتات ورود؟", ["نعم دائمًا", "لا يلزم", "لا توجد نباتات", "كلها أشجار"], 1, "كون الورود نباتات لا يعني أن كل النباتات ورود."),
        ("ما العدد التالي: 1، 1، 2، 3، 5، ...؟", ["6", "7", "8", "10"], 2, "في متتالية فيبوناتشي، التالي 3 + 5 = 8."),
        ("لدى أسرة 4 بنات ولكل بنت أخ واحد مشترك، كم عدد الأبناء؟", ["4", "5", "8", "9"], 1, "الأخ واحد مشترك بين البنات الأربع، فيكون العدد 5 أبناء."),
        ("أي شهر يحتوي 28 يومًا على الأقل؟", ["فبراير فقط", "يناير فقط", "كل الشهور", "لا شهر"], 2, "كل الشهور تحتوي 28 يومًا على الأقل."),
    ]),
    "digital_safety": ("🛡️ الأمان الرقمي", [
        ("ما التصرف الأفضل عند وصول رابط مجهول يطلب كلمة المرور؟", ["فتحه فورًا", "إرساله للجميع", "عدم فتحه والتحقق من المصدر", "إدخال البيانات للتجربة"], 2, "تحقق من المصدر ولا تدخل بياناتك عبر رابط مشبوه."),
        ("أين يُفضّل حفظ رمز التحقق لمرة واحدة؟", ["إرساله للآخرين", "إبقاؤه سريًا وعدم مشاركته", "نشره في المجموعة", "كتابته في تعليق عام"], 1, "رموز التحقق سرية ولا تُشارك مع أي شخص."),
        ("ما فائدة المصادقة الثنائية؟", ["إضافة طبقة حماية", "إلغاء كلمة المرور دائمًا", "زيادة الإعلانات", "إبطاء الهاتف فقط"], 0, "تضيف وسيلة تحقق أخرى لحماية الحساب."),
        ("ما أفضل كلمة مرور؟", ["12345678", "اسمك وتاريخ ميلادك", "عبارة طويلة وفريدة", "كلمة password"], 2, "استخدم كلمة مرور طويلة وفريدة لكل حساب."),
        ("ماذا تفعل عند الاشتباه باختراق حسابك؟", ["تجاهل الأمر", "تغيير كلمة المرور وتأمين الحساب", "إرسال الرمز للمشتبه به", "حذف كل التنبيهات فقط"], 1, "غيّر كلمة المرور من الموقع الرسمي وراجع جلسات الدخول."),
        ("هل ينبغي تثبيت التطبيقات من مصادر مجهولة؟", ["نعم دائمًا", "فقط إذا كان اسمها مشهورًا", "لا، استخدم المتاجر والمصادر الرسمية", "أرسلها لصديق أولًا"], 2, "المصادر الرسمية تقلل خطر التطبيقات الضارة."),
        ("ما معنى النسخة الاحتياطية؟", ["نسخة لاستعادة البيانات", "إعلان منبثق", "حذف الملفات نهائيًا", "تغيير خلفية الهاتف"], 0, "تساعد النسخ الاحتياطية على استعادة البيانات عند فقدها."),
        ("أي معلومة لا ينبغي نشرها علنًا؟", ["نصيحة عامة", "كلمة المرور ورمز التحقق", "رابط حكومي رسمي", "موعد عام"], 1, "لا تنشر كلمات المرور أو رموز التحقق أو البيانات الحساسة."),
    ]),
    "daily_life": ("🌟 معلومات الحياة اليومية", [
        ("كم دقيقة في الساعة؟", ["30", "45", "60", "100"], 2, "الساعة تساوي 60 دقيقة."),
        ("كم يومًا في الأسبوع؟", ["5", "6", "7", "8"], 2, "الأسبوع سبعة أيام."),
        ("ما الوحدة الشائعة لقياس كتلة الأجسام؟", ["اللتر", "الكيلوغرام", "المتر", "الثانية"], 1, "الكيلوغرام وحدة لقياس الكتلة."),
        ("أي وسيلة تساعد على تقليل فقدان الملفات؟", ["عدم الحفظ", "نسخ احتياطي دوري", "إغلاق الجهاز أثناء الحفظ", "حذف النسخ"], 1, "احتفظ بنسخة احتياطية محدثة في مكان آمن."),
        ("ما أول خطوة عند استخدام خدمة حكومية إلكترونية؟", ["فتح رابط عشوائي", "التأكد من الرابط الرسمي", "مشاركة كلمة المرور", "إرسال رمز التحقق"], 1, "تأكد من عنوان الموقع الرسمي قبل تسجيل الدخول."),
        ("ما التصرف الأنسب عند نسيان موعد مهم؟", ["عدم تنظيم المواعيد", "استخدام تقويم أو تذكير", "الاعتماد على الذاكرة فقط", "حذف التنبيهات"], 1, "التقويم والتذكيرات تساعد على عدم تفويت المواعيد."),
        ("أي مما يلي يحافظ على بطارية الهاتف غالبًا؟", ["رفع السطوع دائمًا", "ترك التطبيقات غير اللازمة تعمل دائمًا", "تقليل السطوع عند الحاجة", "تشغيل كل الاتصالات بلا حاجة"], 2, "تقليل السطوع وإيقاف الميزات غير اللازمة قد يوفّر الطاقة."),
        ("ما أفضل طريقة للتعامل مع خبر غير مؤكد؟", ["نشره فورًا", "التحقق من مصدر موثوق", "تغيير عنوانه", "اعتباره صحيحًا تلقائيًا"], 1, "تحقق من المصادر الموثوقة قبل مشاركة الأخبار."),
    ]),
}

WORD_BANK = [
    ("مدرسة", "مكان يتعلم فيه الطلاب"),
    ("مكتبة", "مكان توجد فيه الكتب"),
    ("حاسوب", "جهاز لمعالجة البيانات"),
    ("بحر", "مسطح مائي واسع ومالح"),
    ("نخلة", "شجرة تشتهر بإنتاج التمر"),
    ("قمر", "جرم يدور حول الأرض"),
    ("طائرة", "وسيلة نقل تطير في الجو"),
    ("مفتاح", "أداة لفتح الأقفال"),
    ("ساعة", "أداة لمعرفة الوقت"),
    ("حديقة", "مكان فيه نباتات وأشجار"),
]

MEMBER_GAMES = [
    ("🧠 أسئلة ثقافية وذكاء", "game_quiz"), ("🔤 تحدي الكلمات", "game_word"),
    ("🧩 فك الكلمات المبعثرة", "game_scramble"), ("✊ حجر ورقة مقص", "game_rps"),
    ("⭕ إكس أو لاعبين", "game_xo_start"), ("⚡ تحدي السرعة", "game_speed"),
    ("🎯 تحدي اليوم", "game_daily"), ("🏆 المتصدرون", "game_scores"),
]

ADMIN_GAMES = [
    ("🧠 الأسئلة الثقافية", "game_quiz"), ("🔤 تحدي الكلمات", "game_word"),
    ("🧩 الكلمات المبعثرة", "game_scramble"), ("✊ حجر ورقة مقص", "game_rps"),
    ("⭕ إكس أو", "game_xo_start"), ("⚡ تحدي السرعة", "game_speed"),
    ("🎯 تحدي اليوم", "game_daily"), ("🎯 تحدي الخاسر", "game_challenge"),
    ("🏆 المتصدرون", "game_scores"), ("📅 أسبوعي/شهري", "game_periods"),
]

PERSISTENT_GAMES = {
    "kingdom": {"title": "🌍 مملكة القروب", "description": "طوّر مملكتك وتقدم بمراحل محفوظة."},
    "hero": {"title": "⚔️ مغامرة البطل", "description": "تقدم في مغامرة وتحديات محفوظة."},
    "cards": {"title": "🃏 ألبوم المقتنيات", "description": "اجمع المقتنيات وافتح عناصر جديدة."},
    "alliance": {"title": "🏰 تحالفات القروب", "description": "أنشئ تحالفات وتنافس مع الأعضاء."},
    "case": {"title": "🕵️ ملف القضية", "description": "تابع قضية مستمرة وحاول حل ألغازها."},
    "season": {"title": "🏆 موسم التحديات", "description": "شارك في موسم واحتفظ بتقدمك."},
}


async def send_quiz_stage(context, chat_id, category, stage):
    """ينشر مرحلة من مسابقة من خمس مراحل مع تصحيح محفوظ في قاعدة البيانات."""
    if category not in QUIZ_CATEGORIES or stage < 1 or stage > 5:
        return None
    title, questions = QUIZ_CATEGORIES[category]
    # Rotate the offline bank: avoid repeating questions from the latest rounds
    # in this group/category until the available questions have been used.
    recent_rows = db.execute(
        "SELECT answer FROM game_rounds WHERE chat_id=? AND kind='quiz_stage' ORDER BY created_at DESC LIMIT 20",
        (chat_id,),
    ).fetchall()
    recent_indices = set()
    for (stored_answer,) in recent_rows:
        try:
            stored_category, _stored_stage, stored_index, _stored_correct = stored_answer.split("|")
            if stored_category == category:
                recent_indices.add(int(stored_index))
        except (AttributeError, ValueError):
            continue
    candidates = [i for i in range(len(questions)) if i not in recent_indices] or list(range(len(questions)))
    question_index = random.choice(candidates)
    question, options, correct_index, explanation = questions[question_index]
    token = uuid.uuid4().hex[:10]
    answer_data = f"{category}|{stage}|{question_index}|{correct_index}"
    db.execute("INSERT INTO game_rounds(token,chat_id,kind,answer,completed,created_at) VALUES(?,?,?,?,0,?)",
               (token, chat_id, "quiz_stage", answer_data, now()))
    db.commit()
    rows = [[InlineKeyboardButton(f"{chr(65+i)}. {option}", callback_data=f"game_stage_answer:{token}:{i}")]
            for i, option in enumerate(options)]
    await context.bot.send_message(chat_id,
        f"🏁 المسابقة متعددة المراحل — {title}\nالمرحلة {stage} من 5\n\n{question}\n\nاختر الإجابة الصحيحة لتحصل على نقاط وتفتح المرحلة التالية.",
        reply_markup=InlineKeyboardMarkup(rows))
    return token

def ensure_settings(chat_id):
    cols = ", ".join(f'"{f}" INTEGER DEFAULT {DEFAULTS[f]}' for f in SETTING_FIELDS)
    # Migrate old schemas without ever dropping the original settings table.
    existing = db.execute("PRAGMA table_info(settings)").fetchall()
    existing_names = {row[1] for row in existing}
    if existing and "chat_id" not in existing_names:
        legacy_table = "settings_legacy_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        legacy_names = set(existing_names)
        db.execute(f'ALTER TABLE settings RENAME TO "{legacy_table}"')
        db.execute(f"CREATE TABLE settings(chat_id INTEGER PRIMARY KEY, {cols})")
        # A legacy table without chat_id can only represent global settings.
        # Preserve it intact, and copy its first row as a baseline for known groups.
        common_fields = [field for field in SETTING_FIELDS if field in legacy_names]
        if common_fields:
            select_fields = ", ".join(f'"{field}"' for field in common_fields)
            legacy_row = db.execute(f'SELECT {select_fields} FROM "{legacy_table}" LIMIT 1').fetchone()
            if legacy_row:
                legacy_values = dict(zip(common_fields, legacy_row))
                targets = {0}
                try:
                    targets.update(int(row[0]) for row in db.execute("SELECT chat_id FROM watched_groups").fetchall())
                except sqlite3.DatabaseError:
                    pass
                field_sql = ", ".join(f'"{field}"' for field in SETTING_FIELDS)
                placeholders = ", ".join("?" for _ in SETTING_FIELDS)
                for target_chat_id in targets:
                    values = [int(bool(legacy_values.get(field, DEFAULTS[field]))) for field in SETTING_FIELDS]
                    db.execute(f"INSERT OR IGNORE INTO settings(chat_id, {field_sql}) VALUES(?, {placeholders})",
                               [target_chat_id, *values])
        db.commit()
        log.warning("Migrated legacy settings schema without deleting original table; preserved as %s", legacy_table)
        existing_names = {"chat_id", *SETTING_FIELDS}
    elif not existing:
        db.execute(f"CREATE TABLE settings(chat_id INTEGER PRIMARY KEY, {cols})")
        db.commit()
        existing_names = {"chat_id", *SETTING_FIELDS}
    for field in SETTING_FIELDS:
        if field not in existing_names:
            db.execute(f'ALTER TABLE settings ADD COLUMN "{field}" INTEGER DEFAULT {DEFAULTS[field]}')
    row = db.execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        # Explicit values are required because older SQLite databases keep their original column defaults.
        fields_sql = ", ".join(f'"{field}"' for field in SETTING_FIELDS)
        placeholders = ", ".join("?" for _ in SETTING_FIELDS)
        values = [int(bool(DEFAULTS[field])) for field in SETTING_FIELDS]
        db.execute(f"INSERT INTO settings(chat_id, {fields_sql}) VALUES(?, {placeholders})", [chat_id, *values])
        db.commit()

# Migrate the small settings table used by the first version.
ensure_settings(0)
if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
    cols = {r[1] for r in db.execute("PRAGMA table_info(settings)").fetchall()}
    for field in SETTING_FIELDS:
        if field not in cols:
            db.execute(f'ALTER TABLE settings ADD COLUMN "{field}" INTEGER DEFAULT {DEFAULTS[field]}')
    db.commit()

# Apply the requested default protection to groups already registered, once only.
db.execute("CREATE TABLE IF NOT EXISTS bot_migrations (migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
if not db.execute("SELECT 1 FROM bot_migrations WHERE migration_key=?", ("default_media_link_protection_v1",)).fetchone():
    for (existing_chat_id,) in db.execute("SELECT chat_id FROM watched_groups").fetchall():
        ensure_settings(existing_chat_id)
        db.execute("UPDATE settings SET links=1, photos=1, videos=1 WHERE chat_id=?", (existing_chat_id,))
    db.execute("INSERT INTO bot_migrations(migration_key, applied_at) VALUES(?,?)", ("default_media_link_protection_v1", now()))
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
    """النشر للمالك/المفوّض، أو للموظف الذي منحه المالك صلاحية محددة."""
    if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
        return False
    if is_owner(uid) or is_delegated_owner(uid):
        return True
    return can_use_capability(uid, "publish", chat_id)

async def can_publish_to_live(bot, uid, chat_id):
    """Compatibility wrapper around the isolated live publishing access service."""
    return await _service_can_publish_to_live(
        bot, uid, chat_id,
        can_publish=can_publish_to,
        bad_request_type=BadRequest,
        forbidden_type=Forbidden,
        logger=log,
    )

def can_use_capability(uid, capability, chat_id=0):
    """Compatibility wrapper; permission policy lives in services.permissions."""
    return _service_can_use_capability(db, OWNER_ID, uid, capability, chat_id)


def capability_group_ids(uid, capability):
    """Compatibility wrapper; scope resolution lives in services.permissions."""
    return _service_capability_group_ids(db, OWNER_ID, uid, capability)


_telegram_admin_cache = {}

async def is_telegram_group_admin(bot, uid, chat_id):
    """تحقق من صلاحية مشرف Telegram مع ذاكرة مؤقتة قصيرة لتجنب طلب API لكل رسالة."""
    key = (int(chat_id), int(uid))
    now_mono = time.monotonic()
    cached = _telegram_admin_cache.get(key)
    if cached and now_mono - cached[0] < 45:
        return cached[1]
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
        result = member.status in ("creator", "administrator")
        _telegram_admin_cache[key] = (now_mono, result)
        return result
    except Exception as exc:
        log.debug("تعذر التحقق من مشرف القروب %s في %s: %s", uid, chat_id, exc)
        return False

async def can_access_group(bot, uid, chat_id):
    """الوصول لقروب محدد فقط؛ مشرف Telegram يدير قروبه ولا يرث صلاحيات مالك البوت."""
    if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
        return False
    if not is_owner(uid) and not external_group_active(chat_id):
        return False
    if is_owner(uid):
        return True
    if is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
        return True
    if group_role(chat_id, uid):
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
            return member.status not in ("left", "kicked")
        except Exception:
            return False
    return await is_telegram_group_admin(bot, uid, chat_id)

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
    # صلاحيات المستخدمين الممنوحة من المالك تفتح لوحة محدودة بالميزات المصرح بها فقط.
    has_capability = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? LIMIT 1", (uid,)).fetchone())
    return is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid) or has_capability

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
    "مشرف": {"delete", "mute", "unmute"},
    "مشرف عام": {"delete", "mute", "unmute", "kick", "ban", "unban", "pin", "protection"},
    "مدير": {"delete", "mute", "unmute", "kick", "ban", "unban", "pin", "protection"},
    "مدير عام": {"delete", "mute", "unmute", "kick", "ban", "unban", "pin", "protection", "manage_roles", "logs", "welcome", "stats"},
}


def group_role(chat_id, uid):
    row = db.execute("SELECT role FROM group_roles WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
    return row[0] if row else ""


def has_group_permission(chat_id, uid, permission):
    # The primary/delegated owner and general manager can manage group operations.
    # A protection manager is deliberately limited to protection controls; the role
    # must not implicitly grant member moderation, deletion, or role-management.
    if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid):
        return True
    if is_protection_manager(uid):
        return permission == "protection"
    if permission == "protection" and can_use_capability(uid, "protection", chat_id):
        return True
    if permission == "tag" and can_use_capability(uid, "tag", chat_id):
        return True
    if permission == "delete" and can_use_capability(uid, "bulk_delete", chat_id):
        return True
    return permission in GROUP_ROLE_PERMISSIONS.get(group_role(chat_id, uid), set())


async def require_group_permission(update, permission, message=None):
    if not await require_admin(update):
        return False
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    external_group_permission = False
    ext = external_group_info(chat_id)
    if ext and ext[0] and external_group_active(chat_id):
        cap = "protection" if permission == "protection" else ("tag" if permission == "tag" else "members")
        external_group_permission = external_group_cap_enabled(chat_id, cap) and await is_telegram_group_admin(update.get_bot(), uid, chat_id)
    if has_group_permission(chat_id, uid, permission) or external_group_permission:
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
    chat_id = update.effective_chat.id
    if not external_group_active(chat_id) and not is_owner(uid):
        await update.effective_message.reply_text("⛔ خدمات البوت متوقفة عن هذا القروب من المالك الأساسي.")
        return False
    has_internal_role = (is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)
                         or bool(group_role(chat_id, uid))
                         or can_use_capability(uid, "protection", chat_id)
                         or can_use_capability(uid, "bulk_delete", chat_id))
    telegram_admin = await is_telegram_group_admin(update.get_bot(), uid, chat_id)
    ext = external_group_info(chat_id)
    if ext and ext[0] and not is_owner(uid):
        command_text = (update.effective_message.text or "").strip().lower()
        command_name = command_text.split()[0].split("@")[0] if command_text.startswith("/") else ""
        member_commands = {"/ban","/unban","/kick","/mute","/unmute","/warn","/muted","/unmuteall","/del","/pin","/badges","/badge_remove","/givepoints","/setmembertitle"}
        if command_name in member_commands and not external_group_cap_enabled(chat_id, "members"):
            await update.effective_message.reply_text("⛔ إدارة الأعضاء غير مفعّلة لهذا القروب."); return False
        protection_commands = {"/locks","/settings","/archive_on","/archive_off","/archive"}
        arabic_lock_text = normalized_arabic_text(command_text)
        arabic_lock_aliases = {normalized_arabic_text(alias) for alias in ARABIC_ALIASES}
        if (command_name in protection_commands or arabic_lock_text in arabic_lock_aliases) and not external_group_cap_enabled(chat_id, "protection"):
            await update.effective_message.reply_text("⛔ الحماية غير مفعّلة لهذا القروب."); return False
    external_protection_ok = external_group_cap_enabled(chat_id, "protection")
    if not has_internal_role and not (telegram_admin and external_protection_ok):
        await update.effective_message.reply_text("⛔ ليس لديك صلاحية الإدارة في هذا القروب.")
        return False
    # أوامر الإشراف للمفوّض تعمل فقط داخل القروب الذي يرسل الأمر فيه.
    if is_delegated_owner(uid) and not is_owner(uid):
        if not await can_access_group(update.get_bot(), uid, update.effective_chat.id):
            await update.effective_message.reply_text("⛔ لا تملك صلاحية التحكم في هذا القروب.")
            return False
    return True

def register_group(chat, actor=None):
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    db.execute(
        "INSERT OR REPLACE INTO watched_groups(chat_id,title,added_at) "
        "VALUES(?,?,COALESCE((SELECT added_at FROM watched_groups WHERE chat_id=?),?))",
        (chat.id, chat.title or "", chat.id, now())
    )
    actor_id = int(getattr(actor, "id", 0) or 0)
    actor_name = getattr(actor, "full_name", "") or ""
    actor_username = getattr(actor, "username", "") or ""
    # First registration is treated as external unless the primary owner is the actor.
    db.execute("""INSERT OR IGNORE INTO external_group_control
        (chat_id,title,is_external,added_by,added_by_name,added_by_username,enabled,ai_enabled,created_at,updated_at)
        VALUES(?,?,?,?,?,?,1,0,?,?)""",
        (chat.id, chat.title or "", 0 if actor_id and actor_id == OWNER_ID else 1,
         actor_id, actor_name, actor_username, now(), now()))
    db.execute("UPDATE external_group_control SET title=? WHERE chat_id=? AND COALESCE(title,'')<>?", (chat.title or "", chat.id, chat.title or ""))
    # Enrich unknown actor metadata without reclassifying a group from an ordinary message.
    # Only the first trusted bot-membership event determines whether a newly registered group
    # is owner-managed or external; a later message from the bot owner must not silently convert
    # somebody else's group into an internal group.
    if actor_id:
        db.execute("""UPDATE external_group_control SET title=?, added_by=CASE WHEN added_by=0 THEN ? ELSE added_by END,
            added_by_name=CASE WHEN added_by_name='' THEN ? ELSE added_by_name END,
            added_by_username=CASE WHEN added_by_username='' THEN ? ELSE added_by_username END,
            updated_at=? WHERE chat_id=?""",
            (chat.title or "", actor_id, actor_name, actor_username, now(), chat.id))
    # Default external permissions: common group tools only. Sensitive tools stay disabled.
    for _cap, _enabled in (("tag",1),("protection",1),("members",1),("settings",1),("welcome",1),("stats",1),
                           ("publish",0),("ai",0),("executive",0),("mass_ban",0),("cross_group",0)):
        db.execute("""INSERT OR IGNORE INTO external_group_capabilities
            (chat_id,capability,enabled,granted_by,granted_at) VALUES(?,?,?,?,?)""",
            (chat.id, _cap, _enabled, OWNER_ID, now()))
    db.commit()
    # Create the settings row immediately so the secure default (links locked) applies from first use.
    ensure_settings(chat.id)
    # Install default auto-replies immediately when the group is first registered.
    # INSERT OR IGNORE preserves an explicit on/off choice made by an administrator.
    ensure_auto_replies(chat.id)


def external_group_info(chat_id):
    return db.execute("SELECT is_external,enabled,ai_enabled,title,added_by,added_by_name,added_by_username FROM external_group_control WHERE chat_id=?", (chat_id,)).fetchone()


def external_group_active(chat_id):
    row = db.execute("SELECT enabled FROM external_group_control WHERE chat_id=?", (chat_id,)).fetchone()
    return bool(row and row[0])


def external_group_cap_enabled(chat_id, capability):
    row = db.execute("SELECT is_external FROM external_group_control WHERE chat_id=?", (chat_id,)).fetchone()
    if not row or not row[0]:
        return True
    cap = db.execute("SELECT enabled FROM external_group_capabilities WHERE chat_id=? AND capability=?", (chat_id, capability)).fetchone()
    return bool(cap and cap[0])


def record_member_violation(chat_id, user, reason, abusive=False):
    uid = int(getattr(user, "id", 0) or 0)
    if not uid:
        return 0, 0
    row = db.execute("SELECT warning_count,abuse_count,reasons_json FROM member_violations WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
    count = int(row[0] or 0) + 1 if row else 1
    abuse_count = int(row[1] or 0) + (1 if abusive else 0) if row else (1 if abusive else 0)
    try:
        reasons = json.loads(row[2] or "[]") if row else []
    except Exception:
        reasons = []
    reasons.append({"reason": reason[:120], "at": now()})
    reasons = reasons[-50:]
    db.execute("""INSERT INTO member_violations(chat_id,user_id,display_name,username,warning_count,abuse_count,last_reason,reasons_json,last_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
        display_name=excluded.display_name, username=excluded.username, warning_count=excluded.warning_count,
        abuse_count=excluded.abuse_count, last_reason=excluded.last_reason, reasons_json=excluded.reasons_json, last_at=excluded.last_at""",
        (chat_id, uid, getattr(user, "full_name", "") or "عضو", getattr(user, "username", "") or "",
         count, abuse_count, reason[:120], json.dumps(reasons, ensure_ascii=False), now()))
    db.commit()
    return count, abuse_count


def format_public_user(user):
    name = html.escape((getattr(user, "full_name", "") or "عضو")[:100])
    mention = f'<a href="tg://user?id={int(user.id)}">{name}</a>'
    username = getattr(user, "username", "") or ""
    return mention + (f" (@{html.escape(username)})" if username else "")


def _mute_permissions():
    return ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
        can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
        can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
        can_add_web_page_previews=False)


async def register_warning_and_enforce(context, chat, user, reason, abusive=False):
    """Record a violation, publish an interactive warning card, and persist a 3-day mute at warning 3."""
    warning_count, abuse_count = record_member_violation(chat.id, user, reason, abusive=abusive)
    muted_until = None
    already_active = db.execute("SELECT muted_until,status FROM automatic_mutes WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
    decision = decide_warning_action(warning_count, bool(already_active and already_active[1] == 'active'))
    if decision.should_mute:
        until_dt = datetime.now(timezone.utc) + timedelta(days=decision.mute_days)
        muted_until = until_dt.isoformat(timespec='seconds')
        try:
            await context.bot.restrict_chat_member(chat_id=chat.id, user_id=user.id,
                permissions=_mute_permissions(), until_date=until_dt)
            db.execute("""INSERT INTO automatic_mutes(chat_id,user_id,muted_until,reason,warning_count,status,created_at,updated_at)
                VALUES(?,?,?,?,?,'active',?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
                muted_until=excluded.muted_until,reason=excluded.reason,warning_count=excluded.warning_count,
                status='active',updated_at=excluded.updated_at""",
                (chat.id,user.id,muted_until,reason[:200],warning_count,now(),now()))
            db.commit()
            log_action(chat.id, user.id, 'automatic_mute_3_days', f'warning_count={warning_count}; until={muted_until}; reason={reason[:150]}')
        except Exception as exc:
            log.warning("Automatic 3-day mute failed chat=%s user=%s (%s)", chat.id, user.id, type(exc).__name__)
            muted_until = None
    buttons = [
        [InlineKeyboardButton("🔎 مراجعة المخالفة", callback_data=f"warnreview:{chat.id}:{user.id}")],
        [InlineKeyboardButton("📋 سجل الإنذارات", callback_data=f"warnlog:{chat.id}:{user.id}")],
        [InlineKeyboardButton("↩️ إلغاء آخر إنذار", callback_data=f"warnremove_last:{chat.id}:{user.id}"),
         InlineKeyboardButton("🧹 تصفير الإنذارات", callback_data=f"warnclear:{chat.id}:{user.id}")],
    ]
    if is_owner(getattr(user, 'id', 0)):
        buttons = []
    body = (f"⚠️ <b>كرت إنذار رسمي</b>\n\n👤 العضو: {format_public_user(user)}\n"
            f"📝 السبب: {html.escape(reason[:250])}\n🔢 عدد الإنذارات المسجلة: <b>{warning_count}</b>\n")
    if warning_count >= 3:
        if muted_until:
            body += "\n🔇 <b>تم كتم العضو لمدة 3 أيام</b> بعد الإنذار الثالث.\n⏳ سيتم فك الكتم تلقائيًا عند انتهاء المدة."
        elif already_active and already_active[1] == 'active':
            body += "\n🔇 العضو مكتوم حاليًا؛ لم تُمدد مدة الكتم تلقائيًا."
        else:
            body += "\n⚠️ تعذر تنفيذ الكتم تلقائيًا؛ تم تسجيل المخالفة ويجب مراجعة صلاحيات البوت."
    try:
        await context.bot.send_message(chat_id=chat.id, text=body, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Warning card send failed chat=%s user=%s (%s)", chat.id, user.id, type(exc).__name__)
    return warning_count, abuse_count, bool(muted_until)


async def warn_cmd(update, context):
    """Manual warning by replying to a member or supplying their Telegram ID."""
    msg = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not msg or not chat or not actor:
        return
    if chat.type == ChatType.PRIVATE:
        args = list(context.args or [])
        if not (is_owner(actor.id) or is_delegated_owner(actor.id) or can_use_capability(actor.id, "protection", 0)):
            await msg.reply_text("⛔ منح الإنذارات من الخاص متاح للمالك أو المفوّض أو من مُنح صلاحية الحماية العامة.")
            return
        if len(args) < 2:
            await msg.reply_text("الصيغة من الخاص: /warn CHAT_ID USER_ID سبب_الإنذار")
            return
        try:
            target_chat_id, target_id = int(args[0]), int(args[1])
            if target_chat_id >= 0 or target_id <= 0:
                raise ValueError
        except (ValueError, TypeError):
            await msg.reply_text("تأكد من إدخال معرّف القروب ثم ID العضو. مثال: /warn -1001234567890 123456789 رابط مخالف")
            return
        if not (is_owner(actor.id) or is_delegated_owner(actor.id) or can_use_capability(actor.id, "protection", target_chat_id)):
            await msg.reply_text("⛔ ليس لديك صلاحية منح الإنذارات في هذا القروب.")
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (target_chat_id,)).fetchone():
            await msg.reply_text("القروب غير مسجل لدى البوت.")
            return
        reason = " ".join(args[2:])[:200] if len(args) > 2 else "مخالفة تعليمات القروب"
        try:
            member = await context.bot.get_chat_member(target_chat_id, target_id)
            target = member.user
        except Exception:
            from telegram import User
            target = User(id=target_id, first_name=f"ID {target_id}", is_bot=False)
        if target.is_bot or is_owner(target.id) or is_delegated_owner(target.id):
            await msg.reply_text("لا يمكن منح إنذار لحساب بوت أو للمالك/المفوّض.")
            return
        fake_chat = await context.bot.get_chat(target_chat_id)
        count, _, _ = await register_warning_and_enforce(context, fake_chat, target, reason, abusive=False)
        log_action(target_chat_id, actor.id, "manual_warning_private", f"target={target.id}; count={count}; reason={reason[:150]}")
        await msg.reply_text(f"✅ تم تسجيل الإنذار رقم {count} للعضو {target.id} في القروب {target_chat_id}.")
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("استخدم /warn من الخاص مع CHAT_ID وID العضو، أو داخل القروب بالرد على رسالته.")
        return
    if not await require_admin(update):
        return
    if not (is_owner(actor.id) or is_delegated_owner(actor.id) or is_general_manager(actor.id)
            or is_protection_manager(actor.id) or can_use_capability(actor.id, "protection", chat.id)
            or group_role(chat.id, actor.id) in {"مشرف", "مشرف عام", "مدير", "مدير عام"}):
        await msg.reply_text("⛔ منح الإنذارات متاح للمالك أو المفوّض أو الرتب المخوّلة بالإنذارات فقط.")
        return
    if external_group_info(chat.id) and external_group_info(chat.id)[0] and not is_owner(actor.id) and not external_group_cap_enabled(chat.id, "protection"):
        await msg.reply_text("⛔ صلاحية الحماية غير مفعّلة لهذا القروب الخارجي.")
        return
    target = None
    args = list(context.args or [])
    reason = "مخالفة تعليمات القروب"
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        if args:
            reason = " ".join(args)[:200]
    elif args:
        try:
            target_id = int(args[0])
            if target_id <= 0:
                raise ValueError
        except (ValueError, TypeError):
            await msg.reply_text("الصيغة: رد على رسالة العضو واكتب /warn السبب، أو اكتب /warn ID السبب.")
            return
        if len(args) > 1:
            reason = " ".join(args[1:])[:200]
        try:
            member = await context.bot.get_chat_member(chat.id, target_id)
            target = member.user
        except Exception:
            # A valid numeric ID can be warned even if Telegram cannot return the member profile.
            from telegram import User
            target = User(id=target_id, first_name=f"ID {target_id}", is_bot=False)
    else:
        await msg.reply_text("⚠️ لمنح إنذار: رد على رسالة العضو واكتب /warn السبب، أو استخدم /warn ID السبب.")
        return
    if target.is_bot:
        await msg.reply_text("لا يمكن منح إنذار لحساب بوت.")
        return
    if is_owner(target.id) or is_delegated_owner(target.id) or is_general_manager(target.id) or is_protection_manager(target.id):
        await msg.reply_text("⛔ لا يمكن منح إنذار للمالك أو الإدارة العليا من خلال هذه الأداة.")
        return
    count, _, muted = await register_warning_and_enforce(context, chat, target, reason, abusive=False)
    log_action(chat.id, actor.id, "manual_warning", f"target={target.id}; count={count}; reason={reason[:150]}")
    await msg.reply_text(f"✅ تم تسجيل الإنذار رقم {count} للعضو ID {target.id}.")


async def automatic_unmute_worker(app):
    """Unmute due members; permanent errors are closed and transient retries are throttled."""
    retry_after = {}
    while True:
        try:
            due = db.execute(
                "SELECT chat_id,user_id,muted_until FROM automatic_mutes "
                "WHERE status='active' AND muted_until<=? ORDER BY muted_until LIMIT 100",
                (datetime.now(timezone.utc).isoformat(timespec='seconds'),)
            ).fetchall()
            for chat_id, user_id, muted_until in due:
                retry_key = (int(chat_id), int(user_id))
                if retry_after.get(retry_key, 0) > time.time():
                    continue
                try:
                    await app.bot.restrict_chat_member(
                        chat_id=chat_id, user_id=user_id,
                        permissions=ChatPermissions(
                            can_send_messages=True, can_send_audios=True, can_send_documents=True,
                            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                            can_send_voice_notes=True, can_send_polls=True,
                            can_send_other_messages=True, can_add_web_page_previews=True
                        )
                    )
                    db.execute(
                        "UPDATE automatic_mutes SET status='completed',updated_at=? "
                        "WHERE chat_id=? AND user_id=? AND status='active'",
                        (now(), chat_id, user_id)
                    )
                    db.commit()
                    retry_after.pop(retry_key, None)
                    log_action(chat_id, user_id, 'automatic_unmute_3_days', f'until={muted_until}')
                    try:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(f"🔊 <b>إشعار إداري | فك الكتم تلقائيًا</b>\n\n"
                                  f"تم فك الكتم عن العضو <a href=\"tg://user?id={user_id}\">{user_id}</a> "
                                  "بعد انتهاء مدة الكتم المحددة بثلاثة أيام.\n\nنأمل الالتزام بتعليمات المجموعة."),
                            parse_mode='HTML'
                        )
                    except Exception as exc:
                        log.info("Unmute announcement failed chat=%s user=%s (%s): %s",
                                 chat_id, user_id, type(exc).__name__, exc)
                except BadRequest as exc:
                    # BadRequest is a permanent request/state error, not a transient network failure.
                    # If the user is already unrestricted or has left, close the queue item; otherwise
                    # mark it failed for admin review rather than generating the same warning every 30s.
                    detail = str(exc)
                    lowered = detail.casefold()
                    already_unrestricted = False
                    user_left = any(term in lowered for term in (
                        'user not found', 'participant_id_invalid', 'user_not_participant',
                        'member not found', 'chat not found', 'chat_id_invalid'
                    ))
                    if not user_left:
                        try:
                            member = await app.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                            permissions = getattr(member, 'permissions', None)
                            member_status = getattr(member, 'status', None)
                            if member_status in ('left', 'kicked'):
                                user_left = True
                            elif member_status in ('member', 'administrator', 'creator', 'owner'):
                                # Telegram reports ordinary/admin members when they are not muted.
                                already_unrestricted = True
                            elif member_status == 'restricted' and permissions is not None:
                                already_unrestricted = bool(getattr(permissions, 'can_send_messages', False))
                        except (BadRequest, Forbidden) as member_exc:
                            member_detail = str(member_exc).casefold()
                            user_left = any(term in member_detail for term in (
                                'user not found', 'participant_id_invalid', 'user_not_participant',
                                'member not found', 'chat not found', 'chat_id_invalid'
                            ))
                    retry_after.pop(retry_key, None)
                    retry_after.pop(retry_key + ("attempts",), None)
                    final_status = 'completed' if (already_unrestricted or user_left) else 'failed'
                    db.execute(
                        "UPDATE automatic_mutes SET status=?,updated_at=? "
                        "WHERE chat_id=? AND user_id=? AND status='active'",
                        (final_status, now(), chat_id, user_id)
                    )
                    db.commit()
                    log.warning(
                        "Automatic unmute stopped chat=%s user=%s status=%s BadRequest=%s",
                        chat_id, user_id, final_status, detail[:500]
                    )
                except Forbidden as exc:
                    # Missing bot rights cannot be fixed by retrying every 30 seconds.
                    db.execute(
                        "UPDATE automatic_mutes SET status='failed',updated_at=? "
                        "WHERE chat_id=? AND user_id=? AND status='active'",
                        (now(), chat_id, user_id)
                    )
                    db.commit()
                    retry_after.pop(retry_key, None)
                    retry_after.pop(retry_key + ("attempts",), None)
                    log.warning("Automatic unmute stopped chat=%s user=%s: bot permission error: %s",
                                chat_id, user_id, str(exc)[:500])
                except Exception as exc:
                    # Retry only unexpected/transient failures; keep the log informative.
                    # Exponential backoff (up to 30 minutes) avoids hammering Telegram and flooding logs.
                    previous = retry_after.get(retry_key + ("attempts",), 0)
                    attempts = int(previous) + 1
                    retry_after[retry_key + ("attempts",)] = attempts
                    delay = min(1800, 30 * (2 ** min(attempts - 1, 6)))
                    retry_after[retry_key] = time.time() + delay
                    log.warning("Automatic unmute transient failure chat=%s user=%s (%s): %s; retry in %ss",
                                chat_id, user_id, type(exc).__name__, str(exc)[:300], delay)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Automatic unmute worker failed")
            await asyncio.sleep(30)


def normalized_arabic_text(value):
    value = (value or "").casefold()
    value = re.sub(r"[\u064b-\u065f\u0670]", "", value)
    return value.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ؤ":"و","ئ":"ي","ة":"ه"}))


BAD_WORD_PATTERNS = (
    "كلب", "يا كلب", "حيوان", "يا حيوان", "انطم", "انقلع", "يا حمار", "حمار",
    "غبي", "يا غبي", "حقير", "يا حقير", "قذر", "تافه", "يا تافه"
)


def detect_abusive_language(text):
    normalized = normalized_arabic_text(text)
    if not normalized:
        return ""
    for phrase in BAD_WORD_PATTERNS:
        needle = normalized_arabic_text(phrase)
        if " " in needle:
            if needle in normalized:
                return phrase
        elif re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", normalized, flags=re.UNICODE):
            return phrase
    return ""


async def send_member_mass_mention(bot, chat_id):
    members = db.execute("SELECT user_id,display_name FROM member_activity WHERE chat_id=? ORDER BY display_name COLLATE NOCASE", (chat_id,)).fetchall()
    mentions, seen = [], set()
    for member_id, display_name in members:
        if not member_id or member_id in seen:
            continue
        seen.add(member_id)
        label = html.escape((display_name or "عضو").replace("\n", " ")[:80])
        mentions.append(f'<a href="tg://user?id={member_id}">{label}</a>')
    if not mentions:
        return 0, 0
    chunks = split_mentions_for_telegram(mentions)
    for chunk in chunks:
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
        await asyncio.sleep(0.35)
    return len(mentions), len(chunks)


def global_ban_applies(user_id, chat_id):
    row = db.execute("SELECT scope,active FROM global_bans WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row[1]:
        return False
    if row[0] == "all":
        return True
    return bool(db.execute("SELECT 1 FROM global_ban_groups WHERE user_id=? AND chat_id=?", (user_id, chat_id)).fetchone())

STAFF_CAPABILITY_LABELS = {
    "tag": "📣 التاق الجماعي",
    "publish": "📢 النشر والإعلانات",
    "protection": "🛡️ أوامر الحماية",
    "members": "👥 إدارة الأعضاء",
    "welcome": "👋 الترحيب والمنشن",
    "settings": "⚙️ إعدادات القروبات والردود",
    "ai": "🧠 الذكاء الاصطناعي",
    "executive": "🤖 المساعد التنفيذي",
    "inquiries": "📨 صندوق الاستفسارات والدعم",
    "calendar": "🗓️ الخدمات الطلابية والتقويم",
    "mass_ban": "🚫 الحظر الجماعي",
    "bulk_delete": "🗑️ مسح الرسائل حتى 500 رسالة لكل قروب",
    "security_review": "🛡️ تقارير المراجعة الأمنية",
    "archive": "📨 سجل التحويلات والملفات",
    "message_lock": "🔒 صلاحية قفل وفتح الرسائل",
    "reentry": "🚫 مراجعة المطرودين وطلبات العودة",
}


def staff_permissions_markup(target_uid):
    """Owner/delegated-owner UI for staff role and global capability grants."""
    rows = []
    manager = bool(db.execute("SELECT 1 FROM managers WHERE user_id=?", (target_uid,)).fetchone())
    protection_manager = bool(db.execute("SELECT 1 FROM protection_managers WHERE user_id=?", (target_uid,)).fetchone())
    rows.append([InlineKeyboardButton(("✅ " if manager else "⬜ ") + "مدير عام (إدارة تشغيلية واسعة)", callback_data=f"staffrole:{target_uid}:manager")])
    rows.append([InlineKeyboardButton(("✅ " if protection_manager else "⬜ ") + "مدير حماية", callback_data=f"staffrole:{target_uid}:protection")])
    for cap, label in STAFF_CAPABILITY_LABELS.items():
        granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone())
        rows.append([InlineKeyboardButton(("✅ " if granted else "⬜ ") + label + " — جميع القروبات", callback_data=f"staffcap:{target_uid}:{cap}")])
    rows.append([InlineKeyboardButton("📍 إدارة صلاحيات قروب محدد", callback_data=f"staffgroups:{target_uid}")])
    rows.append([InlineKeyboardButton("🔄 تحديث", callback_data=f"staffmanage:{target_uid}"), InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def unified_permissions_markup(target_uid):
    """Primary-owner-only combined role and group-scope management UI."""
    delegated = bool(db.execute("SELECT 1 FROM delegated_owners WHERE user_id=?", (target_uid,)).fetchone())
    manager = bool(db.execute("SELECT 1 FROM managers WHERE user_id=?", (target_uid,)).fetchone())
    protection = bool(db.execute("SELECT 1 FROM protection_managers WHERE user_id=?", (target_uid,)).fetchone())
    rows = [
        [InlineKeyboardButton(("✅ " if delegated else "⬜ ") + "مالك مفوّض (صلاحيات تشغيلية واسعة)", callback_data=f"unifiedrole:{target_uid}:delegated")],
        [InlineKeyboardButton(("✅ " if manager else "⬜ ") + "مدير عام", callback_data=f"unifiedrole:{target_uid}:manager")],
        [InlineKeyboardButton(("✅ " if protection else "⬜ ") + "مدير حماية", callback_data=f"unifiedrole:{target_uid}:protection")],
    ]
    for cap, label in STAFF_CAPABILITY_LABELS.items():
        if cap == "archive":
            continue
        granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone())
        rows.append([InlineKeyboardButton(("✅ " if granted else "⬜ ") + label + " — جميع القروبات", callback_data=f"unifiedcap:{target_uid}:{cap}")])
    archive_granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability='archive' AND chat_id=0", (target_uid,)).fetchone())
    rows.append([InlineKeyboardButton(("✅ " if archive_granted else "⬜ ") + "📨 سجل التحويلات والملفات — جميع القروبات", callback_data=f"unifiedcap:{target_uid}:archive")])
    rows.append([InlineKeyboardButton("📍 تحديد الصلاحيات حسب القروب", callback_data=f"unifiedgroups:{target_uid}")])
    rows.append([InlineKeyboardButton("🔄 تحديث", callback_data=f"unifiedmanage:{target_uid}"), InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def unified_group_list_markup(target_uid):
    groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title LIMIT 80").fetchall()
    rows = [[InlineKeyboardButton("🌐 جميع القروبات (الصلاحيات العامة بالأعلى)", callback_data=f"unifiedmanage:{target_uid}")]]
    rows.extend([[InlineKeyboardButton(f"📍 {title or chat_id}", callback_data=f"unifiedgroup:{target_uid}:{chat_id}")] for chat_id, title in groups])
    rows.append([InlineKeyboardButton("⬅️ إدارة المستخدم", callback_data=f"unifiedmanage:{target_uid}")])
    return InlineKeyboardMarkup(rows)


def unified_group_markup(target_uid, chat_id):
    title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    rows = []
    for cap, label in STAFF_CAPABILITY_LABELS.items():
        if cap == "archive":
            continue
        granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id IN (0,?)", (target_uid, cap, chat_id)).fetchone())
        denied = bool(db.execute("SELECT 1 FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id)).fetchone())
        if denied: granted = False
        rows.append([InlineKeyboardButton(("✅ " if granted else "⬜ ") + label, callback_data=f"unifiedgcap:{target_uid}:{chat_id}:{cap}")])
    archive_granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability='archive' AND chat_id IN (0,?)", (target_uid, chat_id)).fetchone())
    archive_denied = bool(db.execute("SELECT 1 FROM staff_capability_denials WHERE user_id=? AND capability='archive' AND chat_id=?", (target_uid, chat_id)).fetchone())
    if archive_denied:
        archive_granted = False
    rows.append([InlineKeyboardButton(("✅ " if archive_granted else "⬜ ") + "📨 سجل التحويلات والملفات لهذا القروب", callback_data=f"unifiedgcap:{target_uid}:{chat_id}:archive")])
    current_role = group_role(chat_id, target_uid)
    for role in ("مشرف", "مشرف عام", "مدير", "مدير عام"):
        rows.append([InlineKeyboardButton(("✅ " if current_role == role else "⬜ ") + "رتبة القروب: " + role, callback_data=f"unifiedgrole:{target_uid}:{chat_id}:{role}")])
    if current_role:
        rows.append([InlineKeyboardButton("➖ إزالة رتبة القروب", callback_data=f"unifiedgrole:{target_uid}:{chat_id}:remove")])
    rows.append([InlineKeyboardButton("⬅️ اختيار القروب", callback_data=f"unifiedgroups:{target_uid}")])
    rows.append([InlineKeyboardButton("⬅️ إدارة المستخدم", callback_data=f"unifiedmanage:{target_uid}")])
    return InlineKeyboardMarkup(rows)


def staff_group_capabilities_markup(target_uid, chat_id):
    rows = []
    title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    for cap, label in STAFF_CAPABILITY_LABELS.items():
        granted = can_use_capability(target_uid, cap, chat_id)
        # A global grant may have a local denial; show effective permission, then toggle it.
        rows.append([InlineKeyboardButton(("✅ " if granted else "⬜ ") + label, callback_data=f"staffgcap:{target_uid}:{chat_id}:{cap}")])
    current_role = group_role(chat_id, target_uid)
    rows.append([InlineKeyboardButton("🪪 الرتبة داخل هذا القروب: " + (current_role or "عضو"), callback_data="noop")])
    for role_label in ("مشرف", "مشرف عام", "مدير", "مدير عام"):
        rows.append([InlineKeyboardButton(("✅ " if current_role == role_label else "⬜ ") + "رفع رتبة: " + role_label, callback_data=f"staffgrole:{target_uid}:{chat_id}:{role_label}")])
    if current_role:
        rows.append([InlineKeyboardButton("➖ إزالة الرتبة من هذا القروب", callback_data=f"staffgrole:{target_uid}:{chat_id}:remove")])
    rows.append([InlineKeyboardButton("⬅️ اختيار القروب", callback_data=f"staffgroups:{target_uid}")])
    rows.append([InlineKeyboardButton("⬅️ إدارة الموظف", callback_data=f"staffmanage:{target_uid}")])
    return InlineKeyboardMarkup(rows)





EXTERNAL_CAPABILITY_LABELS = {
    "tag": "📣 التاق الجماعي",
    "protection": "🛡️ الحماية العادية",
    "members": "👥 إدارة الأعضاء والرتب المحلية",
    "settings": "⚙️ إعدادات القروب",
    "welcome": "👋 الترحيب",
    "ai": "🧠 الذكاء الاصطناعي",
}


def external_groups_markup():
    rows = []
    groups = db.execute("""SELECT e.chat_id,COALESCE(NULLIF(e.title,''),w.title,e.chat_id),e.enabled,e.added_by_name,e.added_by
        FROM external_group_control e LEFT JOIN watched_groups w ON w.chat_id=e.chat_id
        WHERE e.is_external=1 ORDER BY e.created_at DESC LIMIT 60""").fetchall()
    for cid, title, enabled, added_name, added_by in groups:
        state = "🟢" if enabled else "🔴"
        rows.append([InlineKeyboardButton(f"{state} {str(title)[:45]} · {cid}", callback_data=f"external_group:{cid}")])
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد قروبات خارجية مسجلة حتى الآن", callback_data="noop")])
    rows.append([InlineKeyboardButton("🆘 صندوق الدعم الفني", callback_data="external_support_inbox")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def external_group_manage_markup(chat_id):
    row = db.execute("""SELECT e.title,e.enabled,e.ai_enabled,e.added_by,e.added_by_name,e.added_by_username
        FROM external_group_control e WHERE e.chat_id=? AND e.is_external=1""", (chat_id,)).fetchone()
    if not row:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القروبات الخارجية", callback_data="external_groups")]])
    title, enabled, ai_enabled, added_by, added_name, added_username = row
    rows = [[InlineKeyboardButton(("⏸️ إيقاف البوت عن القروب" if enabled else "▶️ تفعيل البوت للقروب"),
                                  callback_data=f"external_toggle:{chat_id}")]]
    rows.append([InlineKeyboardButton(("🧠 تعطيل الذكاء الاصطناعي" if ai_enabled else "🧠 منح الذكاء الاصطناعي"),
                                      callback_data=f"external_ai:{chat_id}")])
    for cap, label in EXTERNAL_CAPABILITY_LABELS.items():
        if cap == "ai":
            continue
        enabled_row = db.execute("SELECT enabled FROM external_group_capabilities WHERE chat_id=? AND capability=?", (chat_id, cap)).fetchone()
        active = bool(enabled_row and enabled_row[0])
        rows.append([InlineKeyboardButton(("✅ " if active else "⬜ ") + label, callback_data=f"external_cap:{chat_id}:{cap}")])
    rows.append([InlineKeyboardButton("⚠️ قائمة مخالفين القروب", callback_data=f"external_violators:{chat_id}")])
    rows.append([InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="external_groups")])
    return InlineKeyboardMarkup(rows)


def support_tickets_markup():
    rows = []
    tickets = db.execute("""SELECT id,group_title,requester_name,status FROM external_support_tickets
        ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, id DESC LIMIT 40""").fetchall()
    for ticket_id, title, requester, status in tickets:
        status_label = {"new":"🆕","answered":"✅","closed":"🔒"}.get(status,"📨")
        rows.append([InlineKeyboardButton(f"{status_label} #{ticket_id} · {str(title)[:28]} · {str(requester)[:18]}",
                                          callback_data=f"support_ticket:{ticket_id}")])
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد طلبات دعم حتى الآن", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def massban_groups_markup(groups, selected):
    rows = [[InlineKeyboardButton(("✅ " if cid in selected else "⬜ ") + str(title or cid)[:48],
                                  callback_data=f"massban_group_toggle:{cid}")] for cid,title in groups[:45]]
    rows.append([InlineKeyboardButton("✅ تحديد الكل", callback_data="massban_groups_all"),
                 InlineKeyboardButton("🧹 إلغاء التحديد", callback_data="massban_groups_none")])
    rows.append([InlineKeyboardButton(f"➡️ متابعة ({len(selected)})", callback_data="massban_groups_done")])
    rows.append([InlineKeyboardButton("⬅️ إلغاء", callback_data="massban_menu")])
    return InlineKeyboardMarkup(rows)


def can_bulk_delete(uid, chat_id=0):
    """المالك/المفوّض/المدير/مشرف الحماية أو موظف مُنح صلاحية المسح ضمن نطاقه."""
    if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
        return True
    return can_use_capability(uid, "bulk_delete", chat_id)


def bulk_delete_groups_for_user(uid):
    """قائمة القروبات المسموح للمستخدم بمسح الرسائل منها بحسب صلاحياته."""
    rows = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
    if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
        return rows
    allowed = capability_group_ids(uid, "bulk_delete")
    return [row for row in rows if int(row[0]) in allowed]


def bulk_delete_groups_markup(groups, selected):
    rows = [[InlineKeyboardButton(("✅ " if int(cid) in selected else "⬜ ") + str(title or cid)[:42],
                                  callback_data=f"bulkdel_toggle:{cid}")] for cid, title in groups[:45]]
    if not rows:
        rows = [[InlineKeyboardButton("لا توجد قروبات ضمن صلاحياتك", callback_data="noop")]]
    rows.append([InlineKeyboardButton("✅ تحديد الكل", callback_data="bulkdel_all"),
                 InlineKeyboardButton("🧹 إلغاء التحديد", callback_data="bulkdel_none")])
    rows.append([InlineKeyboardButton(f"➡️ متابعة ({len(selected)})", callback_data="bulkdel_groups_done")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="bulkdel_cancel")])
    return InlineKeyboardMarkup(rows)


def bulk_delete_confirm_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ تأكيد المسح", callback_data="bulkdel_confirm")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="bulkdel_cancel")],
    ])


def panel_markup(uid):
    """واجهة مرتبة حسب الدور، مع الحفاظ على جميع أقسام الإدارة السابقة."""
    # يجمع نص الزر الأيقونة الوظيفية الجديدة مع مسمى القسم السابق حتى لا تضيع أي وظيفة.
    if is_delegated_owner(uid) and not is_owner(uid):
        delegated_rows = [
            [InlineKeyboardButton("🛡️📡 الحماية والرادار الأمني", callback_data="security_groups"), InlineKeyboardButton("👥🧰 إدارة الأعضاء والإشراف", callback_data="administration_groups")],
            [InlineKeyboardButton("📣📢 مركز الإعلانات والنشر", callback_data="broadcast_start"), InlineKeyboardButton("👋🎉 الترحيب الذكي", callback_data="welcome")],
            [InlineKeyboardButton("📋🧾 سجل المخالفات والعمليات", callback_data="logs")],
            [InlineKeyboardButton("🎬 مركز الوسائط", callback_data="media_admin")],
            [InlineKeyboardButton("⚙️ إعدادات القروبات", callback_data="settings"), InlineKeyboardButton("🔔 التنبيهات", callback_data="alerts")],
            [InlineKeyboardButton("🏅🎖️ الرتب الإدارية", callback_data="roles_groups"), InlineKeyboardButton("🩺 حالة البوت", callback_data="health")],
            [InlineKeyboardButton("📣 استدعاء التاق للقروبات", callback_data="owner_tag_groups")],
            [InlineKeyboardButton("🧭 مركز الطاقم", callback_data="staff_center"), InlineKeyboardButton("📋 المهام والمناوبات", callback_data="staff_tasks")],
            [InlineKeyboardButton("👥 إدارة المشرفين والمدراء والصلاحيات", callback_data="staff_permissions")],
            [InlineKeyboardButton("💬 إدارة الردود الآلية لكل القروبات", callback_data="owner_autoreplies")],
        ]
        if can_use_capability(uid, "mass_ban"):
            delegated_rows.append([InlineKeyboardButton("🚫 الحظر الجماعي", callback_data="massban_menu")])
        delegated_rows.append([InlineKeyboardButton("🗑️ مسح الرسائل (حتى 500 لكل قروب)", callback_data="bulkdel_menu")])
        if can_use_capability(uid, "security_review"):
            delegated_rows.append([InlineKeyboardButton("🛡️ المراجعة الأمنية", callback_data="security_review_latest")])
        delegated_rows.append([InlineKeyboardButton("⚠️ الإنذارات التفاعلية", callback_data="warnings_center")])
        if can_use_capability(uid, "message_lock") or capability_group_ids(uid, "message_lock"):
            delegated_rows.append([InlineKeyboardButton("🔒 قفل/فتح الرسائل في القروبات", callback_data="allmsg_open:lock"), InlineKeyboardButton("🔓 فتح الرسائل في القروبات", callback_data="allmsg_open:unlock")])
        if (can_use_capability(uid, "reentry") or capability_group_ids(uid, "reentry")
                or can_use_capability(uid, "join_approval") or capability_group_ids(uid, "join_approval")):
            delegated_rows.append([InlineKeyboardButton("🚫 المحظورون والمرفوضون وطلبات الانضمام", callback_data="restricted_members")])
        return InlineKeyboardMarkup(delegated_rows)
    if not is_owner(uid) and not is_delegated_owner(uid) and (is_general_manager(uid) or is_protection_manager(uid)):
        rows = [
            [InlineKeyboardButton("🛡️ الحماية", callback_data="security_groups"),
             InlineKeyboardButton("👥 إدارة الأعضاء", callback_data="administration_groups")],
            [InlineKeyboardButton("📋 السجلات", callback_data="logs")],
            [InlineKeyboardButton("⚙️ إعدادات القروبات", callback_data="settings"),
             InlineKeyboardButton("👋 الترحيب", callback_data="welcome")],
            [InlineKeyboardButton("🔔 التنبيهات", callback_data="alerts"),
             InlineKeyboardButton("🩺 حالة البوت", callback_data="health")],
        ]
        if can_use_capability(uid, "publish"):
            rows.append([InlineKeyboardButton("📣 النشر والإعلانات", callback_data="broadcast_start")])
        if can_use_capability(uid, "tag"):
            rows.append([InlineKeyboardButton("📣 استدعاء التاق", callback_data="owner_tag_groups")])
        if can_use_capability(uid, "mass_ban"):
            rows.append([InlineKeyboardButton("🚫 الحظر الجماعي", callback_data="massban_menu")])
        if can_bulk_delete(uid):
            rows.append([InlineKeyboardButton("🗑️ مسح الرسائل (حتى 500 لكل قروب)", callback_data="bulkdel_menu")])
        if can_use_capability(uid, "message_lock") or capability_group_ids(uid, "message_lock"):
            rows.append([InlineKeyboardButton("🔒 قفل/فتح الرسائل في القروبات", callback_data="allmsg_open:lock"), InlineKeyboardButton("🔓 فتح الرسائل في القروبات", callback_data="allmsg_open:unlock")])
        if can_use_capability(uid, "reentry") or capability_group_ids(uid, "reentry") or can_use_capability(uid, "join_approval") or capability_group_ids(uid, "join_approval"):
            rows.append([InlineKeyboardButton("🚫 المحظورون والمرفوضون وطلبات الانضمام", callback_data="restricted_members")])
        if is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid, "protection"):
            rows.append([InlineKeyboardButton("⚠️ الإنذارات التفاعلية", callback_data="warnings_center")])
        rows.append([InlineKeyboardButton("❌ إغلاق اللوحة", callback_data="home")])
        return InlineKeyboardMarkup(rows)
    if not is_owner(uid) and not is_delegated_owner(uid) and not is_general_manager(uid) and not is_protection_manager(uid):
        rows = []
        if can_use_capability(uid, "protection"):
            rows.append([InlineKeyboardButton("⚡ أوامر الحماية للقروبات المصرح بها", callback_data="bulk_security")])
        if can_use_capability(uid, "publish"):
            rows.append([InlineKeyboardButton("📣 النشر والإعلانات", callback_data="broadcast_start")])
        if can_use_capability(uid, "tag"):
            rows.append([InlineKeyboardButton("📣 استدعاء التاق", callback_data="owner_tag_groups")])
        if can_use_capability(uid, "message_lock") or capability_group_ids(uid, "message_lock"):
            rows.append([InlineKeyboardButton("🔒 قفل/فتح الرسائل في القروبات", callback_data="allmsg_open:lock"), InlineKeyboardButton("🔓 فتح الرسائل في القروبات", callback_data="allmsg_open:unlock")])
        if (can_use_capability(uid, "reentry") or capability_group_ids(uid, "reentry")
                or can_use_capability(uid, "join_approval") or capability_group_ids(uid, "join_approval")):
            rows.append([InlineKeyboardButton("🚫 المحظورون والمرفوضون وطلبات الانضمام", callback_data="restricted_members")])
        if can_use_capability(uid, "protection"):
            rows.append([InlineKeyboardButton("⚠️ الإنذارات التفاعلية", callback_data="warnings_center")])
        if can_bulk_delete(uid) or capability_group_ids(uid, "bulk_delete"):
            rows.append([InlineKeyboardButton("🗑️ مسح الرسائل (حتى 500 لكل قروب)", callback_data="bulkdel_menu")])
        rows.append([InlineKeyboardButton("❌ إغلاق اللوحة", callback_data="home")])
        return InlineKeyboardMarkup(rows)
    owner_rows = [
        [InlineKeyboardButton("🛡️📡 الحماية والرادار الأمني", callback_data="security"), InlineKeyboardButton("👥🧰 إدارة الأعضاء والإشراف", callback_data="administration")],
        [InlineKeyboardButton("⚡ أوامر الحماية الجماعية", callback_data="bulk_security")],
        [InlineKeyboardButton("🗑️ مسح الرسائل في قروبات محددة (حتى 500)", callback_data="bulkdel_menu")],
        [InlineKeyboardButton("👁️ مراقبة القروبات المسجلة", callback_data="monitor_groups")],
        [InlineKeyboardButton("🚫 المحظورون والمرفوضون وطلبات العودة", callback_data="restricted_members")],
        [InlineKeyboardButton("🌟 المحتوى التحفيزي اليومي", callback_data="motivation_home")],
        [InlineKeyboardButton("🌙 أذكار المساء المجدولة للقروبات", callback_data="adhkar_auto_settings")],
        [InlineKeyboardButton("🔒 قفل الرسائل في قروبات محددة", callback_data="allmsg_open:lock"), InlineKeyboardButton("🔓 فتح الرسائل في قروبات محددة", callback_data="allmsg_open:unlock")],
        [InlineKeyboardButton("📣📢 مركز الإعلانات والنشر", callback_data="broadcast_start"), InlineKeyboardButton("👋🎉 الترحيب الذكي", callback_data="welcome")],
        [InlineKeyboardButton("📋🧾 سجل المخالفات والعمليات", callback_data="logs")],
        [InlineKeyboardButton("🎬 مركز الوسائط", callback_data="media_admin")],
        [InlineKeyboardButton("⚙️ إعدادات القروبات", callback_data="settings"), InlineKeyboardButton("🏅🎖️ الرتب الإدارية", callback_data="roles_groups")],
        [InlineKeyboardButton("🔔 التنبيهات", callback_data="alerts"), InlineKeyboardButton("🩺 صحة البوت", callback_data="health")],
        [InlineKeyboardButton("📣 استدعاء التاق للقروبات", callback_data="owner_tag_groups")],
        [InlineKeyboardButton("💬 إدارة الردود الآلية لكل القروبات", callback_data="owner_autoreplies")],
        [InlineKeyboardButton("📨 صندوق استفسارات الأعضاء", callback_data="inquiries_inbox"), InlineKeyboardButton("🧭 مركز الطاقم", callback_data="staff_center")],
        [InlineKeyboardButton("🛡️ توزيع الصلاحيات والقروبات", callback_data="unified_permissions")],
        [InlineKeyboardButton("👥 إدارة المشرفين والمدراء والصلاحيات", callback_data="staff_permissions")],
        [InlineKeyboardButton("🗂️ استفسارات الطاقم", callback_data="staff_inquiries_inbox"), InlineKeyboardButton("📋 المهام والمناوبات", callback_data="staff_tasks")],
        [InlineKeyboardButton("📡 إدارة القنوات والنشر", callback_data="channels_manage")],
        [InlineKeyboardButton("👑 إدارة المفوّضين", callback_data="delegated_owner"), InlineKeyboardButton("💾 النسخ الاحتياطي", callback_data="owner_backup")],
        [InlineKeyboardButton("⚠️ الإنذارات التفاعلية", callback_data="warnings_center"), InlineKeyboardButton("📨 سجل التحويلات والملفات", callback_data="archive")],
        [InlineKeyboardButton("🧩 إعدادات النظام العليا", callback_data="owner_system")],
    ]
    if is_owner(uid):
        owner_rows.append([InlineKeyboardButton("🌐 إدارة القروبات الخارجية", callback_data="external_groups"),
                           InlineKeyboardButton("🆘 صندوق الدعم الفني", callback_data="external_support_inbox")])
        owner_rows.append([InlineKeyboardButton("🚫 الحظر الجماعي", callback_data="massban_menu"),
                           InlineKeyboardButton("🛡️ المراجعة الأمنية الأسبوعية", callback_data="security_review_latest")])
        owner_rows.append([InlineKeyboardButton("🔄 إدارة تحديثات البوت", callback_data="update_manager")])
    return InlineKeyboardMarkup(owner_rows)

def settings_page_markup(chat_id, page=0, back_callback="home"):
    items = [
        ("منع الروابط", "links"), ("الكيبورد", "keyboard"),
        ("الصوت", "audio"), ("صور GIF المتحركة", "gif"), ("الملفات", "files"),
        ("منع تكرار الرسائل", "repeat"), ("الفيديو", "videos"), ("الصور", "photos"),
        ("أسماء المستخدمين", "username"), ("الإشارات للأعضاء", "tag"), ("البوتات", "bots"),
        ("الملصقات", "stickers"), ("حذف الرسائل المعدلة", "edit"),
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
    rows.append([InlineKeyboardButton("⬅️ لوحة القروب" if back_callback.startswith("group_panel_home:") else "⬅️ القائمة الرئيسية", callback_data=back_callback)])
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


def _assistant_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def remember_assistant_user(user):
    if not user:
        return
    now = _assistant_now()
    db.execute("""INSERT INTO assistant_users(user_id,username,display_name,first_seen,last_seen)
        VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
        display_name=excluded.display_name,last_seen=excluded.last_seen""",
        (user.id, user.username or "", user.full_name or "", now, now))
    db.execute("INSERT OR IGNORE INTO assistant_preferences(user_id) VALUES(?)", (user.id,))
    db.commit()


async def ask_cmd(update, context):
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("لخصوصيتك، أرسل الاستفسار في الخاص مع البوت.")
        return
    context.user_data["awaiting_general_inquiry"] = True
    await update.effective_message.reply_text("💬 أرسل استفسارك الآن في رسالة واحدة. سيصل إلى المالك الأساسي فقط، ولن يُنشر في أي قروب. أرسل /cancel لإلغاء الطلب.")


async def addchannel_cmd(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ ربط القنوات للمالك الأساسي فقط.")
        return
    if not context.args:
        await update.effective_message.reply_text("الاستخدام: /addchannel @channelusername\nأضف البوت مشرفًا في القناة مع صلاحية نشر الرسائل أولًا.")
        return
    target = context.args[0].strip()
    try:
        channel = await context.bot.get_chat(target)
        if channel.type != ChatType.CHANNEL:
            await update.effective_message.reply_text("المعرّف ليس قناة تيليجرام.")
            return
        bot_member = await context.bot.get_chat_member(channel.id, context.bot.id)
        if getattr(bot_member, "status", "") != "administrator" or not getattr(bot_member, "can_post_messages", False):
            await update.effective_message.reply_text("⛔ لم يتم الربط: أضف البوت مشرفًا في القناة وفعّل صلاحية نشر الرسائل، ثم أعد المحاولة.")
            return
        db.execute("INSERT INTO linked_channels(chat_id,title,username,linked_by,linked_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,username=excluded.username,linked_by=excluded.linked_by,linked_at=excluded.linked_at",
                   (channel.id, channel.title or str(channel.id), channel.username or "", update.effective_user.id, now()))
        db.commit()
        await update.effective_message.reply_text(f"✅ تم ربط القناة: {channel.title}\nالمعرّف: {channel.id}\nيمكنك الآن اختيارها من لوحة القنوات للنشر.")
    except Exception as exc:
        log.warning("channel registration failed: %s", exc)
        await update.effective_message.reply_text("تعذر ربط القناة. تأكد من صحة @username وأن البوت مشرف مع صلاحية النشر.")


async def delchannel_cmd(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ حذف القنوات المرتبطة للمالك الأساسي فقط.")
        return
    if not context.args:
        await update.effective_message.reply_text("الاستخدام: /delchannel @channelusername أو /delchannel CHANNEL_ID")
        return
    raw = context.args[0].strip()
    row = None
    if raw.lstrip('-').isdigit():
        row = db.execute("SELECT chat_id,title FROM linked_channels WHERE chat_id=?", (int(raw),)).fetchone()
    else:
        row = db.execute("SELECT chat_id,title FROM linked_channels WHERE username=?", (raw.lstrip('@'),)).fetchone()
    if not row:
        await update.effective_message.reply_text("لم أجد هذه القناة ضمن القنوات المرتبطة.")
        return
    db.execute("DELETE FROM linked_channels WHERE chat_id=?", (row[0],)); db.commit()
    await update.effective_message.reply_text(f"✅ تم فك ربط القناة: {row[1]}")


async def get_live_linked_channels(bot, purge_confirmed=True):
    """Compatibility wrapper for live linked-channel verification."""
    return await _service_get_live_linked_channels(
        bot=bot,
        db=db,
        channel_type=ChatType.CHANNEL,
        bad_request_type=BadRequest,
        forbidden_type=Forbidden,
        logger=log,
        purge_confirmed=purge_confirmed,
    )


async def channels_cmd(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ قائمة القنوات للمالك الأساسي فقط.")
        return
    rows = await get_live_linked_channels(context.bot)
    text = "📡 القنوات المتصلة والجاهزة للنشر:\n" + ("\n".join(f"• {title} — @{username or 'بدون معرف'} — {cid}" for cid,title,username in rows) if rows else "لا توجد قنوات متصلة وجاهزة للنشر. تحقق من إضافة البوت مشرفًا مع صلاحية نشر الرسائل.")
    await update.effective_message.reply_text(text[:4000])


async def publish_channels_cmd(update, context):
    if not update.effective_user or not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ النشر للقنوات من المالك الأساسي فقط.")
        return
    live_rows = await get_live_linked_channels(context.bot)
    if not live_rows:
        await update.effective_message.reply_text("لا توجد قنوات متصلة وجاهزة للنشر. تحقق من صلاحية البوت في القناة.")
        return
    context.user_data["channel_broadcast_selected"] = []
    context.user_data["channel_broadcast_stage"] = "select"
    kb = [[InlineKeyboardButton(f"⬜ {title}", callback_data=f"channel_pub_toggle:{cid}")] for cid,title,username in live_rows[:50]]
    kb += [[InlineKeyboardButton("🌐 تحديد جميع القنوات", callback_data="channel_pub_all")], [InlineKeyboardButton("➡️ متابعة", callback_data="channel_pub_prepare")], [InlineKeyboardButton("❌ إلغاء", callback_data="channel_pub_cancel")]]
    await update.effective_message.reply_text("📡 اختر القنوات المستهدفة للنشر:", reply_markup=InlineKeyboardMarkup(kb))


def _is_aistatus_alias(text):
    """Accept status command with or without slash, and tolerate a trailing slash."""
    value = (text or "").strip().casefold()
    value = value.lstrip("/").rstrip("/").strip()
    return value == "aistatus"


async def process_private_workflows(update, context):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        return
    raw = (msg.text or "").strip()
    # Input step for the interactive, role-scoped bulk-delete panel.
    if context.user_data.get("bulk_delete_stage") in ("count", "confirm") and raw.lower() == "/cancel":
        for key in ("bulk_delete_stage", "bulk_delete_count", "bulk_delete_selected_groups", "bulk_delete_groups_snapshot"):
            context.user_data.pop(key, None)
        await msg.reply_text("تم إلغاء عملية مسح الرسائل.")
        return
    if context.user_data.get("bulk_delete_stage") == "count":
        if raw.startswith("/"):
            await msg.reply_text("أرسل عددًا من 1 إلى 500، أو /cancel لإلغاء العملية.")
            return
        try:
            requested_count = int(raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")))
        except ValueError:
            await msg.reply_text("❌ أرسل عددًا صحيحًا من 1 إلى 500.")
            return
        if not 1 <= requested_count <= 500:
            await msg.reply_text("⚠️ الحد المسموح من 1 إلى 500 رسالة لكل قروب.")
            return
        selected = [int(x) for x in context.user_data.get("bulk_delete_selected_groups", [])]
        allowed = {int(cid) for cid, _ in bulk_delete_groups_for_user(user.id)}
        selected = [cid for cid in selected if cid in allowed]
        if not selected:
            for key in ("bulk_delete_stage", "bulk_delete_count", "bulk_delete_selected_groups", "bulk_delete_groups_snapshot"):
                context.user_data.pop(key, None)
            await msg.reply_text("⛔ لا توجد قروبات محددة ضمن صلاحياتك. افتح لوحة التحكم من جديد.")
            return
        context.user_data["bulk_delete_selected_groups"] = selected
        context.user_data["bulk_delete_count"] = requested_count
        context.user_data["bulk_delete_stage"] = "confirm"
        names = context.user_data.get("bulk_delete_groups_snapshot", {})
        group_text = "\n".join(f"• {names.get(cid, cid)}" for cid in selected[:30])
        await msg.reply_text(f"⚠️ تأكيد مسح الرسائل\n\nعدد الرسائل المطلوب: {requested_count} لكل قروب\nعدد القروبات: {len(selected)}\n\nالقروبات المحددة:\n{group_text}\n\nملاحظة: يمكن مسح الرسائل التي رصدها البوت منذ تشغيله فقط، ويقيّد تيليجرام حذف بعض الرسائل الأقدم من 48 ساعة. تأكد من الاختيار قبل المتابعة.", reply_markup=bulk_delete_confirm_markup())
        return
    # Status commands must remain commands even while another private workflow is waiting.
    if _is_aistatus_alias(raw):
        return
    # New owner UI workflows: unified permissions, warning operations and archive search.
    pending = next((key for key in ("awaiting_motivation_edit", "awaiting_unified_target_id", "awaiting_warning_grant", "awaiting_warning_log_search", "awaiting_archive_search") if context.user_data.get(key)), None)
    if pending:
        if raw.startswith("/") and raw.lower() != "/cancel":
            context.user_data.pop(pending, None)
            await msg.reply_text("ℹ️ ألغيت انتظار الإدخال حتى لا يتعارض مع الأمر الجديد. أعد إرسال الأمر الآن.")
            return
        if raw.lower() == "/cancel":
            context.user_data.pop(pending, None); await msg.reply_text("تم إلغاء العملية."); return
        if pending == "awaiting_motivation_edit":
            if not is_owner(user.id):
                context.user_data.pop(pending, None); await msg.reply_text("⛔ للمالك الأساسي فقط."); return
            try: draft_id = int(context.user_data.pop(pending))
            except (TypeError, ValueError):
                await msg.reply_text("تعذر تحديد المسودة. افتح /motivation وحاول مجددًا."); return
            if not raw or len(raw) > 3500 or raw.startswith("/"):
                await msg.reply_text("أرسل نصًا صالحًا لا يتجاوز 3500 حرف، أو /cancel للإلغاء."); return
            cur = db.execute("UPDATE motivation_drafts SET content=?,content_type='محتوى معدّل',created_at=? WHERE id=? AND status='pending'", (raw,now(),draft_id)); db.commit()
            if not cur.rowcount:
                await msg.reply_text("لم تعد المسودة بانتظار التعديل. افتح /motivation لمراجعة الحالة."); return
            await show_motivation_draft(msg, draft_id)
            return
        if pending == "awaiting_unified_target_id":
            if not is_owner(user.id): context.user_data.pop(pending,None); await msg.reply_text("⛔ للمالك الأساسي فقط."); return
            if not raw.isdigit() or int(raw) <= 0:
                context.user_data.pop(pending,None); await msg.reply_text("❌ أُلغيت العملية لأن المعرّف غير صحيح. افتح الأيقونة مجددًا وأرسل ID رقميًا."); return
            target_uid=int(raw); context.user_data.pop(pending,None)
            if target_uid == OWNER_ID: await msg.reply_text("هذا هو حساب المالك الأساسي ولا يمكن تعديل صلاحياته."); return
            await msg.reply_text(f"🛡️ توزيع الصلاحيات والقروبات\nالمستخدم: {user_display_name(target_uid)}\nID: {target_uid}\nاختر الرتبة أو الصلاحية:",reply_markup=unified_permissions_markup(target_uid)); return
        if pending == "awaiting_warning_grant":
            selected_chat = context.user_data.get(pending)
            context.user_data.pop(pending,None)
            parts=raw.split()
            try:
                if isinstance(selected_chat, int) and not isinstance(selected_chat, bool):
                    chat_id=selected_chat
                    if not parts: raise ValueError
                    target_uid=int(parts[0]); reason_parts=parts[1:]
                else:
                    if len(parts)<2: raise ValueError
                    chat_id=int(parts[0]); target_uid=int(parts[1]); reason_parts=parts[2:]
            except ValueError:
                await msg.reply_text("❌ أرسل ID العضو فقط أو ID العضو ثم سبب الإنذار."); return
            if chat_id >= 0 or target_uid <= 0: await msg.reply_text("❌ تأكد من صحة معرّف القروب وID العضو."); return
            if not (is_owner(user.id) or is_delegated_owner(user.id) or can_use_capability(user.id,"protection",chat_id) or is_general_manager(user.id) or is_protection_manager(user.id)):
                await msg.reply_text("⛔ لا تملك صلاحية منح إنذار في هذا القروب."); return
            if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone(): await msg.reply_text("القروب غير مسجل لدى البوت."); return
            try:
                member=await context.bot.get_chat_member(chat_id,target_uid); target=member.user
            except Exception:
                from telegram import User
                target=User(id=target_uid,first_name=f"ID {target_uid}",is_bot=False)
            if target.is_bot or is_owner(target.id) or is_delegated_owner(target.id) or is_general_manager(target.id) or is_protection_manager(target.id):
                await msg.reply_text("⛔ لا يمكن منح إنذار لحساب بوت أو للمالك/الإدارة العليا."); return
            reason=" ".join(reason_parts)[:200] if reason_parts else "مخالفة تعليمات القروب"
            try:
                target_chat=await context.bot.get_chat(chat_id)
                count,_,_=await register_warning_and_enforce(context,target_chat,target,reason,abusive=False)
                log_action(chat_id,user.id,"manual_warning_owner_ui",f"target={target_uid}; count={count}; reason={reason}")
                await msg.reply_text(f"✅ تم تسجيل الإنذار رقم {count} للعضو {target_uid} في القروب {chat_id}.")
            except Exception as exc:
                log.warning("Owner warning UI failed chat=%s target=%s: %s",chat_id,target_uid,exc)
                await msg.reply_text("تعذر تنفيذ الإنذار. تحقق من صلاحية البوت في القروب ومن وجوده فيه.")
            return
        if pending == "awaiting_warning_log_search":
            context.user_data.pop(pending,None)
            parts=raw.split()
            if len(parts)!=2:
                await msg.reply_text("❌ الصيغة: CHAT_ID USER_ID"); return
            try: chat_id=int(parts[0]); target_uid=int(parts[1])
            except ValueError: await msg.reply_text("❌ أدخل معرّفين رقميين."); return
            if not (is_owner(user.id) or is_delegated_owner(user.id) or is_general_manager(user.id) or is_protection_manager(user.id) or can_use_capability(user.id,"protection",chat_id)):
                await msg.reply_text("⛔ لا تملك صلاحية هذا السجل."); return
            row=db.execute("SELECT display_name,username,warning_count,abuse_count,last_reason,reasons_json,last_at FROM member_violations WHERE chat_id=? AND user_id=?",(chat_id,target_uid)).fetchone()
            if not row: await msg.reply_text("لا يوجد سجل إنذارات لهذا العضو في هذا القروب."); return
            name,username,count,abuse,reason,reasons_json,last_at=row
            try: reasons=json.loads(reasons_json or "[]")
            except Exception: reasons=[]
            lines=[f"📋 سجل الإنذارات: {name or target_uid} (ID: {target_uid})",f"القروب: {chat_id}",f"إجمالي الإنذارات: {count} | الألفاظ المسيئة: {abuse}",f"آخر سبب: {reason or 'غير محدد'}",f"آخر تحديث: {last_at or 'غير محدد'}","", "المخالفات الأخيرة:"]
            lines.extend([f"• {x.get('at','')}: {x.get('reason','')}" for x in reasons[-10:]])
            await msg.reply_text("\n".join(lines)[:3900],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧹 تصفير الإنذارات",callback_data=f"warnclear:{chat_id}:{target_uid}")]])); return
        if pending == "awaiting_archive_search":
            context.user_data.pop(pending,None)
            allowed_archive_groups = capability_group_ids(user.id, "archive")
            if not (is_owner(user.id) or allowed_archive_groups):
                await msg.reply_text("⛔ لا تملك صلاحية البحث في سجل التحويلات."); return
            key=raw.strip().lstrip("@").strip()
            if not key: await msg.reply_text("أدخل ID أو اسم مستخدم للبحث."); return
            if key.isdigit(): rows=db.execute("SELECT chat_id,chat_title,user_id,display_name,username,message_text,created_at,content_type,forward_origin,message_link,media_file_id FROM message_archive WHERE user_id=? ORDER BY id DESC LIMIT 100",(int(key),)).fetchall()
            else: rows=db.execute("SELECT chat_id,chat_title,user_id,display_name,username,message_text,created_at,content_type,forward_origin,message_link,media_file_id FROM message_archive WHERE LOWER(username)=LOWER(?) ORDER BY id DESC LIMIT 100",(key,)).fetchall()
            if not is_owner(user.id): rows=[row for row in rows if row[0] in allowed_archive_groups and can_use_capability(user.id,"archive",row[0])]
            rows=rows[:15]
            if not rows: await msg.reply_text("لا توجد نتائج ضمن القروبات المصرح لك بها. البحث يقتصر على الرسائل التي أرشفها البوت بعد تفعيل الأرشفة."); return
            out=["📨 نتائج سجل التحويلات والملفات"]
            for cid,title,tid,name,username,body,created,kind,origin,link,media_file_id in rows:
                media_note = "\n📎 مرفق وسائط محفوظ بالمعرّف" if media_file_id else ""
                out.append(f"\n📍 {title or cid} ({cid})\n👤 {name or 'عضو'} — ID: {tid}" + (f" (@{username})" if username else "") + f"\n🕒 {created} | النوع: {kind or 'غير محدد'}\n↪️ المصدر: {origin or 'غير متاح'}\n📄 {(body or '[محتوى غير نصي]')[:180]}{media_note}" + (f"\n🔗 {link}" if link else "\n🔗 رابط الرسالة غير متاح"))
            await msg.reply_text("\n".join(out)[:3900],disable_web_page_preview=True); return
    # A status request must never be consumed as the next value of another private workflow.
    if _is_aistatus_alias(raw):
        return
    if any(context.user_data.get(key) is not None for key in (
        "external_support_state", "mass_ban_state", "support_reply_ticket_id",
        "awaiting_coursework_page", "awaiting_calendar_search", "owner_autoreply_state",
        "awaiting_general_inquiry", "inquiry_reply_id", "awaiting_channel_broadcast",
        "awaiting_broadcast", "awaiting_delegated_owner_id", "awaiting_staff_permissions_id",
        "awaiting_unified_target_id", "awaiting_warning_grant", "awaiting_warning_log_search", "awaiting_archive_search",
        "awaiting_admin_action", "staff_task_state", "staff_shift_state", "staff_appeal_state",
        "staff_inquiry_state", "staff_badge_state"
    )):
        context.user_data["_skip_assistant_private_message_id"] = msg.message_id
    support_state = context.user_data.get("external_support_state")
    if support_state:
        if raw.lower() == "/cancel":
            context.user_data.pop("external_support_state", None)
            await msg.reply_text("تم إلغاء طلب الدعم."); return
        if len(raw) < 5:
            await msg.reply_text("يرجى كتابة وصف أوضح للمشكلة (5 أحرف على الأقل)، أو /cancel للإلغاء."); return
        chat_id = int(support_state.get("chat_id", 0) or 0)
        group_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if not group_row:
            context.user_data.pop("external_support_state", None)
            await msg.reply_text("القروب غير مسجل في البوت؛ تعذر إنشاء طلب الدعم."); return
        cur = db.execute("""INSERT INTO external_support_tickets
            (chat_id,group_title,requester_id,requester_name,requester_username,message,status,created_at)
            VALUES(?,?,?,?,?,?,'new',?)""",
            (chat_id, group_row[0] or str(chat_id), user.id, user.full_name or "", user.username or "", raw[:3500], now()))
        db.commit()
        ticket_id = cur.lastrowid
        context.user_data.pop("external_support_state", None)
        await msg.reply_text(f"✅ تم إرسال طلب الدعم رقم #{ticket_id}. سيصلك الرد هنا عند معالجة الطلب.")
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"🆘 طلب دعم فني جديد #{ticket_id}\nالقروب: {group_row[0] or chat_id} ({chat_id})\n"
                     f"المرسل: {html.escape(user.full_name or 'مستخدم')} (ID: {user.id})"
                     f"{' — @'+html.escape(user.username) if user.username else ''}\n\n{html.escape(raw[:3000])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📨 فتح الطلب والرد", callback_data=f"support_ticket:{ticket_id}")]])
            )
        except Exception as exc:
            log.warning("Failed to notify owner about support ticket %s: %s", ticket_id, exc)
        return

    mass_state = context.user_data.get("mass_ban_state")
    if mass_state:
        if raw.lower() == "/cancel":
            context.user_data.pop("mass_ban_state", None)
            await msg.reply_text("تم إلغاء عملية الحظر الجماعي."); return
        target_id = int(raw) if raw.isdigit() else 0
        username = raw.lstrip("@").strip() if raw.startswith("@") else ""
        target_row = None
        if target_id:
            target_row = db.execute("SELECT display_name,username FROM member_activity WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (target_id,)).fetchone()
            target_name = target_row[0] if target_row else f"المستخدم {target_id}"
            target_username = target_row[1] if target_row else ""
        elif username:
            target_row = db.execute("SELECT user_id,display_name,username FROM member_activity WHERE lower(username)=lower(?) ORDER BY message_count DESC LIMIT 1", (username,)).fetchone()
            if not target_row:
                await msg.reply_text("لم أجد اسم المستخدم ضمن الأعضاء الذين رصدهم البوت. أرسل ID الصحيح أو الاسم الظاهر في القروب، أو /cancel."); return
            target_id, target_name, target_username = int(target_row[0]), target_row[1] or "عضو", target_row[2] or username
        elif raw:
            candidates = db.execute("""SELECT user_id,MAX(display_name),MAX(username),SUM(message_count) AS activity
                FROM member_activity WHERE lower(display_name)=lower(?) OR lower(display_name) LIKE lower(?)
                GROUP BY user_id ORDER BY activity DESC LIMIT 3""", (raw, "%" + raw + "%")).fetchall()
            if len(candidates) != 1:
                if candidates:
                    choices = "\n".join(f"• {r[1] or 'عضو'} (@{r[2]}) — ID: {r[0]}" if r[2] else f"• {r[1] or 'عضو'} — ID: {r[0]}" for r in candidates)
                    await msg.reply_text("وجدت أكثر من عضو أو اسمًا غير فريد. أرسل رقم ID لتحديد الشخص بدقة:\n" + choices); return
                await msg.reply_text("لم أجد هذا الاسم ضمن الأعضاء الذين رصدهم البوت. أرسل ID أو @username معروفًا، أو /cancel."); return
            target_id, target_name, target_username = int(candidates[0][0]), candidates[0][1] or "عضو", candidates[0][2] or ""
        else:
            await msg.reply_text("أرسل رقم ID أو @username أو الاسم الظاهر في القروب، أو /cancel للإلغاء."); return
        scope = mass_state.get("scope", "all")
        group_ids = list(dict.fromkeys(int(x) for x in mass_state.get("groups", []))) if scope == "selected" else [
            int(r[0]) for r in db.execute("SELECT chat_id FROM watched_groups ORDER BY chat_id").fetchall()
        ]
        if not group_ids:
            context.user_data.pop("mass_ban_state", None)
            await msg.reply_text("لا توجد قروبات مستهدفة للحظر."); return
        db.execute("""INSERT INTO global_bans(user_id,display_name,username,reason,scope,active,created_by,created_at)
            VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,
            username=excluded.username,reason=excluded.reason,scope=excluded.scope,active=1,created_by=excluded.created_by,created_at=excluded.created_at""",
            (target_id, target_name or "عضو", target_username or "", mass_state.get("reason","حظر إداري جماعي"),
             scope, user.id, now()))
        db.execute("DELETE FROM global_ban_groups WHERE user_id=?", (target_id,))
        if scope == "selected":
            db.executemany("INSERT OR IGNORE INTO global_ban_groups(user_id,chat_id) VALUES(?,?)", [(target_id, cid) for cid in group_ids])
        db.commit()
        success = failed = 0
        for cid in group_ids:
            if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (cid,)).fetchone():
                failed += 1; continue
            try:
                await context.bot.ban_chat_member(chat_id=cid, user_id=target_id)
                log_action(cid, user.id, "global_ban", f"target={target_id}; scope={scope}")
                success += 1
            except Exception as exc:
                failed += 1
                log.warning("Global ban failed chat=%s target=%s: %s", cid, target_id, exc)
        context.user_data.pop("mass_ban_state", None)
        await msg.reply_text(
            f"🚫 اكتمل تنفيذ الحظر {'الشامل' if scope == 'all' else 'الجزئي'}.\n"
            f"العضو: {target_name} (ID: {target_id})\nالقروبات المستهدفة: {len(group_ids)}\n"
            f"تم الحظر: {success}\nتعذر الحظر: {failed}\n\n"
            "يُحاول النظام منع العضو من العودة إلى القروبات المشمولة بالحظر عند اكتشاف محاولة انضمام."
        )
        return

    support_reply_id = context.user_data.get("support_reply_ticket_id")
    if support_reply_id:
        if not is_owner(user.id):
            context.user_data.pop("support_reply_ticket_id", None)
            await msg.reply_text("الرد على طلبات الدعم متاح للمالك الأساسي فقط."); return
        if raw.lower() == "/cancel":
            context.user_data.pop("support_reply_ticket_id", None)
            await msg.reply_text("تم إلغاء الرد."); return
        ticket = db.execute("SELECT chat_id,requester_id,group_title FROM external_support_tickets WHERE id=?", (support_reply_id,)).fetchone()
        if not ticket:
            context.user_data.pop("support_reply_ticket_id", None)
            await msg.reply_text("طلب الدعم غير موجود."); return
        db.execute("UPDATE external_support_tickets SET status='answered',answered_by=?,answer=?,answered_at=? WHERE id=?",
                   (user.id, raw[:3500], now(), support_reply_id))
        db.commit()
        context.user_data.pop("support_reply_ticket_id", None)
        try:
            await context.bot.send_message(chat_id=ticket[1], text=f"📩 رد الدعم الفني على طلبك #{support_reply_id}\nالقروب: {ticket[2]}\n\n{raw[:3500]}")
            await msg.reply_text(f"✅ تم إرسال الرد على الطلب #{support_reply_id}.")
        except Exception as exc:
            await msg.reply_text(f"⚠️ تم حفظ الرد، لكن تعذر إرساله إلى صاحب الطلب: {exc}")
        return

    if context.user_data.get("awaiting_coursework_page"):
        page_no = int(context.user_data.pop("awaiting_coursework_page"))
        if raw.lower() == "/cancel":
            await msg.reply_text("تم إلغاء تحرير الصفحة.", reply_markup=coursework_menu_markup()); return
        if len(raw) > 12000:
            await msg.reply_text("المحتوى طويل جدًا؛ أرسل نصًا لا يتجاوز 12000 حرف.", reply_markup=coursework_edit_markup(page_no)); return
        timestamp_now = datetime.now(ZoneInfo("Asia/Riyadh")).isoformat(timespec="seconds")
        db.execute("""INSERT INTO coursework_pages(user_id,page_no,page_title,content,updated_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(user_id,page_no) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
                   (user.id, page_no, f"الأعمال الفصلية - الصفحة {page_no}", raw, timestamp_now))
        db.commit()
        await msg.reply_text(f"✅ تم حفظ الصفحة {page_no} بنجاح. يمكنك الرجوع إليها وتعديلها في أي وقت.", reply_markup=coursework_edit_markup(page_no)); return
    if context.user_data.get("awaiting_calendar_search"):
        context.user_data.pop("awaiting_calendar_search", None)
        if raw.lower() == "/cancel":
            await msg.reply_text("تم إلغاء البحث.", reply_markup=assistant_menu_markup()); return
        result = await calendar_date_text(raw)
        if result:
            await msg.reply_text(result, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 بحث عن تاريخ آخر", callback_data="calendar:search")], [InlineKeyboardButton("📅 فتح التقويم", callback_data="calendar:current")], [InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="calendar:back")]])); return
        # Event search across nearby calendar months; this searches the event labels supplied by the calendar service.
        found = []
        today = datetime.now(ZoneInfo("Asia/Riyadh"))
        month_targets = []
        for offset in range(0, 13):
            total = today.year * 12 + (today.month - 1) + offset
            month_targets.append((total // 12, total % 12 + 1))
        month_results = await asyncio.gather(*(calendar_month_text(yy, mm) for yy, mm in month_targets), return_exceptions=True)
        for (yy, mm), month_text in zip(month_targets, month_results):
            if isinstance(month_text, str) and month_text:
                for line in month_text.splitlines():
                    if raw.casefold() in line.casefold() and ("—" in line or "هـ" in line):
                        found.append(f"{yy}-{mm:02d}: {line.strip()}")
            if len(found) >= 15: break
        if found:
            await msg.reply_text("🔎 نتائج البحث في المناسبات والتقويم:\n\n" + "\n".join(found[:15]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 بحث جديد", callback_data="calendar:search")], [InlineKeyboardButton("📅 التقويم", callback_data="calendar:current")]])); return
        await msg.reply_text("لم أجد تاريخًا أو مناسبة مطابقة. جرّب تاريخًا مثل 2026-10-16 أو 16/10/2026، أو كلمة مثل رمضان أو عرفة.\nأرسل /cancel للإلغاء.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 إعادة البحث", callback_data="calendar:search")]])); return
    # Prevent other private-text handlers from also treating workflow messages as assistant prompts.
    if (context.user_data.get("staff_task_state") or context.user_data.get("staff_shift_state") or context.user_data.get("staff_appeal_state") or context.user_data.get("staff_inquiry_state") or context.user_data.get("staff_badge_state") or context.user_data.get("inquiry_reply_id") or context.user_data.get("awaiting_general_inquiry")
        or context.user_data.get("awaiting_channel_broadcast") or context.user_data.get("owner_autoreply_state")
        or context.user_data.get("awaiting_broadcast") or context.user_data.get("awaiting_delegated_owner_id") or context.user_data.get("awaiting_staff_permissions_id")
        or context.user_data.get("awaiting_admin_action")):
        context.user_data["_skip_assistant_private_message_id"] = msg.message_id
    # Allow the owner/delegated owner to award a badge directly with ID|badge|reason,
    # without first opening the staff panel. Keep this ahead of the assistant fallback.
    if (not context.user_data.get("staff_task_state")
            and not context.user_data.get("staff_shift_state")
            and not context.user_data.get("staff_appeal_state")
            and not context.user_data.get("staff_inquiry_state")
            and not context.user_data.get("inquiry_reply_id")
            and not context.user_data.get("awaiting_general_inquiry")
            and not context.user_data.get("awaiting_channel_broadcast")
            and not context.user_data.get("owner_autoreply_state")
            and not context.user_data.get("awaiting_broadcast")
            and not context.user_data.get("awaiting_delegated_owner_id")
            and not context.user_data.get("awaiting_staff_permissions_id")
            and not context.user_data.get("awaiting_admin_action")
            and not context.user_data.get("staff_badge_state")
            and (is_owner(user.id) or is_delegated_owner(user.id))
            and raw.count("|") >= 2):
        badge_parts = [part.strip() for part in raw.split("|", 2)]
        if len(badge_parts) == 3 and badge_parts[0].isdigit() and badge_parts[1]:
            target = int(badge_parts[0])
            badge = badge_parts[1][:80]
            reason = badge_parts[2][:300]
            db.execute(
                "INSERT INTO staff_badges(user_id,badge,awarded_by,reason,created_at) VALUES(?,?,?,?,?)",
                (target, badge, user.id, reason, now()),
            )
            db.commit()
            log_action(0, user.id, "staff_badge_awarded", f"target={target}; badge={badge}; reason={reason}")
            notified = True
            try:
                await context.bot.send_message(
                    target,
                    f"🏅 حصلت على وسام: {badge}\nالسبب: {reason or 'تقدير لجهودك'}",
                )
            except Exception:
                notified = False
            await msg.reply_text(
                f"✅ تم حفظ الوسام للعضو {target}.\n🏅 الوسام: {badge}\n"
                f"📝 السبب: {reason or 'لم يُحدّد'}\n"
                + ("📩 تم إرسال إشعار للعضو." if notified else "ℹ️ حُفظ الوسام، لكن تعذّر إشعار العضو في الخاص؛ قد يحتاج إلى بدء محادثة مع البوت.")
            )
            return
    # Primary-owner inquiry replies are deliberately not delegated.
    staff_task_state = context.user_data.get("staff_task_state")
    if staff_task_state:
        context.user_data.pop("staff_task_state", None)
        if raw.strip().lower() == "/cancel":
            await msg.reply_text("تم إلغاء إنشاء المهمة."); return
        parts = [x.strip() for x in raw.split("|", 2)]
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
            await msg.reply_text("الصيغة: رقم_المشرف | عنوان المهمة | التفاصيل (اختياري)\nمثال: 123456789 | متابعة القروب | الرد على الاستفسارات")
            context.user_data["staff_task_state"] = True; return
        target_id = int(parts[0]); title = parts[1][:120]; details = parts[2][:1000] if len(parts)>2 else ""
        cur = db.execute("INSERT INTO staff_tasks(chat_id,assigned_to,assigned_by,title,details,status,due_at,created_at,updated_at) VALUES(0,?,?,?,?, 'open','',?,?)", (target_id, user.id, title, details, now(), now()))
        db.commit(); task_id = cur.lastrowid
        log_action(0, user.id, "staff_task_assigned", f"task={task_id}; target={target_id}; title={title}")
        try: await context.bot.send_message(target_id, f"📋 تكليف جديد #{task_id}\n{title}\n{details}\n\nلإنهائه استخدم /staffdone {task_id}")
        except Exception: pass
        await msg.reply_text(f"✅ تم تسجيل المهمة #{task_id} وإسنادها إلى {target_id}."); return
    staff_shift_state = context.user_data.get("staff_shift_state")
    if staff_shift_state:
        context.user_data.pop("staff_shift_state", None)
        if raw.strip().lower() == "/cancel": await msg.reply_text("تم إلغاء المناوبة."); return
        parts = [x.strip() for x in raw.split("|", 2)]
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
            await msg.reply_text("الصيغة: رقم_المشرف | موعد بداية المناوبة | نهاية المناوبة (اختياري)")
            context.user_data["staff_shift_state"] = True; return
        target_id=int(parts[0]); starts=parts[1][:80]; ends=parts[2][:80] if len(parts)>2 else ""
        db.execute("INSERT INTO staff_shifts(chat_id,user_id,assigned_by,starts_at,ends_at,status,created_at) VALUES(0,?,?,?,?,'scheduled',?)",(target_id,user.id,starts,ends,now())); db.commit(); log_action(0,user.id,"staff_shift_assigned",f"target={target_id}; starts={starts}; ends={ends}")
        try: await context.bot.send_message(target_id, f"🗓️ تم تعيين مناوبة لك\nالبداية: {starts}\nالنهاية: {ends or 'غير محددة'}")
        except Exception: pass
        await msg.reply_text("✅ تم تسجيل المناوبة."); return
    staff_badge_state = context.user_data.get("staff_badge_state")
    if staff_badge_state:
        context.user_data.pop("staff_badge_state", None)
        if not (is_owner(user.id) or is_delegated_owner(user.id)):
            await msg.reply_text("هذه الوظيفة للمالك والمفوّض فقط."); return
        if raw.strip().lower() == "/cancel": await msg.reply_text("تم إلغاء منح الوسام."); return
        parts=[x.strip() for x in raw.split("|",2)]
        if len(parts)<2 or not parts[0].isdigit() or not parts[1]:
            await msg.reply_text("الصيغة: رقم_المشرف | اسم الوسام | السبب (اختياري)")
            context.user_data["staff_badge_state"]=True; return
        target=int(parts[0]); badge=parts[1][:80]; reason=parts[2][:300] if len(parts)>2 else ""
        db.execute("INSERT INTO staff_badges(user_id,badge,awarded_by,reason,created_at) VALUES(?,?,?,?,?)",(target,badge,user.id,reason,now())); db.commit(); log_action(0,user.id,"staff_badge_awarded",f"target={target}; badge={badge}; reason={reason}")
        try: await context.bot.send_message(target,f"🏅 حصلت على وسام: {badge}\nالسبب: {reason or 'تقدير لجهودك'}")
        except Exception: pass
        await msg.reply_text("✅ تم تسجيل الوسام وإرسال إشعار للمشرف إن أمكن."); return
    staff_inquiry_state = context.user_data.get("staff_inquiry_state")
    if staff_inquiry_state:
        context.user_data.pop("staff_inquiry_state", None)
        if raw.strip().lower() == "/cancel": await msg.reply_text("تم إلغاء الاستفسار."); return
        cur=db.execute("INSERT INTO staff_inquiries(user_id,chat_id,body,status,created_at) VALUES(?,0,?,'new',?)",(user.id,raw[:3000],now())); db.commit(); iid=cur.lastrowid
        await notify_owners(context, f"📨 استفسار طاقم جديد #{iid}\nمن: {user.full_name} ({user.id})\n\n{raw[:2500]}", "membership")
        await msg.reply_text("✅ وصل استفسارك إلى المالك الأساسي."); return
    reply_state = context.user_data.get("inquiry_reply_id")
    if reply_state:
        if not is_owner(user.id):
            context.user_data.pop("inquiry_reply_id", None); return
        reply_text = (msg.text or "").strip()
        if not reply_text or reply_text.startswith("/"):
            await msg.reply_text("أرسل نص الرد، أو /cancel للإلغاء."); return
        row = db.execute("SELECT user_id,status FROM general_inquiries WHERE id=?", (int(reply_state),)).fetchone()
        if not row:
            context.user_data.pop("inquiry_reply_id", None); await msg.reply_text("الاستفسار غير موجود."); return
        try:
            await context.bot.send_message(row[0], f"📨 رد المالك على استفسارك رقم #{reply_state}:\n\n{reply_text}")
            db.execute("UPDATE general_inquiries SET status='answered',reply_text=?,replied_at=? WHERE id=?", (reply_text, now(), int(reply_state))); db.commit()
            await msg.reply_text(f"✅ تم إرسال الرد لصاحب الاستفسار #{reply_state}.")
        except Exception:
            await msg.reply_text("تعذر إرسال الرد؛ يجب أن يكون صاحب الاستفسار قد بدأ محادثة مع البوت ولم يحظره.")
        context.user_data.pop("inquiry_reply_id", None); return
    if context.user_data.get("awaiting_general_inquiry"):
        raw = (msg.text or "").strip()
        if raw == "/cancel":
            context.user_data.pop("awaiting_general_inquiry", None); await msg.reply_text("تم إلغاء الاستفسار."); return
        if not raw or raw.startswith("/") or len(raw) > 3000:
            await msg.reply_text("أرسل نصًا واضحًا لا يتجاوز 3000 حرف، أو /cancel للإلغاء."); return
        cur = db.execute("INSERT INTO general_inquiries(user_id,username,display_name,inquiry_text,status,created_at) VALUES(?,?,?,?,?,?)", (user.id, user.username or "", user.full_name or "", raw, "new", now()))
        inquiry_id = cur.lastrowid; db.commit(); context.user_data.pop("awaiting_general_inquiry", None)
        await msg.reply_text(f"✅ تم استلام استفسارك برقم #{inquiry_id}. سيصل الرد إليك هنا في الخاص عند مراجعته.")
        if OWNER_ID:
            try:
                await context.bot.send_message(OWNER_ID,
                    f"📨 استفسار عام جديد #{inquiry_id}\nالاسم: {user.full_name}\nالمعرّف: {user.id}\nاسم المستخدم: @{user.username or 'غير متوفر'}\n\n{raw}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ الرد على الاستفسار", callback_data=f"inquiry_reply:{inquiry_id}"), InlineKeyboardButton("✅ إغلاق", callback_data=f"inquiry_close:{inquiry_id}")]]))
            except Exception as exc:
                log.warning("could not notify primary owner of inquiry: %s", exc)
                await msg.reply_text("تم حفظ الطلب، لكن تعذر تنبيه المالك الآن. تأكد أن المالك بدأ محادثة مع البوت باستخدام /start.")
        return
    if context.user_data.get("awaiting_channel_broadcast") and is_owner(user.id):
        context.user_data["channel_broadcast_draft"] = {"chat_id": update.effective_chat.id, "message_id": msg.message_id}
        context.user_data["awaiting_channel_broadcast"] = False
        targets = context.user_data.get("channel_broadcast_selected", [])
        await msg.reply_text(f"👀 معاينة النشر للقنوات\nعدد القنوات المستهدفة: {len(targets)}\nهل تؤكد النشر؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد النشر", callback_data="channel_pub_confirm")],[InlineKeyboardButton("❌ إلغاء", callback_data="channel_pub_cancel")]]))
        return


async def channel_post_listener(update, context):
    """Receives channel posts only from channels linked by the primary owner."""
    post = update.channel_post
    if not post:
        return
    row = db.execute("SELECT title FROM linked_channels WHERE chat_id=?", (post.chat.id,)).fetchone()
    if not row:
        return
    text_value = (post.text or post.caption or "").strip()
    if text_value.startswith("/botstatus"):
        await context.bot.send_message(post.chat.id, "🟢 البوت مرتبط بهذه القناة ويستقبل منشوراتها. لإدارة النشر استخدم /panel في الخاص مع البوت.")


def coursework_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 الصفحة 1", callback_data="coursework:page:1"), InlineKeyboardButton("📄 الصفحة 2", callback_data="coursework:page:2")],
        [InlineKeyboardButton("📄 الصفحة 3", callback_data="coursework:page:3"), InlineKeyboardButton("📄 الصفحة 4", callback_data="coursework:page:4")],
        [InlineKeyboardButton("➕ تحرير/حفظ صفحة", callback_data="coursework:choose_edit")],
        
    ])


def coursework_edit_markup(page_no):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تعديل محتوى الصفحة", callback_data=f"coursework:edit:{page_no}")],
        [InlineKeyboardButton("🧹 مسح محتوى الصفحة", callback_data=f"coursework:clear:{page_no}")],
        [InlineKeyboardButton("⬅️ صفحات الأعمال الفصلية", callback_data="coursework_portal_help")]
    ])


def assistant_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 ملاحظاتي", callback_data="assistant_notes_help"), InlineKeyboardButton("⏰ تذكير جديد", callback_data="assistant_reminder_help")],
        [InlineKeyboardButton("📋 مهامي", callback_data="assistant_tasks_help"), InlineKeyboardButton("🧮 أدواتي الذكية", callback_data="assistant_tools_help")],
        [InlineKeyboardButton("🎓 جدولي الدراسي", callback_data="student_schedule_help"), InlineKeyboardButton("🗓️ اختباراتي ومواعيدي", callback_data="student_events_help")],
        [InlineKeyboardButton("📅 التقويم الهجري والميلادي", callback_data="calendar:home"), InlineKeyboardButton("🤲 الأذكار والأدعية", callback_data="adhkar:home")],
        [InlineKeyboardButton("📄 تلخيص نص/ملف", callback_data="assistant_summary_help"), InlineKeyboardButton("🎓 اختبار تدريبي", callback_data="assistant_quiz_help")],
        [InlineKeyboardButton("📊 استخدامي وخصوصيتي", callback_data="assistant_privacy")],
        [InlineKeyboardButton("💬 الاستفسارات العامة (خاص)", callback_data="inquiry_start")],
        [InlineKeyboardButton("🛡️ لوحة حماية القروبات", callback_data="assistant_group_panel")],
        [InlineKeyboardButton("📌 طريقة تفعيل البوت في قروبك", callback_data="activation_help")]
    ])


ADHKAR = {
    "morning": ("🌅 أذكار الصباح", [
        "أَعُوذُ بِاللَّهِ مِنَ الشَّيْطَانِ الرَّجِيمِ، اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ الْحَيُّ الْقَيُّومُ... (آية الكرسي — البقرة: 255)",
        "قُلْ هُوَ اللَّهُ أَحَدٌ، اللَّهُ الصَّمَدُ، لَمْ يَلِدْ وَلَمْ يُولَدْ، وَلَمْ يَكُن لَّهُ كُفُوًا أَحَدٌ. (الإخلاص)",
        "اللَّهُمَّ بِكَ أَصْبَحْنَا، وَبِكَ أَمْسَيْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ النُّشُورُ.",
        "رَضِيتُ بِاللَّهِ رَبًّا، وَبِالْإِسْلَامِ دِينًا، وَبِمُحَمَّدٍ ﷺ نَبِيًّا. (ثلاث مرات)",
        "اللَّهُمَّ إِنِّي أَسْأَلُكَ الْعَفْوَ وَالْعَافِيَةَ فِي الدُّنْيَا وَالْآخِرَةِ."
    ]),
    "evening": ("🌇 أذكار المساء", [
        "قُلْ هُوَ اللَّهُ أَحَدٌ... (سورة الإخلاص، وتُقرأ مع المعوذتين)",
        "أَمْسَيْنَا وَأَمْسَى الْمُلْكُ لِلَّهِ، وَالْحَمْدُ لِلَّهِ، لَا إِلَٰهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ.",
        "رَضِيتُ بِاللَّهِ رَبًّا، وَبِالْإِسْلَامِ دِينًا، وَبِمُحَمَّدٍ ﷺ نَبِيًّا. (ثلاث مرات)",
        "اللَّهُمَّ إِنِّي أَسْأَلُكَ الْعَفْوَ وَالْعَافِيَةَ فِي الدُّنْيَا وَالْآخِرَةِ."
    ]),
    "sleep": ("🌙 أذكار النوم", [
        "بِاسْمِكَ اللَّهُمَّ أَمُوتُ وَأَحْيَا.",
        "اللَّهُمَّ قِنِي عَذَابَكَ يَوْمَ تَبْعَثُ عِبَادَكَ.",
        "قراءة آية الكرسي قبل النوم.",
        "جمع الكفين وقراءة الإخلاص والفلق والناس، ثم النفث فيهما ومسح ما استطاع من الجسد، ثلاث مرات."
    ]),
    "general": ("🤲 أدعية عامة", [
        "رَبَّنَا آتِنَا فِي الدُّنْيَا حَسَنَةً وَفِي الْآخِرَةِ حَسَنَةً وَقِنَا عَذَابَ النَّارِ. (البقرة: 201)",
        "رَبِّ زِدْنِي عِلْمًا. (طه: 114)",
        "اللَّهُمَّ إِنِّي أَسْأَلُكَ الْهُدَى وَالتُّقَى وَالْعَفَافَ وَالْغِنَى.",
        "اللَّهُمَّ أَعِنِّي عَلَى ذِكْرِكَ وَشُكْرِكَ وَحُسْنِ عِبَادَتِكَ."
    ])
}


def format_evening_adhkar_message():
    """Build a compact, readable Arabic evening-adhkar post for Telegram."""
    return (
        "🌙 <b>أَذْكَارُ الْمَسَاءِ</b>\n"
        "<i>طمأنينةٌ للقلب، وذكرٌ لله في ختام اليوم</i>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📖 <b>آية الكرسي</b>\n"
        "اللَّهُ لَا إِلَٰهَ إِلَّا هُوَ الْحَيُّ الْقَيُّومُ ۚ لَا تَأْخُذُهُ سِنَةٌ وَلَا نَوْمٌ ۚ لَهُ مَا فِي السَّمَاوَاتِ وَمَا فِي الْأَرْضِ ۗ مَنْ ذَا الَّذِي يَشْفَعُ عِنْدَهُ إِلَّا بِإِذْنِهِ ۚ يَعْلَمُ مَا بَيْنَ أَيْدِيهِمْ وَمَا خَلْفَهُمْ ۖ وَلَا يُحِيطُونَ بِشَيْءٍ مِنْ عِلْمِهِ إِلَّا بِمَا شَاءَ ۚ وَسِعَ كُرْسِيُّهُ السَّمَاوَاتِ وَالْأَرْضَ ۖ وَلَا يَئُودُهُ حِفْظُهُمَا ۚ وَهُوَ الْعَلِيُّ الْعَظِيمُ.\n\n"
        "🤲 <b>سورة الإخلاص</b> (ثلاث مرات)\n"
        "قُلْ هُوَ اللَّهُ أَحَدٌ ۝ اللَّهُ الصَّمَدُ ۝ لَمْ يَلِدْ وَلَمْ يُولَدْ ۝ وَلَمْ يَكُنْ لَهُ كُفُوًا أَحَدٌ.\n\n"
        "🤲 <b>سورة الفلق</b> (ثلاث مرات)\n"
        "قُلْ أَعُوذُ بِرَبِّ الْفَلَقِ ۝ مِنْ شَرِّ مَا خَلَقَ ۝ وَمِنْ شَرِّ غَاسِقٍ إِذَا وَقَبَ ۝ وَمِنْ شَرِّ النَّفَّاثَاتِ فِي الْعُقَدِ ۝ وَمِنْ شَرِّ حَاسِدٍ إِذَا حَسَدَ.\n\n"
        "🤲 <b>سورة الناس</b> (ثلاث مرات)\n"
        "قُلْ أَعُوذُ بِرَبِّ النَّاسِ ۝ مَلِكِ النَّاسِ ۝ إِلَٰهِ النَّاسِ ۝ مِنْ شَرِّ الْوَسْوَاسِ الْخَنَّاسِ ۝ الَّذِي يُوَسْوِسُ فِي صُدُورِ النَّاسِ ۝ مِنَ الْجِنَّةِ وَالنَّاسِ.\n\n"
        "🌿 <b>من أذكار المساء</b>\n"
        "أَمْسَيْنَا وَأَمْسَى الْمُلْكُ لِلَّهِ، وَالْحَمْدُ لِلَّهِ، لَا إِلَٰهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ، لَهُ الْمُلْكُ وَلَهُ الْحَمْدُ وَهُوَ عَلَى كُلِّ شَيْءٍ قَدِيرٌ. رَبِّ أَسْأَلُكَ خَيْرَ مَا فِي هَذِهِ اللَّيْلَةِ وَخَيْرَ مَا بَعْدَهَا، وَأَعُوذُ بِكَ مِنْ شَرِّ مَا فِي هَذِهِ اللَّيْلَةِ وَشَرِّ مَا بَعْدَهَا.\n\n"
        "اللَّهُمَّ بِكَ أَمْسَيْنَا، وَبِكَ أَصْبَحْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ الْمَصِيرُ.\n\n"
        "رَضِيتُ بِاللَّهِ رَبًّا، وَبِالْإِسْلَامِ دِينًا، وَبِمُحَمَّدٍ ﷺ نَبِيًّا. <b>(ثلاث مرات)</b>\n\n"
        "بِسْمِ اللَّهِ الَّذِي لَا يَضُرُّ مَعَ اسْمِهِ شَيْءٌ فِي الْأَرْضِ وَلَا فِي السَّمَاءِ وَهُوَ السَّمِيعُ الْعَلِيمُ. <b>(ثلاث مرات)</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤍 <i>اللهم اجعل مساءكم سكينةً وطمأنينة، واملأ قلوبكم بذكرك.</i>"
    )


def adhkar_auto_settings_markup(page=0):
    groups = db.execute(
        "SELECT w.chat_id, COALESCE(NULLIF(w.title,''), CAST(w.chat_id AS TEXT)), COALESCE(s.enabled,0) "
        "FROM watched_groups w LEFT JOIN adhkar_group_settings s ON s.chat_id=w.chat_id ORDER BY w.title,w.chat_id"
    ).fetchall()
    page_size = 30
    page_count = max(1, (len(groups) + page_size - 1) // page_size)
    page = max(0, min(int(page or 0), page_count - 1))
    rows = []
    for chat_id, title, enabled in groups[page * page_size:(page + 1) * page_size]:
        icon = "✅" if int(enabled) else "⬜"
        rows.append([InlineKeyboardButton(f"{icon} {str(title)[:42]}", callback_data=f"adhkar_auto_toggle:{int(chat_id)}:{page}")])
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد قروبات مسجلة", callback_data="noop")])
    rows.append([InlineKeyboardButton("🌙 تفعيل للجميع", callback_data="adhkar_auto_all_on"), InlineKeyboardButton("⏸️ إيقاف للجميع", callback_data="adhkar_auto_all_off")])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"adhkar_auto_page:{page - 1}"))
    navigation.append(InlineKeyboardButton(f"صفحة {page + 1}/{page_count}", callback_data="noop"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("التالي ➡️", callback_data=f"adhkar_auto_page:{page + 1}"))
    rows.append(navigation)
    rows.append([InlineKeyboardButton("🔄 تحديث", callback_data=f"adhkar_auto_page:{page}"), InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def adhkar_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌅 الصباح", callback_data="adhkar:morning"), InlineKeyboardButton("🌇 المساء", callback_data="adhkar:evening")],
        [InlineKeyboardButton("🌙 النوم", callback_data="adhkar:sleep"), InlineKeyboardButton("🤲 أدعية عامة", callback_data="adhkar:general")],
        [InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="adhkar:back")]
    ])


def calendar_markup(year, month):
    """Compact fallback controls when the calendar API is unavailable."""
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ السابق", callback_data=f"calendar:month:{prev_year}:{prev_month}"), InlineKeyboardButton("التالي ▶️", callback_data=f"calendar:month:{next_year}:{next_month}")],
        [InlineKeyboardButton("🔎 بحث عن تاريخ", callback_data="calendar:search"), InlineKeyboardButton("📆 اليوم", callback_data="calendar:current")],
        
    ])


async def calendar_grid_view(year, month):
    """Build an interactive month grid; each date button contains Gregorian and Hijri dates."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"https://api.aladhan.com/v1/gToHCalendar/{month}/{year}")
            response.raise_for_status()
            payload = response.json()
        days = payload.get("data") or []
        if not days:
            return None, calendar_markup(year, month)
        month_name = days[0].get("gregorian", {}).get("month", {}).get("en", str(month))
        hijri_names = []
        for item in days:
            hm = item.get("hijri", {}).get("month", {}).get("ar", "")
            if hm and hm not in hijri_names:
                hijri_names.append(hm)
        hijri_title = " – ".join(hijri_names[:2]) or "التاريخ الهجري"
        header = (f"🗓️ التقويم الهجري والميلادي\n"
                  f"📅 {month_name} {year}\n🌙 {hijri_title}\n"
                  "اضغط على أي يوم لعرض تفاصيله؛ قد يختلف التاريخ الهجري يومًا بحسب ثبوت الهلال.")
        # Telegram inline keyboards have a small callback payload; date cells use compact data.
        weekday_names = ["أحد", "إثنين", "ثلاثاء", "أربعاء", "خميس", "جمعة", "سبت"]
        rows = [[InlineKeyboardButton(name, callback_data="calendar:noop") for name in weekday_names]]
        by_day = {}
        for item in days:
            g = item.get("gregorian", {})
            h = item.get("hijri", {})
            try:
                n = int(g.get("day"))
            except (TypeError, ValueError):
                continue
            by_day[n] = (str(h.get("day", "?")), str(h.get("month", {}).get("ar", "")), str(h.get("year", "")))
        today = datetime.now(ZoneInfo("Asia/Riyadh"))
        month_grid = pycalendar.Calendar(firstweekday=6).monthdayscalendar(year, month)  # Sunday first
        for week in month_grid:
            row = []
            for n in week:
                if not n:
                    row.append(InlineKeyboardButton("·", callback_data="calendar:noop"))
                    continue
                hijri_day, hijri_month, hijri_year = by_day.get(n, ("?", "", ""))
                label = f"🔹{n}\n{hijri_day}هـ" if (year, month, n) == (today.year, today.month, today.day) else f"{n}\n{hijri_day}هـ"
                row.append(InlineKeyboardButton(label, callback_data=f"calendar:day:{year}:{month}:{n}"))
            rows.append(row)
        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        rows.extend([
            [InlineKeyboardButton("◀️ الشهر السابق", callback_data=f"calendar:month:{prev_year}:{prev_month}"), InlineKeyboardButton("الشهر التالي ▶️", callback_data=f"calendar:month:{next_year}:{next_month}")],
            [InlineKeyboardButton("🔎 بحث عن تاريخ", callback_data="calendar:search"), InlineKeyboardButton("📆 اليوم", callback_data="calendar:current")],
            
        ])
        return header, InlineKeyboardMarkup(rows)
    except Exception as exc:
        log.warning("calendar grid unavailable: %s", exc)
        return None, calendar_markup(year, month)


# Saudi school-year holidays (1448-1449 AH / 2026-2027), sourced from the published
# general-education calendar. These are date ranges for the general school calendar;
# some regions/institutions may have different schedules.
SAUDI_SCHOOL_HOLIDAYS_1448 = [
    ("إجازة اليوم الوطني", "2026-09-23", "2026-09-26"),
    ("إجازة الخريف", "2026-11-20", "2026-11-28"),
    ("إجازة منتصف العام الدراسي", "2027-01-08", "2027-01-16"),
    ("إجازة يوم التأسيس", "2027-02-19", "2027-02-22"),
    ("إجازة عيد الفطر المدرسية", "2027-02-06", "2027-03-13"),
    ("إجازة عيد الأضحى المدرسية", "2027-05-07", "2027-05-22"),
    ("إجازة نهاية العام الدراسي", "2027-06-24", "2027-06-24"),
]


def _school_holiday_lines_for_month(year, month):
    from datetime import date
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1)
    last = last.fromordinal(last.toordinal() - 1)
    lines = []
    for title, start_s, end_s in SAUDI_SCHOOL_HOLIDAYS_1448:
        start = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
        if start <= last and end >= first:
            lines.append(f"🎓 {title} — {start.strftime('%d/%m/%Y')} إلى {end.strftime('%d/%m/%Y')}")
    return lines


async def calendar_month_text(year, month):
    """Fetch Gregorian month with Hijri dates and holiday labels from AlAdhan API,
    then add Saudi school-holiday ranges for academic year 1448-1449."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"https://api.aladhan.com/v1/gToHCalendar/{month}/{year}")
            r.raise_for_status()
            payload = r.json()
        days = payload.get("data", [])
        if not days:
            return None
        month_name = days[0]["gregorian"]["month"].get("en", str(month))
        lines = [f"📅 التقويم الهجري والميلادي — {month_name} {year}", "📌 التاريخ الهجري المعروض حسب بيانات خدمة التقويم؛ قد تختلف بعض المناسبات بحسب ثبوت الهلال.", ""]
        for day in days:
            g = day.get("gregorian", {})
            h = day.get("hijri", {})
            holidays = h.get("holidays") or []
            holiday = (" — " + "، ".join(holidays)) if holidays else ""
            lines.append(f"{g.get('day','?')} {g.get('month',{}).get('en','')} | {h.get('day','?')} {h.get('month',{}).get('ar',h.get('month',{}).get('en',''))} {h.get('year','')}هـ{holiday}")
        school_holidays = _school_holiday_lines_for_month(year, month)
        if school_holidays:
            lines.extend(["", "🎓 إجازات العام الدراسي السعودي 1448-1449هـ:", *school_holidays])
        return "\n".join(lines)
    except Exception as exc:
        log.warning("calendar API unavailable: %s", exc)
        return None


async def calendar_date_text(query):
    """Convert a Gregorian date (YYYY-MM-DD or DD/MM/YYYY) to Hijri using AlAdhan."""
    q = (query or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", q)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", q)
        if not match:
            return None
        day, month, year = map(int, match.groups())
    try:
        date_obj = datetime(year, month, day)
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(f"https://api.aladhan.com/v1/gToH/{date_obj.strftime('%d-%m-%Y')}")
            r.raise_for_status()
            payload = r.json().get("data", {}).get("hijri", {})
        if not payload:
            return None
        g_month = date_obj.strftime("%B")
        h_month = payload.get("month", {}).get("ar", payload.get("month", {}).get("en", ""))
        holidays = payload.get("holidays") or []
        extra = "\n🎉 المناسبة: " + "، ".join(holidays) if holidays else ""
        return f"📅 التاريخ الميلادي: {day:02d}/{month:02d}/{year} ({g_month})\n🌙 التاريخ الهجري: {payload.get('day','?')} {h_month} {payload.get('year','')}هـ{extra}\n\nملاحظة: قد تختلف بعض المناسبات حسب ثبوت الهلال في السعودية."
    except Exception as exc:
        log.warning("calendar date conversion failed: %s", exc)
        return None


def _offline_summary(source, limit=8):
    import re
    text = re.sub(r"\s+", " ", source).strip()
    if not text:
        return "لم أجد نصًا قابلًا للتلخيص."
    sentences = re.split(r"(?<=[.!؟])\s+", text)
    sentences = [x.strip() for x in sentences if x.strip()]
    if len(sentences) <= limit:
        chosen = sentences
    else:
        # Extractive summary: select sentences distributed across the text; no AI or external API.
        indexes = sorted(set(round(i*(len(sentences)-1)/(limit-1)) for i in range(limit)))
        chosen = [sentences[i] for i in indexes]
    return "📄 ملخص استخراجي محلي (دون ذكاء اصطناعي):\n\n" + "\n".join(f"• {x[:500]}" for x in chosen)[:3500]


async def openai_assistant_response(user_text, chat_title="المحادثة"):
    """Generate a direct assistant answer through OpenAI API; returns Arabic error text on failure."""
    if not OPENAI_API_KEY:
        return None
    prompt = (user_text or "").strip()
    if not prompt:
        return "اكتب سؤالك بعد الأمر /ai، مثال: /ai اشرح لي مبادئ الإدارة."
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": "أنت المساعد التنفيذي لبوت تيليجرام. أجب بالعربية بوضوح واختصار. يمكنك اقتراح خطوات، لكن لا تدّعِ أنك نفذت إجراءً إداريًا داخل تيليجرام ما لم تنفذه وظيفة برمجية فعلية. لا تطلب مفاتيح أو كلمات مرور."},
                        {"role": "user", "content": f"السياق: {chat_title}\nالطلب: {prompt[:6000]}"}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 800
                }
            )
            if response.status_code == 401:
                log.warning("OpenAI API returned 401 Unauthorized")
                return "⚠️ مفتاح OpenAI مرفوض (401). تحقق من OPENAI_API_KEY في Render دون مشاركته."
            if response.status_code == 429:
                body = response.text[:500].lower()
                log.warning("OpenAI API returned 429")
                if "insufficient_quota" in body or "quota" in body or "billing" in body:
                    return "⚠️ تعذر استخدام الذكاء الاصطناعي بسبب الرصيد أو حصة الفوترة. تحقق من صفحة Usage وBilling في OpenAI."
                return "⚠️ وصلت طلبات الذكاء إلى حد السرعة مؤقتًا. انتظر قليلًا ثم أعد المحاولة."
            response.raise_for_status()
            data = response.json()
            answer = str(data["choices"][0]["message"]["content"]).strip()
            return answer[:3900] if answer else "لم يصل رد نصي من خدمة الذكاء الاصطناعي. حاول مرة أخرى."
    except httpx.TimeoutException:
        log.warning("OpenAI API request timed out")
        return "⚠️ انتهت مهلة الاتصال بخدمة الذكاء الاصطناعي. حاول مرة أخرى بعد قليل."
    except Exception as exc:
        log.exception("OpenAI assistant request failed: %s", exc)
        return "⚠️ تعذر الاتصال بخدمة الذكاء الاصطناعي. راجع Render Logs لمعرفة السبب، ولا تشارك المفاتيح السرية."


async def ai_cmd(update, context):
    """Explicit /ai command in private or registered groups with existing capability checks."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    # AI is restricted to the primary owner and delegated owners. Capability grants
    # for ordinary staff must not bypass this product-level access boundary.
    if chat.type == ChatType.PRIVATE and not (is_owner(user.id) or is_delegated_owner(user.id)):
        await msg.reply_text("⛔ استخدام المساعد الذكي متاح للمالك الأساسي والمالك المفوّض فقط.")
        return
    if not OPENAI_API_KEY:
        await msg.reply_text("⚠️ لم يتم ضبط OPENAI_API_KEY في بيئة Render.")
        return
    if chat.type == ChatType.PRIVATE:
        prompt = " ".join(context.args).strip()
        if not prompt:
            await msg.reply_text("اكتب سؤالك بعد الأمر، مثال:\n/ai اشرح لي مبادئ الإدارة")
            return
        await msg.reply_text("⏳ جاري إعداد الإجابة...")
        answer = await openai_assistant_response(prompt, "الخاص")
        await msg.reply_text(answer or "⚠️ خدمة الذكاء الاصطناعي غير متاحة.")
        return
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        registered = bool(db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat.id,)).fetchone())
        if not registered:
            await msg.reply_text("⛔ هذا القروب غير مسجل ضمن القروبات التي يديرها البوت.")
            return
        ext = external_group_info(chat.id)
        if ext and ext[0]:
            if not ext[1]:
                await msg.reply_text("⛔ خدمات البوت متوقفة عن هذا القروب من المالك الأساسي.")
                return
            if not ext[2] or not external_group_cap_enabled(chat.id, "ai"):
                await msg.reply_text("⛔ الذكاء الاصطناعي غير متاح للقروبات الخارجية افتراضيًا. يمكن للمالك الأساسي تفعيله يدويًا.")
                return
            if not (is_owner(user.id) or is_delegated_owner(user.id)):
                await msg.reply_text("⛔ استخدام المساعد الذكي في القروبات الخارجية متاح للمالك الأساسي والمالك المفوّض فقط.")
                return
        else:
            ai_setting = db.execute("SELECT enabled FROM ai_group_settings WHERE chat_id=?", (chat.id,)).fetchone()
            if not ai_setting or not ai_setting[0]:
                await msg.reply_text("⛔ الذكاء الاصطناعي غير مفعّل لهذا القروب من المالك الأساسي.")
                return
            if not (is_owner(user.id) or is_delegated_owner(user.id)):
                await msg.reply_text("⛔ استخدام المساعد الذكي في القروبات متاح للمالك الأساسي والمالك المفوّض فقط.")
                return
        prompt = " ".join(context.args).strip()
        if not prompt:
            await msg.reply_text("اكتب سؤالك بعد الأمر، مثال: /ai اشرح لي مبادئ الإدارة")
            return
        await msg.reply_text("⏳ جاري إعداد الإجابة...")
        answer = await openai_assistant_response(prompt, chat.title or "قروب تيليجرام")
        await msg.reply_text(answer or "⚠️ خدمة الذكاء الاصطناعي غير متاحة.")
        return
    await msg.reply_text("استخدم /ai في الخاص أو داخل قروب مسجل ومصرح به.")


async def aigroups_cmd(update, context):
    """Owner-only control: /aigroups all on|off, /aigroups CHAT_ID on|off, /aigroups list."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_owner(user.id):
        await msg.reply_text("⛔ هذه اللوحة للمالك الأساسي فقط.")
        return
    args = [str(x).strip() for x in (context.args or [])]
    usage = (
        "🤖 إدارة الذكاء الاصطناعي للقروبات\n\n"
        "/aigroups list — عرض القروبات ومعرّفاتها وحالة الذكاء\n"
        "/aigroups all on — تفعيل الذكاء لكل القروبات المسجلة\n"
        "/aigroups all off — إيقافه عن جميع القروبات\n"
        "/aigroups CHAT_ID on — تفعيل قروب محدد\n"
        "/aigroups CHAT_ID off — إيقاف قروب محدد\n\n"
        "عند التفعيل، يحاول البوت إرسال ترحيب مولّد بالذكاء الاصطناعي للقروب ويبلغك بالنتيجة."
    )
    if not args:
        await msg.reply_text(usage)
        return
    mode = args[0].lower()
    if mode == "list":
        rows = db.execute("SELECT w.chat_id, COALESCE(w.title,''), COALESCE(a.enabled, CASE WHEN e.is_external=1 THEN e.ai_enabled ELSE 0 END,0), COALESCE(e.is_external,0) FROM watched_groups w LEFT JOIN ai_group_settings a ON a.chat_id=w.chat_id LEFT JOIN external_group_control e ON e.chat_id=w.chat_id ORDER BY w.title").fetchall()
        if not rows:
            await msg.reply_text("لا توجد قروبات مسجلة حاليًا.")
            return
        lines = ["📋 حالة الذكاء الاصطناعي للقروبات:"]
        for cid, title, enabled, is_external in rows[:100]:
            lines.append(f"{'🟢' if enabled else '⚪️'} {title or 'قروب بلا اسم'} | {cid} | {'خارجي' if is_external else 'داخلي'}")
        if len(rows) > 100:
            lines.append(f"… توجد {len(rows)-100} قروبات إضافية.")
        await msg.reply_text("\n".join(lines)[:3900])
        return
    if len(args) != 2 or args[1].lower() not in ("on", "off"):
        await msg.reply_text(usage)
        return
    enable = int(args[1].lower() == "on")
    if mode == "all":
        ids = [int(r[0]) for r in db.execute("SELECT chat_id FROM watched_groups").fetchall()]
    else:
        try:
            ids = [int(args[0])]
        except ValueError:
            await msg.reply_text("❌ معرّف القروب غير صحيح. استخدم /aigroups list لمعرفة المعرفات.")
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (ids[0],)).fetchone():
            await msg.reply_text("❌ القروب غير مسجل لدى البوت. استخدم /aigroups list.")
            return
    if not ids:
        await msg.reply_text("لا توجد قروبات مسجلة لتطبيق الأمر عليها.")
        return
    succeeded = 0
    for cid in ids:
        ext = db.execute("SELECT is_external FROM external_group_control WHERE chat_id=?", (cid,)).fetchone()
        is_external = bool(ext and ext[0])
        try:
            if is_external:
                db.execute("UPDATE external_group_control SET ai_enabled=?,updated_at=? WHERE chat_id=?", (enable, now(), cid))
                db.execute("INSERT INTO external_group_capabilities(chat_id,capability,enabled,granted_by,granted_at) VALUES(?,'ai',?,?,?) ON CONFLICT(chat_id,capability) DO UPDATE SET enabled=excluded.enabled,granted_by=excluded.granted_by,granted_at=excluded.granted_at", (cid, enable, user.id, now()))
            else:
                db.execute("INSERT INTO ai_group_settings(chat_id,enabled,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (cid, enable, user.id, now()))
            db.commit()
            succeeded += 1
            title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (cid,)).fetchone()
            title = (title_row[0] if title_row else None) or str(cid)
            welcome_result = "لم يُرسل ترحيب (تم إيقاف الذكاء)."
            if enable:
                if not OPENAI_API_KEY:
                    welcome_result = "تعذّر الترحيب: OPENAI_API_KEY غير مضبوط في Render."
                else:
                    welcome = await openai_assistant_response(
                        f"اكتب رسالة ترحيب عربية قصيرة ولطيفة لأعضاء هذا القروب عند تفعيل المساعد الذكي. وضّح أن الذكاء الاصطناعي متاح للمساعدة والإجابة عن الأسئلة، ولا تدّعِ معرفة اسم مالك القروب. اجعلها مناسبة للنشر مباشرة، مع رموز تعبيرية قليلة.",
                        f"رسالة ترحيب لقروب {title}"
                    )
                    if welcome and not welcome.startswith("⚠️"):
                        try:
                            await context.bot.send_message(chat_id=cid, text="🤖 تم تفعيل المساعد الذكي في هذا القروب!\n\n" + welcome[:3300])
                            welcome_result = "تم إرسال ترحيب الذكاء الاصطناعي بنجاح."
                        except Exception as exc:
                            log.warning("AI welcome send failed for group %s: %s", cid, exc)
                            welcome_result = "تعذّر إرسال الترحيب داخل القروب؛ تحقق من صلاحية البوت في إرسال الرسائل."
                    else:
                        welcome_result = "تعذّر إنشاء الترحيب عبر OpenAI؛ راجع إعدادات الخدمة وسجلات Render."
            try:
                await context.bot.send_message(chat_id=OWNER_ID, text=f"🤖 تحديث الذكاء الاصطناعي\nالقروب: {title}\nالمعرّف: {cid}\nالحالة: {'مفعّل' if enable else 'متوقف'}\nالنتيجة: {welcome_result}")
            except Exception as exc:
                log.warning("Could not notify owner about AI group change %s: %s", cid, exc)
        except Exception as exc:
            log.exception("Failed to change AI setting for group %s: %s", cid, exc)
    await msg.reply_text(f"✅ تم {'تفعيل' if enable else 'إيقاف'} الذكاء الاصطناعي في {succeeded} من أصل {len(ids)} قروب.\nاستخدم /aigroups list لمراجعة الحالات. إشعار المالك والترحيب يعتمدان على صلاحية البوت والاتصال بالخدمة.")


async def offline_assistant_response(user_id, user_text):
    """Offline helper: no AI provider, network call, API key, or claim of generative AI."""
    text = (user_text or "").strip()
    lower = text.lower()
    if any(x in lower for x in ("السلام عليكم", "مرحبا", "هلا", "صباح الخير", "مساء الخير")):
        return "وعليكم السلام ورحمة الله وبركاته 🌷\nأنا مساعد خدمات يعمل محليًا دون ذكاء اصطناعي. أستطيع مساعدتك في الملاحظات والمهام والتذكيرات والمصاريف والجدول الدراسي ومواعيد الاختبارات. اضغط أحد أزرار القائمة أو اكتب /help."
    if any(x in lower for x in ("جدول", "اختبار", "مذاكرة", "دراسة", "واجب", "محاضرة")):
        return "🎓 لخدمات الدراسة استخدم:\n/studyadd اليوم | الوقت | المادة — إضافة مادة للجدول\n/studyschedule — عرض الجدول\n/exam التاريخ YYYY-MM-DD HH:MM | اسم الاختبار — إضافة موعد اختبار\n/exams — عرض الاختبارات والمواعيد\n/delexam رقم — حذف موعد"
    if any(x in lower for x in ("مهمة", "مهامي", "task")):
        return "📋 إدارة المهام: /task نص المهمة، /tasks لعرضها، /donetask رقم لإنجازها، /deltask رقم لحذفها."
    if any(x in lower for x in ("تذكير", "ذكرني", "موعد")):
        return "⏰ لإضافة تذكير: /remind YYYY-MM-DD HH:MM | نص التذكير، ولعرض التذكيرات: /reminders."
    return "ℹ️ هذا الإصدار لا يستخدم الذكاء الاصطناعي ولا يتصل بأي مزود خارجي. أستطيع تنفيذ الخدمات المحددة من القائمة مثل الملاحظات والمهام والتذكيرات والمصاريف والجدول الدراسي والاختبارات. استخدم /help لعرض الأوامر."


async def assistant_note_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("📝 لحفظ ملاحظة اكتب:\n/note نص الملاحظة\n\nلعرض آخر ملاحظاتك: /notes")
        return
    db.execute("INSERT INTO assistant_notes(user_id,note,created_at) VALUES(?,?,?)", (user.id,text[:2000],_assistant_now()))
    db.execute("UPDATE assistant_users SET notes_count=notes_count+1 WHERE user_id=?", (user.id,))
    db.commit()
    await update.effective_message.reply_text("✅ حفظت الملاحظة في حسابك الخاص.")


async def assistant_notes_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    rows = db.execute("SELECT id,note,created_at FROM assistant_notes WHERE user_id=? ORDER BY id DESC LIMIT 10", (user.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("ما عندك ملاحظات محفوظة. استخدم /note ثم اكتب ملاحظتك.")
        return
    body = "📝 آخر ملاحظاتك:\n\n" + "\n".join(f"{i}. {note} — {created[:10]}" for i,(note,created) in enumerate([(r[1],r[2]) for r in rows],1))
    await update.effective_message.reply_text(body[:4000])


async def assistant_remind_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.effective_message.reply_text("⏰ لإنشاء تذكير اكتب بهذا الشكل (بتوقيت السعودية):\n/remind 2026-10-12 18:30 | نص التذكير\nمثال: /remind 2026-10-12 18:30 | مراجعة المحاضرة")
        return
    when_text, reminder_text = [x.strip() for x in raw.split("|", 1)]
    if not reminder_text:
        await update.effective_message.reply_text("اكتب نص التذكير بعد علامة |.")
        return
    try:
        local_dt = datetime.strptime(when_text, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Riyadh"))
        if local_dt <= datetime.now(ZoneInfo("Asia/Riyadh")):
            raise ValueError("past")
    except ValueError:
        await update.effective_message.reply_text("صيغة التاريخ أو الوقت غير صحيحة، أو أن الموعد مضى. استخدم YYYY-MM-DD HH:MM بتوقيت السعودية.")
        return
    utc_dt = local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO assistant_reminders(user_id,reminder_text,remind_at,created_at) VALUES(?,?,?,?)", (user.id,reminder_text[:1000],utc_dt,_assistant_now()))
    db.execute("UPDATE assistant_users SET reminders_count=reminders_count+1 WHERE user_id=?", (user.id,))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم حفظ التذكير.\n📅 {local_dt.strftime('%Y-%m-%d %H:%M')} بتوقيت السعودية\n📝 {reminder_text[:1000]}")


async def assistant_reminders_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    rows = db.execute("SELECT id,reminder_text,remind_at FROM assistant_reminders WHERE user_id=? AND sent=0 ORDER BY remind_at LIMIT 20", (user.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد تذكيرات قادمة. لإنشاء تذكير استخدم /remind.")
        return
    lines = ["⏰ تذكيراتك القادمة:"]
    for rid, text, when in rows:
        try:
            local = datetime.fromisoformat(when).astimezone(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            local = when
        lines.append(f"#{rid} — {local}\n{text}")
    lines.append("\nلحذف تذكير: /delreminder رقم_التذكير")
    await update.effective_message.reply_text("\n\n".join(lines)[:4000])


async def assistant_delreminder_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استخدم: /delreminder رقم_التذكير (من /reminders).")
        return
    rid = int(context.args[0])
    cur = db.execute("DELETE FROM assistant_reminders WHERE id=? AND user_id=? AND sent=0", (rid,user.id))
    db.commit()
    await update.effective_message.reply_text("✅ تم حذف التذكير." if cur.rowcount else "لم أجد تذكيرًا قادمًا بهذا الرقم في حسابك.")


async def assistant_reminder_worker(app):
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            due = db.execute("SELECT id,user_id,reminder_text FROM assistant_reminders WHERE sent=0 AND remind_at<=? ORDER BY remind_at LIMIT 50", (now,)).fetchall()
            for rid, user_id, reminder_text in due:
                try:
                    await app.bot.send_message(chat_id=user_id, text="⏰ تذكيرك:\n" + reminder_text[:3500])
                    db.execute("UPDATE assistant_reminders SET sent=1 WHERE id=?", (rid,))
                except (Forbidden, BadRequest) as exc:
                    # Telegram has rejected delivery permanently (for example, the user blocked the bot).
                    log.warning("Reminder cannot be delivered to user %s (%s); marking it handled", user_id, type(exc).__name__)
                    db.execute("UPDATE assistant_reminders SET sent=1 WHERE id=?", (rid,))
                except Exception as exc:
                    # Keep it pending after transient/network errors so the next worker cycle can retry.
                    log.warning("Temporary reminder delivery failure for user %s (%s); will retry", user_id, type(exc).__name__)
            if due:
                db.commit()
            # Deliver scheduled student exams/deadlines when their time arrives.
            now_local = datetime.now().astimezone().isoformat(timespec="seconds")
            due_events = db.execute("SELECT id,user_id,title,event_at FROM student_events WHERE notified=0 AND event_at<=? ORDER BY event_at LIMIT 50", (now_local,)).fetchall()
            for event_id, event_user_id, title, event_at in due_events:
                try:
                    await app.bot.send_message(chat_id=event_user_id, text=f"🎓 تذكير بموعدك الدراسي: {title}\nالموعد: {event_at.replace('T',' ')[:16]}")
                except Exception as exc:
                    log.warning("Student event notification failed (%s)", type(exc).__name__)
                finally:
                    db.execute("UPDATE student_events SET notified=1 WHERE id=?", (event_id,))
            if due_events:
                db.commit()
            # Mini App reminders: event time is entered in the service's local clock (Saudi time).
            now_riyadh=datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%dT%H:%M")
            mini_due=db.execute("SELECT id,user_id,event_type,title,event_at,reminder_minutes FROM miniapp_events WHERE notified=0 AND datetime(event_at, '-' || reminder_minutes || ' minutes')<=datetime(?) ORDER BY event_at LIMIT 50",(now_riyadh,)).fetchall()
            for mid,muid,mtype,mtitle,mat,mrem in mini_due:
                try:
                    await app.bot.send_message(chat_id=muid,text=f"🔔 تذكير بموعدك الدراسي\n📌 {mtitle}\nالنوع: {mtype}\n📅 {mat.replace('T',' ')[:16]}\nالتنبيه قبل الموعد: {mrem} دقيقة")
                    db.execute("UPDATE miniapp_events SET notified=1 WHERE id=?",(mid,)); db.commit()
                except Exception as exc:
                    log.warning("Mini App reminder delivery failed (%s)",type(exc).__name__)
        except Exception as exc:
            log.warning("Reminder worker error: %s", type(exc).__name__)
        await asyncio.sleep(30)


async def assistant_usage_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    row = db.execute("SELECT ai_requests,notes_count,first_seen,last_seen FROM assistant_users WHERE user_id=?", (user.id,)).fetchone()
    await update.effective_message.reply_text(
        "📊 معلومات استخدامك\n"
        f"طلبات المساعد المحلي المسجلة سابقًا: {row[0]}\nالملاحظات المحفوظة: {row[1]}\n"
        f"أول استخدام: {row[2]}\nآخر استخدام: {row[3]}\n\n"
        "🔒 تحفظ بيانات الاستخدام الأساسية لتحسين الخدمة وإحصاء النشاط. محادثاتك ليست معروضة في لوحة المالك تلقائيًا. استخدم /clear_ai لحذف سجل محادثة المساعد الخاص بك."
    )


async def assistant_stats_cmd(update, context):
    user = update.effective_user
    if not user or not is_owner(user.id):
        await update.effective_message.reply_text("⛔ هذه الإحصائيات للمالك العام فقط.")
        return
    users = db.execute("SELECT COUNT(*) FROM assistant_users").fetchone()[0]
    active7 = db.execute("SELECT COUNT(*) FROM assistant_users WHERE last_seen >= ?", ((datetime.now().astimezone().replace(microsecond=0) - __import__("datetime").timedelta(days=7)).isoformat(),)).fetchone()[0]
    requests = db.execute("SELECT COALESCE(SUM(ai_requests),0) FROM assistant_users").fetchone()[0]
    notes = db.execute("SELECT COUNT(*) FROM assistant_notes").fetchone()[0]
    reminders = db.execute("SELECT COUNT(*) FROM assistant_reminders WHERE sent=0").fetchone()[0]
    groups = db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0]
    tasks = db.execute("SELECT COUNT(*) FROM assistant_tasks").fetchone()[0]
    expenses = db.execute("SELECT COUNT(*) FROM assistant_expenses").fetchone()[0]
    await update.effective_message.reply_text(
        "📊 إحصائيات المساعد والبوت (للمالك العام)\n\n"
        f"👤 مستخدمو المساعد المسجلون: {users}\n"
        f"🟢 المستخدمون خلال آخر 7 أيام: {active7}\n"
        f"🧠 طلبات الذكاء الاصطناعي الناجحة: {requests}\n"
        f"📝 الملاحظات المحفوظة: {notes}\n"
        f"⏰ التذكيرات القادمة: {reminders}\n"
        f"📋 المهام المسجلة: {tasks}\n"
        f"💰 سجلات المصروفات: {expenses}\n"
        f"🛡️ القروبات المسجلة للحماية: {groups}\n\n"
        "لا يعرض هذا التقرير محتوى محادثات المستخدمين."
    )


async def assistant_clear_ai_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    db.execute("DELETE FROM assistant_chat_history WHERE user_id=?", (user.id,))
    db.commit()
    await update.effective_message.reply_text("✅ حُذف سجل محادثة المساعد الذكي الخاص بك.")


async def assistant_task_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    remember_assistant_user(user)
    task_text = " ".join(context.args).strip()
    if not task_text:
        await update.effective_message.reply_text("📋 لإضافة مهمة: /task نص المهمة\nلعرض المهام: /tasks\nلإنجاز مهمة: /donetask رقم\nلحذف مهمة: /deltask رقم")
        return
    cur = db.execute("INSERT INTO assistant_tasks(user_id,task_text,created_at) VALUES(?,?,?)", (user.id, task_text[:1000], _assistant_now()))
    db.commit()
    await update.effective_message.reply_text(f"✅ أُضيفت المهمة رقم #{cur.lastrowid}:\n{task_text[:1000]}")


async def assistant_tasks_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = db.execute("SELECT id,task_text,created_at FROM assistant_tasks WHERE user_id=? AND completed=0 ORDER BY id DESC LIMIT 30", (user.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("ما عندك مهام مفتوحة. أضف مهمة باستخدام /task نص المهمة")
        return
    await update.effective_message.reply_text("📋 مهامك المفتوحة:\n\n" + "\n".join(f"#{rid} — {task} ({created[:10]})" for rid,task,created in rows) + "\n\nلإنجاز مهمة: /donetask رقم | لحذفها: /deltask رقم")


async def assistant_done_task_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استخدم: /donetask رقم_المهمة")
        return
    cur = db.execute("UPDATE assistant_tasks SET completed=1 WHERE id=? AND user_id=? AND completed=0", (int(context.args[0]),user.id))
    db.commit()
    await update.effective_message.reply_text("✅ تم تعليم المهمة كمكتملة." if cur.rowcount else "لم أجد مهمة مفتوحة بهذا الرقم في حسابك.")


async def assistant_delete_task_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استخدم: /deltask رقم_المهمة")
        return
    cur = db.execute("DELETE FROM assistant_tasks WHERE id=? AND user_id=?", (int(context.args[0]),user.id))
    db.commit()
    await update.effective_message.reply_text("✅ تم حذف المهمة." if cur.rowcount else "لم أجد مهمة بهذا الرقم في حسابك.")


async def assistant_expense_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.effective_message.reply_text("💰 سجّل مصروفًا هكذا:\n/expense المبلغ | وصف المصروف\nمثال: /expense 35.5 | قهوة\nلعرض آخر المصاريف: /expenses")
        return
    amount_raw, description = [x.strip() for x in raw.split("|",1)]
    try:
        amount = float(amount_raw.replace(",", "."))
        if amount <= 0 or amount > 100000000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("المبلغ غير صحيح. اكتب مبلغًا موجبًا مثل: /expense 35.5 | قهوة")
        return
    if not description:
        await update.effective_message.reply_text("اكتب وصف المصروف بعد علامة |.")
        return
    cur = db.execute("INSERT INTO assistant_expenses(user_id,amount,description,created_at) VALUES(?,?,?,?)", (user.id,amount,description[:300],_assistant_now()))
    db.commit()
    await update.effective_message.reply_text(f"✅ تم تسجيل المصروف #{cur.lastrowid}\n💵 {amount:.2f} ريال\n🧾 {description[:300]}")


async def assistant_expenses_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = db.execute("SELECT id,amount,description,created_at FROM assistant_expenses WHERE user_id=? ORDER BY id DESC LIMIT 20", (user.id,)).fetchall()
    total = db.execute("SELECT COALESCE(SUM(amount),0) FROM assistant_expenses WHERE user_id=? AND created_at>=?", (user.id,datetime.now().astimezone().replace(day=1,hour=0,minute=0,second=0,microsecond=0).isoformat(timespec="seconds"))).fetchone()[0]
    if not rows:
        await update.effective_message.reply_text("لا توجد مصاريف مسجلة. أضف أول مصروف باستخدام /expense المبلغ | الوصف")
        return
    lines = [f"💰 آخر مصاريفك (حتى 20 سجلًا)\nإجمالي المسجل منذ بداية الشهر: {total:.2f} ريال\n"]
    lines.extend(f"#{rid} — {amount:.2f} ريال — {desc} ({created[:10]})" for rid,amount,desc,created in rows)
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def assistant_summarize_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    source = " ".join(context.args).strip()
    if not source:
        await update.effective_message.reply_text("📄 اكتب /summarize ثم النص الذي تريد تلخيصه. ويمكنك إرسال ملف PDF أو DOCX أو TXT إلى الخاص مع البوت. يجري التلخيص محليًا باختيار جمل من النص دون ذكاء اصطناعي أو إرسال خارجي.")
        return
    await update.effective_message.reply_text(_offline_summary(source[:12000]))


async def assistant_private_document(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or update.effective_chat.type != ChatType.PRIVATE or not msg.document:
        return
    # Do not intercept media used by existing owner workflows such as broadcast drafts.
    if context.user_data.get("owner_autoreply_state") or context.user_data.get("awaiting_welcome_chat") or context.user_data.get("awaiting_broadcast") or context.user_data.get("awaiting_broadcast_confirmation"):
        return
    doc = msg.document
    filename = (doc.file_name or "").lower()
    if doc.file_size and doc.file_size > 8 * 1024 * 1024:
        await msg.reply_text("حجم الملف أكبر من الحد المسموح (8 ميغابايت). أرسل نسخة أصغر.")
        return
    if not filename.endswith((".pdf", ".docx", ".txt")):
        await msg.reply_text("أستطيع محاولة قراءة ملفات PDF وDOCX وTXT فقط حاليًا. الصور وملفات Word القديمة DOC غير مدعومة.")
        return
    await msg.reply_text("📄 استلمت الملف. سأحاول استخراج النص ثم تلخيصه؛ الملفات التي لا تحتوي نصًا قابلًا للاستخراج قد لا تنجح.")
    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
        extracted = ""
        if filename.endswith(".txt"):
            extracted = raw.decode("utf-8", errors="replace")
        elif filename.endswith(".pdf"):
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            extracted = "\n".join((page.extract_text() or "") for page in reader.pages[:40])
        else:
            from docx import Document
            import io
            document = Document(io.BytesIO(raw))
            extracted = "\n".join(paragraph.text for paragraph in document.paragraphs)
        extracted = extracted.strip()
        if not extracted:
            await msg.reply_text("لم أتمكن من استخراج نص من الملف. قد يكون PDF مصوّرًا يحتاج OCR، وهذه النسخة لا تتضمن OCR.")
            return
        remember_assistant_user(user)
        await msg.reply_text("📄 جارٍ استخراج ملخص محلي من النص دون إرساله إلى أي خدمة خارجية.")
        answer = _offline_summary(extracted[:12000])
        await msg.reply_text(("📄 ملخص الملف: " + (doc.file_name or "بدون اسم") + "\n\n" + answer)[:4000])
    except ImportError:
        await msg.reply_text("ميزة قراءة هذا النوع من الملفات تحتاج مكتبات إضافية. تأكد من تحديث requirements.txt وإعادة النشر.")
    except Exception as exc:
        log.warning("Document summary failed (%s)", type(exc).__name__)
        await msg.reply_text("تعذر قراءة الملف أو تلخيصه. تأكد أنه غير تالف وأنه PDF نصي أو DOCX أو TXT، ثم جرّب مجددًا.")


async def assistant_quiz_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    topic = " ".join(context.args).strip()
    if not topic:
        await update.effective_message.reply_text("🎓 اكتب /quiz ثم موضوعًا دراسيًا. سأعطيك قالب مراجعة وأسئلة عامة دون ادعاء أنها من منهج رسمي.")
        return
    await update.effective_message.reply_text(
        f"🎓 قالب مراجعة ذاتية لموضوع: {topic[:300]}\n\n"
        "1) ما تعريف المفهوم الأساسي في هذا الموضوع؟\n"
        "2) اذكر ثلاث نقاط رئيسية تتعلق به.\n"
        "3) ما الفرق بين أهم مصطلحين فيه؟\n"
        "4) اذكر مثالًا تطبيقيًا.\n"
        "5) ما الخطأ الشائع الذي ينبغي تجنبه؟\n\n"
        "ℹ️ هذا قالب تدريبي محلي وليس اختبارًا مولّدًا آليًا أو أسئلة رسمية. أرسل نص الدرس مع /summarize لاستخراج ملخص منه.")


async def student_studyadd_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    raw = " ".join(context.args).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        await update.effective_message.reply_text("الصيغة: /studyadd اليوم | الوقت | المادة\nمثال: /studyadd الأحد | 08:00 | مبادئ الإدارة")
        return
    db.execute("INSERT INTO student_schedule(user_id,weekday,study_time,subject,created_at) VALUES(?,?,?,?,?)", (user.id,parts[0][:30],parts[1][:30],parts[2][:200],_assistant_now()))
    db.commit()
    await update.effective_message.reply_text("✅ تمت إضافة المادة إلى جدولك الدراسي.")


async def student_schedule_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = db.execute("SELECT id,weekday,study_time,subject FROM student_schedule WHERE user_id=? ORDER BY CASE weekday WHEN 'الأحد' THEN 1 WHEN 'الاثنين' THEN 2 WHEN 'الثلاثاء' THEN 3 WHEN 'الأربعاء' THEN 4 WHEN 'الخميس' THEN 5 WHEN 'الجمعة' THEN 6 WHEN 'السبت' THEN 7 ELSE 8 END, study_time", (user.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("لا يوجد جدول دراسي بعد. استخدم /studyadd اليوم | الوقت | المادة")
        return
    await update.effective_message.reply_text("🎓 جدولك الدراسي:\n\n" + "\n".join(f"#{i} — {day}، {tm} — {sub}" for i,day,tm,sub in rows)[:4000])


async def student_studydelete_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE or not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("الصيغة: /delstudy رقم المادة")
        return
    cur = db.execute("DELETE FROM student_schedule WHERE id=? AND user_id=?", (int(context.args[0]),user.id))
    db.commit()
    await update.effective_message.reply_text("✅ تم حذف المادة." if cur.rowcount else "لم أجد مادة بهذا الرقم في جدولك.")


async def student_exam_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    raw = " ".join(context.args).strip()
    parts = [p.strip() for p in raw.split("|",1)]
    if len(parts) != 2 or not all(parts):
        await update.effective_message.reply_text("الصيغة: /exam YYYY-MM-DD HH:MM | اسم الاختبار\nمثال: /exam 2026-11-01 09:00 | اختبار المحاسبة")
        return
    try:
        dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").astimezone().isoformat(timespec="seconds")
    except ValueError:
        await update.effective_message.reply_text("صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD HH:MM مثل 2026-11-01 09:00")
        return
    if datetime.fromisoformat(dt) <= datetime.now().astimezone():
        await update.effective_message.reply_text("موعد الاختبار يجب أن يكون في المستقبل.")
        return
    db.execute("INSERT INTO student_events(user_id,event_type,title,event_at,created_at) VALUES(?,?,?,?,?)", (user.id,"exam",parts[1][:250],dt,_assistant_now()))
    db.commit()
    await update.effective_message.reply_text("✅ تم حفظ موعد الاختبار. سأرسل لك تذكيرًا عندما يحين الموعد إذا كان البوت يعمل.")


async def student_events_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = db.execute("SELECT id,event_type,title,event_at FROM student_events WHERE user_id=? AND notified=0 ORDER BY event_at LIMIT 30", (user.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد اختبارات أو مواعيد قادمة مسجلة.")
        return
    await update.effective_message.reply_text("🗓️ مواعيدك القادمة:\n\n" + "\n".join(f"#{i} — {title} — {when.replace('T',' ')[:16]}" for i,typ,title,when in rows)[:4000])


async def student_event_delete_cmd(update, context):
    user = update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE or not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("الصيغة: /delexam رقم الموعد")
        return
    cur = db.execute("DELETE FROM student_events WHERE id=? AND user_id=?", (int(context.args[0]),user.id))
    db.commit()
    await update.effective_message.reply_text("✅ تم حذف الموعد." if cur.rowcount else "لم أجد موعدًا بهذا الرقم.")


async def assistant_private_text(update, context):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or chat.type != ChatType.PRIVATE or not msg or not msg.text:
        return
    # Do not answer workflow drafts as if they were ordinary assistant prompts.
    if context.user_data.pop("_skip_assistant_private_message_id", None) == msg.message_id:
        return
    if context.user_data.get("owner_autoreply_state") or context.user_data.get("awaiting_welcome_chat") or context.user_data.get("awaiting_broadcast") or context.user_data.get("awaiting_broadcast_confirmation") or context.user_data.get("awaiting_channel_broadcast"):
        return
    remember_assistant_user(user)
    if OPENAI_API_KEY and (is_owner(user.id) or can_use_capability(user.id, "ai") or can_use_capability(user.id, "executive")):
        answer = await openai_assistant_response(msg.text, "الخاص")
        if answer:
            await msg.reply_text(answer)
            return
    answer = await offline_assistant_response(user.id, msg.text)
    await msg.reply_text(answer)


async def start(update, context):
    remember_delegated_user(update.effective_user)
    if update.effective_chat.type == ChatType.PRIVATE:
        remember_assistant_user(update.effective_user)
        if context.args and context.args[0].startswith("support_"):
            try:
                support_chat_id = int(context.args[0].split("_", 1)[1])
            except (ValueError, IndexError):
                await update.effective_message.reply_text("رابط الدعم غير صحيح."); return
            if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (support_chat_id,)).fetchone() or not await is_telegram_group_admin(context.bot, update.effective_user.id, support_chat_id):
                await update.effective_message.reply_text("لا يمكن فتح طلب دعم لهذا القروب إلا لمالكه أو أحد مشرفيه الفعليين في تيليجرام."); return
            context.user_data["external_support_state"] = {"chat_id": support_chat_id}
            group_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (support_chat_id,)).fetchone()
            await update.effective_message.reply_text(
                f"🆘 الدعم الفني — {group_row[0] if group_row else support_chat_id}\nاكتب وصف المشكلة التي تواجهها مع البوت. أرسل /cancel للإلغاء."
            )
            return
        # Show authorized staff a concise panel entry; other users see only permitted personal services.
        if can_use_panel(update.effective_user.id):
            await update.effective_message.reply_text(
                "🛡️ <b>بوت الحماية والمساعدة</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "منظومة لإدارة القروبات وحمايتها، وتنظيم الصلاحيات، ومتابعة العمليات من لوحة موحدة.\n\n"
                "✨ اختر ما تريد فتحه. تظهر لك فقط الأدوات المسموح بها وفق دورك وصلاحياتك.\n\n"
                "📌 لتفعيل البوت في قروبك: أضفه مشرفًا، ثم أرسل داخل القروب:\n"
                "<code>تفعيل @khaled211_bot</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧭 فتح لوحة التحكم", callback_data="home")],
                ]),
            )
            return
        await update.effective_message.reply_text(
            "🤖 <b>بوت الحماية والمساعدة</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "خدمات لإدارة القروبات وحمايتها، والمساعدة الدراسية والشخصية.\n\n"
            "✨ تظهر لك الخدمات المتاحة حسب صلاحياتك.\n"
            "📌 لتفعيل البوت في قروبك: أضفه مشرفًا ثم أرسل داخله: <code>تفعيل @khaled211_bot</code>",
            parse_mode="HTML",
            reply_markup=assistant_menu_markup(),
        )
    else:
        register_group(update.effective_chat, update.effective_user)
        chat = update.effective_chat
        # Professional group-facing introduction; never reveal the bot owner's identity or private controls.
        await update.effective_message.reply_text(
            "🛡️ <b>بوت الحماية والمساعدة</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "أهلًا بكم! أساعد في حماية القروب وتنظيم الإشراف وتقليل الرسائل المزعجة، "
            "مع إتاحة أدوات الإدارة للمستخدمين المخوّلين فقط.\n\n"
            "📌 للتفعيل: ارفع البوت إلى مشرف، ثم أرسل: <code>تفعيل @khaled211_bot</code>\n\n"
            "✨ اختر الخدمة المناسبة من الأزرار أدناه.\n"
            "🔐 تظهر لوحة القروب للمشرفين المصرح لهم، ولا تكشف إعدادات مالك البوت.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛡️ لوحة إدارة القروب", callback_data=f"group_panel_home:{chat.id}")],
                [InlineKeyboardButton("📌 طريقة التفعيل", callback_data="activation_help")],
                [InlineKeyboardButton("🕹️ مركز الألعاب", callback_data="public:back")],
            ]),
        )


async def calendar_cmd(update, context):
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("افتح البوت في الخاص لاستخدام التقويم التفاعلي."); return
    dt = datetime.now(ZoneInfo("Asia/Riyadh"))
    text_value = await calendar_month_text(dt.year, dt.month)
    if not text_value:
        await update.effective_message.reply_text("تعذر جلب التقويم الآن. حاول لاحقًا."); return
    await update.effective_message.reply_text(text_value[:3900], reply_markup=calendar_markup(dt.year, dt.month))


async def adhkar_cmd(update, context):
    await update.effective_message.reply_text("🤲 الأذكار والأدعية\nاختر القسم لعرض الأذكار. ينشر البوت أذكار المساء تلقائيًا فقط في القروبات التي فعّلها المالك من لوحة التحكم.", reply_markup=adhkar_markup())


async def staffdone_cmd(update, context):
    if not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استخدم /staffdone رقم_المهمة")
        return
    task_id=int(context.args[0])
    row=db.execute("SELECT assigned_to FROM staff_tasks WHERE id=?",(task_id,)).fetchone()
    if not row:
        await update.effective_message.reply_text("المهمة غير موجودة."); return
    if row[0] != update.effective_user.id and not (is_owner(update.effective_user.id) or is_delegated_owner(update.effective_user.id)):
        await update.effective_message.reply_text("هذه المهمة ليست مسندة إليك."); return
    db.execute("UPDATE staff_tasks SET status='done',updated_at=? WHERE id=?",(now(),task_id)); db.commit()
    await update.effective_message.reply_text(f"✅ تم تسجيل إكمال المهمة #{task_id}.")

async def games_cmd(update, context):
    """فتح مركز الألعاب التفاعلية داخل تيليجرام مع قصر الإدارة على القروبات المتاحة للمستخدم."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        register_group(chat, user)
        await msg.reply_text("🎮 مركز الألعاب التفاعلية", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 فتح الألعاب لهذا القروب", callback_data=f"member_games:{chat.id}")]
        ]))
        return
    groups = await accessible_groups(context.bot, user.id)
    if not groups:
        await msg.reply_text("لا توجد قروبات متاحة للألعاب. أضف البوت إلى قروبك ثم افتح /games داخل القروب.")
        return
    rows = [[InlineKeyboardButton(f"🎮 {title or cid}", callback_data=f"games_group:{cid}")] for cid,title in groups[:40]]
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    await msg.reply_text("🎮 مركز الألعاب التفاعلية\nاختر القروب لعرض الألعاب والإعدادات المتاحة.", reply_markup=InlineKeyboardMarkup(rows))

async def help_cmd(update, context):
    uid = update.effective_user.id if update.effective_user else 0
    text = (
        "📚 دليل البوت العربي\n\n"
        "🏠 /panel — لوحة التحكم من الخاص (للمصرح لهم)\n"
        "🛡️ من الخاص: اختر القروب ثم غيّر إعدادات الحماية.\n"
        "🔨 من الخاص: اختر القروب ثم نفّذ الحظر والكتم والطرد والحذف والتثبيت برقم المستخدم أو الرسالة.\n"
        "🗑️ مسح جماعي: اكتب داخل القروب «مسح رسائل 500» أو افتح أيقونة المسح في لوحة التحكم وحدد القروبات والعدد.\n"
        "🎮 /games — فتح الألعاب والمراجعات والتحديات داخل القروب.\n"
        "🆔 /id — عرض رقم حسابك\n"
        "🆔 /idgroup — عرض رقم المجموعة\n"
        "🚩 /report — بلاغ سري (بالرد على الرسالة)\n"
        "📋 السجلات من لوحة التحكم\n\n"
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
            "/archive_off — إيقاف أرشفة الرسائل\\n/archive — عرض سجل الرسائل الخاص\\n"
            "/motivation — إدارة المحتوى التحفيزي اليومي وتحديد قروبات النشر (للمالك الأساسي)\\n"
            "/monitor — فتح مراقبة القروبات المسجلة (عدد الأعضاء وحالة البوت والإحصائيات)\n"
            "/grantpublish USER_ID CHAT_ID — منح صلاحية النشر في قروب\n"
            "/grantcap USER_ID tag CHAT_ID — منح التاق للمشرف/المدير في قروب\n"
            "/grantcap USER_ID tag all — منح التاق في جميع القروبات المسجلة\n"
            "/revokepublish USER_ID CHAT_ID — سحب صلاحية النشر من قروب\n"
            "استخدم CHAT_ID=all لمنح/سحب صلاحية النشر في كل القروبات المسجلة."
        )
    await update.effective_message.reply_text(text)

def group_owner_panel_markup(chat_id, uid=None):
    """لوحة إشراف حديثة ومحدودة بالقروب الحالي، وتُظهر التاق فقط للمصرح لهم."""
    s = get_settings(chat_id)
    link_label = "🔓 السماح بالروابط" if s.get("links") else "🔒 منع الروابط"
    link_callback = f"group_panel_toggle_links:{chat_id}"
    can_open_links = uid is None or is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "protection", chat_id))
    if s.get("links") and not can_open_links:
        link_label = "🔒 الروابط مقفلة — الفتح للمالك"
        link_callback = "panel_links_locked"
    rows = [
        [InlineKeyboardButton("🛡️ مركز الحماية والإعدادات", callback_data=f"group_panel_settings:{chat_id}")],
        [InlineKeyboardButton(link_label, callback_data=link_callback)],
        [InlineKeyboardButton("👥 إدارة الأعضاء", callback_data=f"group_panel_admin:{chat_id}"),
         InlineKeyboardButton("🏷️ الرتب والصلاحيات", callback_data=f"group_panel_roles:{chat_id}")],
        [InlineKeyboardButton("📊 إحصائيات القروب", callback_data=f"group_panel_stats:{chat_id}"),
         InlineKeyboardButton("📋 سجل العمليات", callback_data=f"group_panel_logs:{chat_id}")],
    ]
    external_tag_default = bool((external_group_info(chat_id) or (0,))[0]) and external_group_cap_enabled(chat_id, "tag")
    if uid is not None and (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "tag", chat_id) or external_tag_default):
        rows.insert(2, [InlineKeyboardButton("📣 استدعاء التاق للأعضاء", callback_data=f"group_panel_tag:{chat_id}")])
    else:
        rows.insert(2, [InlineKeyboardButton("🔐 التاق (يتطلب موافقة المالك)", callback_data="panel_tag_locked")])
    rows.append([InlineKeyboardButton("🆘 الدعم الفني", callback_data=f"group_panel_support:{chat_id}")])
    rows.append([InlineKeyboardButton("⚠️ قائمة المخالفين", callback_data=f"group_panel_violators:{chat_id}")])
    rows.append([InlineKeyboardButton("🔄 تحديث اللوحة", callback_data=f"group_panel_home:{chat_id}")])
    return InlineKeyboardMarkup(rows)

async def panel(update, context):
    uid = update.effective_user.id
    chat = update.effective_chat
    # داخل القروب: مشرف Telegram يحصل على لوحة لهذا القروب فقط، دون لوحة مالك البوت.
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and not is_owner(uid) and not is_delegated_owner(uid) and not is_general_manager(uid) and not is_protection_manager(uid):
        if not await is_telegram_group_admin(context.bot, uid, chat.id):
            await update.effective_message.reply_text("⛔ لوحة القروب متاحة لمالك القروب ومشرفيه في Telegram فقط.")
            return
        register_group(chat, update.effective_user)
        await update.effective_message.reply_text(
            f"🛡️ لوحة إدارة القروب: {html.escape(chat.title or str(chat.id))}\n"
            "هذه اللوحة تخص هذا القروب فقط. لا تمنحك صلاحيات مالك البوت أو الوصول إلى قروبات أخرى.",
            reply_markup=group_owner_panel_markup(chat.id, uid),
        )
        return
    if not can_use_panel(uid):
        # في الخاص: اعرض فقط القروبات التي ثبت أن المستخدم مشرف فيها.
        groups = await accessible_groups(context.bot, uid)
        if not groups:
            await update.effective_message.reply_text("⛔ لا توجد قروبات مسجلة أنت مالكها أو مشرف فيها. أضف البوت إلى قروبك، امنحه صلاحيات المشرف المطلوبة، ثم أرسل /start داخل القروب.")
            return
        rows = [[InlineKeyboardButton(f"🛡️ {title or chat_id}", callback_data=f"group_panel_home:{chat_id}")] for chat_id, title in groups[:30]]
        await update.effective_message.reply_text(
            "🛡️ لوحة قروباتك\nاختر قروبًا لإدارة صلاحياته المحددة فقط. لا تظهر هنا صلاحيات مالك البوت.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        register_group(chat)
    is_primary_owner = is_owner(uid)
    panel_intro = (
        "👑 لوحة التحكم الرئيسية\n\n"
        f"الصلاحية: {role_name(uid)}\n\n"
        "🛡️ الحماية والإشراف: إدارة القروبات والأعضاء وإعدادات الحماية.\n"
        "📋 المتابعة: السجلات والتنبيهات وحالة البوت.\n"
        + ("👑 إدارة النظام: الردود الآلية والمفوّضون والنسخ الاحتياطي والأرشيف والإعدادات العليا.\n\n" if is_primary_owner else "\n")
        + "اختر القسم المطلوب. تُعرض الخيارات بحسب صلاحيتك، وتُفحص الصلاحية أيضًا عند تنفيذ كل إجراء."
    )
    await update.effective_message.reply_text(panel_intro, reply_markup=panel_markup(uid))

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

async def grantcap(update, context):
    """Owner-only grant: /grantcap USER_ID publish|tag|protection CHAT_ID|all"""
    uid = update.effective_user.id if update.effective_user else 0
    if not is_owner(uid):
        await update.effective_message.reply_text("⛔ منح الصلاحيات المركزية للمالك الأساسي فقط.")
        return
    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "الاستخدام:\n/grantcap USER_ID publish all|CHAT_ID\n"
            "/grantcap USER_ID tag all|CHAT_ID\n/grantcap USER_ID protection all|CHAT_ID\n"
            "/grantcap USER_ID stats all|CHAT_ID\n/grantcap USER_ID ai all|CHAT_ID\n"
            "\n"
            "الصلاحيات: publish للنشر، tag للتاق، protection للحماية، ai للمساعد الذكي. استخدم all لتطبيق الصلاحية على القروبات المسجلة."
        )
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح."); return
    capability = context.args[1].lower()
    if capability not in {"publish", "tag", "protection", "ai"}:
        await update.effective_message.reply_text("الصلاحية المتاحة: publish أو tag أو protection أو ai."); return
    raw_scope = context.args[2].lower()
    if raw_scope == "all":
        scope = 0
        scope_label = "جميع القروبات المسجلة حاليًا ومستقبلًا"
    else:
        try: scope = int(raw_scope)
        except ValueError:
            await update.effective_message.reply_text("حدد all أو رقم القروب."); return
        group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (scope,)).fetchone()
        if not group:
            await update.effective_message.reply_text("القروب غير مسجل. أضف البوت إليه أولًا."); return
        scope_label = group[0] or str(scope)
    if scope == 0:
        db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target, capability))
    else:
        db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?", (target, capability, scope))
    db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)",
                (target, capability, scope, uid, now()))
    db.commit()
    log_action(scope, uid, "staff_capability_granted", f"target={target}; capability={capability}; scope={scope}")
    labels = {"publish":"النشر والإعلانات", "tag":"استدعاء التاق", "protection":"أوامر الحماية", "stats":"الإحصائيات", "ai":"تحليل الذكاء الاصطناعي", "join_approval":"إدارة الموافقة على الدخول"}
    await update.effective_message.reply_text(f"✅ مُنحت صلاحية {labels[capability]} للعضو {target}.\n📍 النطاق: {scope_label}\nيمكن سحبها بالأمر /revokecap.")


async def revokecap(update, context):
    """Owner-only revoke: /revokecap USER_ID capability CHAT_ID|all"""
    uid = update.effective_user.id if update.effective_user else 0
    if not is_owner(uid):
        await update.effective_message.reply_text("⛔ سحب الصلاحيات المركزية للمالك الأساسي فقط."); return
    if len(context.args) < 3:
        await update.effective_message.reply_text("الاستخدام: /revokecap USER_ID publish|tag|protection|stats|ai|join_approval CHAT_ID|all"); return
    try: target = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("رقم المستخدم غير صحيح."); return
    capability = context.args[1].lower()
    if capability not in {"publish", "tag", "protection", "ai"}:
        await update.effective_message.reply_text("الصلاحية المتاحة: publish أو tag أو protection أو ai."); return
    raw_scope = context.args[2].lower()
    if raw_scope == "all":
        db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=?", (target, capability))
        db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target, capability))
        scope_label = "جميع القروبات"
    else:
        try: scope = int(raw_scope)
        except ValueError:
            await update.effective_message.reply_text("حدد all أو رقم القروب."); return
        group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (scope,)).fetchone()
        if not group:
            await update.effective_message.reply_text("القروب غير مسجل."); return
        global_grant = db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target, capability)).fetchone()
        if global_grant:
            db.execute("INSERT OR REPLACE INTO staff_capability_denials(user_id,capability,chat_id,revoked_by,revoked_at) VALUES(?,?,?,?,?)", (target, capability, scope, uid, now()))
        else:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?", (target, capability, scope))
        scope_label = group[0] or str(scope)
    db.commit()
    log_action(0, uid, "staff_capability_revoked", f"target={target}; capability={capability}; scope={raw_scope}")
    await update.effective_message.reply_text(f"✅ تم سحب صلاحية {capability} من العضو {target}.\n📍 النطاق: {scope_label}")


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
    actor_id = update.effective_user.id
    if context.args:
        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("استخدم: /archive رقم_المجموعة")
            return
    else:
        await update.effective_message.reply_text("استخدم /archive رقم_المجموعة لعرض أحدث الرسائل المسجلة. تحصل على رقم المجموعة عبر /idgroup داخلها.")
        return
    if not (is_owner(actor_id) or can_use_capability(actor_id, "archive", chat_id)):
        await update.effective_message.reply_text("⛔ لا تملك صلاحية سجل التحويلات والملفات لهذا القروب.")
        return
    rows = db.execute(
        "SELECT chat_title,display_name,username,user_id,message_text,created_at,content_type,forward_origin,message_link,media_file_id FROM message_archive WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (chat_id,)
    ).fetchall()
    if not rows:
        await update.effective_message.reply_text("لا توجد رسائل مسجلة لهذه المجموعة.")
        return
    parts = ["📨 أحدث الرسائل والتحويلات والملفات:"]
    for title, name, username, user_id, body, created, content_type, forward_origin, message_link, media_file_id in rows:
        media_note = "\n📎 يوجد مرفق محفوظ بالمعرّف" if media_file_id else ""
        parts.append(
            f"\n📍 {title or chat_id}\n👤 {name or 'عضو'} — ID: {user_id} {('@'+username) if username else ''}"
            f"\n🕒 {created}\n📎 النوع: {content_type or 'غير محدد'}{media_note}"
            f"\n↪️ مصدر التحويل: {forward_origin or 'غير متاح/ليست رسالة محوّلة'}"
            f"\n💬 {(body or '[رسالة غير نصية]')[:250]}"
            f"\n🔗 الرسالة: {message_link or 'لا يتوفر رابط مباشر'}"
        )
    output = "\n".join(parts)
    await update.effective_message.reply_text(output[:4000])

async def group_role_command(update, context):
    """ترقية/تنزيل رتبة داخل البوت في القروب الحالي، دون تغيير رتبة تيليجرام الأصلية."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not external_group_active(chat.id) and not is_owner(user.id if user else 0):
        await msg.reply_text("⛔ خدمات البوت متوقفة عن هذا القروب من المالك الأساسي."); return
    # مالك البوت/المفوّض يدير رتب البوت، كما يمكن لمالك القروب ومشرفيه
    # إدارة الرتب المحلية داخل قروبهم فقط بعد التحقق من صلاحيات Telegram.
    ext_info = external_group_info(chat.id)
    if ext_info and ext_info[0] and not is_owner(user.id) and not external_group_cap_enabled(chat.id, "members"):
        await msg.reply_text("⛔ إدارة الأعضاء والرتب غير مفعّلة لهذا القروب من المالك الأساسي.")
        return
    if not (is_owner(user.id) or is_delegated_owner(user.id)):
        if not await is_telegram_group_admin(context.bot, user.id, chat.id):
            await msg.reply_text("⛔ إدارة الرتب متاحة لمالك القروب ومشرفيه فقط.")
            return
    elif is_delegated_owner(user.id) and not is_owner(user.id):
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
    labels = {"delete": "حذف الرسائل", "mute": "الكتم", "unmute": "فك الكتم ومسح قائمة المكتومين", "kick": "الطرد", "ban": "الحظر", "unban": "فك الحظر", "pin": "تثبيت الرسائل", "protection": "قفل وفتح الروابط وبقية إعدادات الحماية", "manage_roles": "إدارة رتب القروب", "logs": "عرض السجلات", "welcome": "إدارة الترحيب", "stats": "عرض الإحصائيات"}
    permissions = "، ".join(labels.get(item, item) for item in sorted(GROUP_ROLE_PERMISSIONS[role]))
    await msg.reply_text(
        f"✅ تم رفع {target.full_name} إلى رتبة {role} داخل البوت.\n"
        f"🛡️ الصلاحيات المفعّلة تلقائيًا: {permissions}.\n"
        "ℹ️ هذه رتبة داخل البوت فقط، ولا تغيّر رتبة العضو أو صلاحياته الأصلية في تيليجرام."
    )


async def id_cmd(update, context):
    msg = update.effective_message
    replied = msg.reply_to_message if msg else None
    if replied and replied.from_user:
        target = replied.from_user
        username_line = f"\n🔗 اسم المستخدم: @{target.username}" if getattr(target, "username", None) else "\n🔗 اسم المستخدم: غير متوفر"
        await msg.reply_text(
            f"🆔 رقم العضو: {target.id}\n👤 الاسم: {target.full_name}{username_line}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 نسخ ID", callback_data=f"copy_user_id:{target.id}")]])
        )
        return
    if context.args and context.args[0].lstrip("-").isdigit():
        await msg.reply_text(f"🆔 الرقم المدخل: {int(context.args[0])}")
        return
    await msg.reply_text(f"🆔 رقمك: {update.effective_user.id}\n💡 لمعرفة رقم عضو، استخدم /id بالرد على رسالته.")

async def badges_cmd(update, context):
    user = update.effective_user
    msg = update.effective_message
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() and (is_owner(user.id) or is_delegated_owner(user.id)) else user.id
    rows = db.execute("SELECT id,badge,reason,created_at FROM staff_badges WHERE user_id=? ORDER BY id DESC LIMIT 30", (target_id,)).fetchall()
    if not rows:
        await msg.reply_text(f"🏅 لا توجد أوسمة مسجلة للعضو {target_id}.")
        return
    lines = [f"🏅 أوسمة العضو {target_id}"]
    for badge_id, badge, reason, created_at in rows:
        lines.append(f"\n#{badge_id} · {badge}\n📝 {reason or 'دون سبب مسجل'}\n🗓️ {created_at}")
    lines.append("\nلحذف وسام (للمالك/المفوّض): /badge_remove رقم_الوسام")
    await msg.reply_text("\n".join(lines))

async def badge_remove_cmd(update, context):
    uid = update.effective_user.id
    if not (is_owner(uid) or is_delegated_owner(uid)):
        await update.effective_message.reply_text("⛔ حذف الأوسمة للمالك والمفوّض فقط."); return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("الصيغة: /badge_remove رقم_الوسام\nاعرض الأوسمة أولًا عبر /badges رقم_العضو."); return
    badge_id = int(context.args[0])
    row = db.execute("SELECT user_id,badge FROM staff_badges WHERE id=?", (badge_id,)).fetchone()
    if not row:
        await update.effective_message.reply_text("لم يتم العثور على هذا الوسام."); return
    db.execute("DELETE FROM staff_badges WHERE id=?", (badge_id,)); db.commit()
    log_action(0, uid, "staff_badge_removed", f"badge_id={badge_id}; target={row[0]}; badge={row[1]}")
    await update.effective_message.reply_text(f"✅ تم حذف الوسام #{badge_id} ({row[1]}) من سجل العضو {row[0]}.")

async def monitor_cmd(update, context):
    """Open the owner-only group monitoring list in private chat."""
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.effective_message.reply_text("⛔ أمر مراقبة جميع القروبات للمالك الأساسي فقط.")
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("افتح البوت في الخاص ثم استخدم /monitor لمتابعة القروبات المسجلة.")
        return
    groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
    if not groups:
        await update.effective_message.reply_text("لا توجد قروبات مسجلة بعد.")
        return
    rows = [[InlineKeyboardButton(f"👁️ {(title or str(chat_id))[:45]}", callback_data=f"monitor_group:{chat_id}")] for chat_id,title in groups[:20]]
    if len(groups) > 20:
        rows.append([InlineKeyboardButton("عرض بقية القروبات ➡️", callback_data="monitor_groups_page:1")])
    rows.append([InlineKeyboardButton("🔄 تحديث القائمة", callback_data="monitor_groups")])
    await update.effective_message.reply_text(f"👁️ مراقبة القروبات\nعدد القروبات المسجلة: {len(groups)}\nاختر قروبًا لعرض معلوماته الحية.", reply_markup=InlineKeyboardMarkup(rows))


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
    if not await require_group_permission(update, "ban"): return
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
    if not await require_group_permission(update, "unban"): return
    target = target_from_update(update, context)
    if not target:
        await update.message.reply_text("استخدم /unban رقم_المستخدم أو بالرد.")
        return
    uid = target.id if hasattr(target, "id") else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        log_action(update.effective_chat.id, update.effective_user.id, "unban", str(uid))
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
        log_action(update.effective_chat.id, update.effective_user.id, "kick", str(uid))
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
        db.execute("INSERT OR REPLACE INTO group_mutes(chat_id,user_id,muted_by,muted_at) VALUES(?,?,?,?)", (update.effective_chat.id, uid, update.effective_user.id, now()))
        db.commit()
        log_action(update.effective_chat.id, update.effective_user.id, "mute", str(uid))
        await update.message.reply_text("✅ تم كتم العضو وتسجيله في قائمة المكتومين.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر الكتم: {e}")

async def unmute(update, context):
    if not await require_group_permission(update, "unmute"): return
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
        db.execute("DELETE FROM group_mutes WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid)); db.commit()
        log_action(update.effective_chat.id, update.effective_user.id, "unmute", str(uid))
        await update.message.reply_text("✅ تم فك الكتم وإزالة العضو من قائمة المكتومين.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر فك الكتم: {e}")

async def muted_cmd(update, context):
    if not await require_group_permission(update, "unmute"): return
    rows = db.execute("SELECT user_id,muted_at FROM group_mutes WHERE chat_id=? ORDER BY muted_at DESC LIMIT 50", (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.message.reply_text("🔇 لا توجد أسماء في قائمة المكتومين التي يتابعها البوت."); return
    body = "\n".join(f"• {uid} — {when}" for uid, when in rows)
    await update.message.reply_text("🔇 قائمة المكتومين المسجلين لدى البوت:\n" + body + "\n\nلفك كتم عضو: /unmute بالرد على رسالته أو /unmute رقم_العضو")

async def unmuteall_cmd(update, context):
    if not await require_group_permission(update, "unmute"): return
    chat_id = update.effective_chat.id
    rows = db.execute("SELECT user_id FROM group_mutes WHERE chat_id=?", (chat_id,)).fetchall()
    if not rows:
        await update.message.reply_text("لا توجد قائمة مكتومين مسجلة لدى البوت."); return
    success, failed = 0, 0
    for (uid,) in rows:
        try:
            await context.bot.restrict_chat_member(chat_id, uid, permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True))
            db.execute("DELETE FROM group_mutes WHERE chat_id=? AND user_id=?", (chat_id, uid))
            log_action(chat_id, update.effective_user.id, "unmute", str(uid)); success += 1
        except Exception as exc:
            log.warning("تعذر فك كتم %s في %s: %s", uid, chat_id, exc); failed += 1
    db.commit()
    await update.message.reply_text(f"🧹 اكتملت محاولة مسح المكتومين المسجلين لدى البوت.\n✅ تم فك الكتم: {success}\n⚠️ تعذر فك الكتم: {failed}\nℹ️ لا يستطيع تيليجرام إتاحة قائمة شاملة بالمكتومين سابقًا؛ تشمل العملية فقط من سجّل البوت كتمهم.")

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
    uid = update.effective_user.id if update.effective_user else 0
    if field == "links" and int(value) == 0 and not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "protection", update.effective_chat.id))):
        await update.effective_message.reply_text("⛔ فتح الروابط للأعضاء يتطلب صلاحية المالك أو تفويض الحماية."); return
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

BULK_SECURITY_OPTIONS = [
    ("🔗 الروابط", "links"), ("🖼️ الصور", "photos"), ("🎥 الفيديو", "videos"),
    ("🔊 الصوت", "audio"), ("📁 الملفات", "files"), ("🎭 الملصقات", "stickers"),
    ("🎞️ الصور المتحركة", "gif"), ("🏷️ أسماء المستخدمين", "username"),
    ("📣 الإشارات والتاق", "tag"), ("🤖 البوتات", "bots"), ("⌨️ الكيبورد", "keyboard"),
    ("🔁 تكرار الرسائل", "repeat"), ("🚪 الدخول", "join_lock"),
    ("👋 رسائل الدخول", "entry"), ("➕ إضافة الأعضاء", "add_lock"), ("✏️ تعديل الرسائل", "edit"),
    ("🧯 إغراق الرسائل", "flood"),
]

def bulk_security_groups_markup(groups, selected):
    rows = [[InlineKeyboardButton(("✅ " if cid in selected else "⬜ ") + (title or str(cid)), callback_data=f"bulksec_toggle:{cid}")] for cid,title in groups[:45]]
    rows.append([InlineKeyboardButton("✅ تحديد الكل", callback_data="bulksec_all"), InlineKeyboardButton("🧹 إلغاء التحديد", callback_data="bulksec_none")])
    rows.append([InlineKeyboardButton(f"➡️ اختيار الأمر ({len(selected)})", callback_data="bulksec_actions")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def bulk_security_actions_markup():
    rows=[]
    for label, field in BULK_SECURITY_OPTIONS:
        rows.append([InlineKeyboardButton("🔒 قفل " + label, callback_data=f"bulksec_do:{field}:1"), InlineKeyboardButton("🔓 فتح " + label, callback_data=f"bulksec_do:{field}:0")])
    rows.append([InlineKeyboardButton("🧩 اختيار عدة أنواع ثم قفلها دفعة واحدة", callback_data="bulksec_multi:1")])
    rows.append([InlineKeyboardButton("🧩 اختيار عدة أنواع ثم فتحها دفعة واحدة", callback_data="bulksec_multi:0")])
    rows.append([InlineKeyboardButton("⚙️ إعداد مجموعة أوامر مختلفة (قفل/فتح لكل نوع)", callback_data="bulksec_combo")])
    rows.append([InlineKeyboardButton("⬅️ اختيار القروبات", callback_data="bulk_security")])
    return InlineKeyboardMarkup(rows)


def bulk_security_multi_markup(selected, value):
    rows = []
    for label, field in BULK_SECURITY_OPTIONS:
        rows.append([InlineKeyboardButton(("✅ " if field in selected else "⬜ ") + label,
                                          callback_data=f"bulksec_multi_toggle:{field}:{value}")])
    rows.append([InlineKeyboardButton("✅ تحديد الكل", callback_data=f"bulksec_multi_all:{value}"),
                 InlineKeyboardButton("🧹 إلغاء التحديد", callback_data=f"bulksec_multi_none:{value}")])
    rows.append([InlineKeyboardButton(f"🚀 تنفيذ {'القفل' if value else 'الفتح'} ({len(selected)})",
                                      callback_data=f"bulksec_multi_apply:{value}")])
    rows.append([InlineKeyboardButton("⬅️ أوامر الحماية", callback_data="bulksec_actions")])
    return InlineKeyboardMarkup(rows)



def bulk_security_combo_markup(selection):
    rows = []
    for label, field in BULK_SECURITY_OPTIONS:
        value = selection.get(field)
        prefix = "🔒 " if value == 1 else ("🔓 " if value == 0 else "⬜ ")
        rows.append([InlineKeyboardButton(prefix + label, callback_data=f"bulksec_combo_toggle:{field}")])
    rows.append([InlineKeyboardButton(f"🚀 تنفيذ الأوامر المحددة ({len(selection)})", callback_data="bulksec_combo_apply")])
    rows.append([InlineKeyboardButton("🧹 مسح التحديد", callback_data="bulksec_combo_clear")])
    rows.append([InlineKeyboardButton("⬅️ أوامر الحماية", callback_data="bulksec_actions")])
    return InlineKeyboardMarkup(rows)

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
    # واجهة مربعة/مختصرة قدر الإمكان ضمن قيود أزرار Telegram: 3 أيقونات رئيسية في صف واحد.
    # أزرار تيليجرام لا تسمح بتحديد لون أو أبعاد مربعة فعلية؛ الترتيب المتجاور يعطي شكل لوحة أيقونات.
    rows = [[
        InlineKeyboardButton("🛡️", callback_data="public:protection"),
    ]]
    if update.effective_user and has_group_permission(chat.id, update.effective_user.id, "delete"):
        rows[0].append(InlineKeyboardButton("🗑️ مسح رسائل", callback_data="bulk_delete_help"))
    secondary = []
    if settings.get("public_commands", 1):
        secondary.append(InlineKeyboardButton("📚 الدليل", callback_data="public:commands"))
    if settings.get("public_activity", 1):
        secondary.append(InlineKeyboardButton("🏆 النشاط", callback_data="public:activity"))
    secondary.append(InlineKeyboardButton("🎬 الوسائط", callback_data=f"member_media:{chat.id}"))
    rows.extend([secondary[i:i+2] for i in range(0, len(secondary), 2) if secondary[i:i+2]])
    text = "🤖 مركز الأعضاء\n\nاختر إحدى الخدمات من لوحة الأيقونات أدناه.\n🔒 أدوات الإدارة وإعدادات النظام مخصصة للمصرّح لهم فقط."
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

def is_bot_activation_phrase(text):
    """Recognize the documented group activation phrase without accepting lookalikes."""
    normalized = " ".join((text or "").strip().split()).casefold()
    return normalized == "تفعيل @khaled211_bot"


async def activate_group_bot(update, context):
    """Register a group only after a Telegram admin and the bot's admin status are verified."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not user or not msg:
        return
    if not await is_telegram_group_admin(context.bot, user.id, chat.id):
        await msg.reply_text("⛔ أمر التفعيل متاح لمالك القروب أو أحد مشرفيه في تيليجرام فقط.")
        return
    try:
        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await msg.reply_text("⚠️ لم يتم التفعيل بعد. ارفع @khaled211_bot إلى مشرف في هذا القروب، ثم أعد إرسال الأمر.")
            return
    except Exception as exc:
        log.info("تعذر التحقق من صلاحيات البوت عند التفعيل في %s: %s", chat.id, exc)
        await msg.reply_text("⚠️ تعذر التحقق من صلاحيات البوت حاليًا. تأكد من رفعه مشرفًا ثم أعد المحاولة.")
        return

    register_group(chat, user)
    ensure_settings(chat.id)
    # Baseline protection is enabled on activation; owners can tune each lock in the panel.
    for field in ("links", "photos", "videos"):
        set_setting(chat.id, field, 1)
    db.execute("UPDATE external_group_control SET enabled=1, title=?, updated_at=? WHERE chat_id=?",
               (chat.title or "", now(), chat.id))
    db.commit()
    log_action(chat.id, user.id, "group_bot_activated", "activation_phrase; links=1; photos=1; videos=1")
    await msg.reply_text(
        "✅ <b>تم تفعيل بوت الحماية بنجاح</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 القروب: {html.escape(chat.title or str(chat.id))}\n"
        "🛡️ الحماية الأساسية مفعّلة: الروابط والصور والفيديو.\n"
        "⚙️ يمكنك تعديل بقية الإعدادات من لوحة إدارة القروب.\n"
        "📌 لن تُنفّذ عمليات إدارية جماعية إلا وفق صلاحيات المستخدم.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡️ فتح لوحة إدارة القروب", callback_data=f"group_panel_home:{chat.id}")],
            [InlineKeyboardButton("📚 دليل البوت", callback_data="public:commands")],
        ]),
    )


async def ArabicTextCommand(update, context):
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = (update.effective_message.text or "").strip().lower()
    if is_bot_activation_phrase(text):
        await activate_group_bot(update, context)
        return
    if text in {"اعدادات", "إعدادات", "اعدادات البوت", "إعدادات البوت", "مساعدة", "اوامر البوت", "أوامر البوت"}:
        await public_settings(update, context)
        return
    if re.fullmatch(r"(?:رفع|ترقية)\s+(?:مشرف عام|مدير عام|مشرف|مدير)", text) or text in {"تنزيل رتبة", "إزالة الإشراف", "تنزيل مشرف"}:
        await group_role_command(update, context)
        return
    # Bulk delete command: "مسح رسائل 1..500" (also accepts "مسح رسايل 50" and legacy "مسح 50").
    match = re.fullmatch(r"(?:مسح(?:\s+(?:رسائل|رسايل))?)\s+(\d{1,3})", text)
    if match:
        if not has_group_permission(update.effective_chat.id, update.effective_user.id, "delete"):
            await update.effective_message.reply_text("⛔ ليس لديك صلاحية مسح الرسائل في هذا القروب.")
            return
        count = int(match.group(1))
        if not 1 <= count <= 500:
            await update.effective_message.reply_text("⚠️ استخدم عددًا من 1 إلى 500، مثل: مسح رسائل 500")
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
    if text in {"قفل رسايل", "قفل رسائل", "قفل الدردشة", "قفل المحادثة", "قفل الشات"}:
        await lock_all_messages_command(update, context)
        return
    if text in {"فتح رسايل", "فتح رسائل", "فتح الدردشة", "فتح المحادثة", "فتح الشات"}:
        await lock_all_messages_command(update, context)
        return
    if text in ARABIC_ALIASES:
        field, value = ARABIC_ALIASES[text]
        await set_lock(update, context, field, value)

recent_messages = defaultdict(lambda: deque(maxlen=6))
# Keep 500 target messages plus the current command message so the command path can exclude itself without reducing the limit to 499.
recent_message_ids = defaultdict(lambda: deque(maxlen=501))
# حماية اختيارية من الإغراق: لا تعمل إلا عند تفعيلها من لوحة الحماية.
flood_message_times = defaultdict(deque)
flood_notice_last = {}

def has_link(text):
    return bool(text and re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", text, re.I))

def message_has_link(msg):
    text_value = (getattr(msg, "text", None) or "") + "\n" + (getattr(msg, "caption", None) or "")
    if has_link(text_value):
        return True
    entities = list(getattr(msg, "entities", None) or []) + list(getattr(msg, "caption_entities", None) or [])
    return any(getattr(entity, "type", "") in ("url", "text_link") for entity in entities)


def has_tag(text):
    return bool(text and re.search(r"(?<!\w)@\w{3,}", text))

def _riyadh_now():
    return datetime.now(ZoneInfo("Asia/Riyadh"))

def _week_start(dt=None):
    dt = dt or _riyadh_now()
    monday = (dt - __import__("datetime").timedelta(days=dt.weekday())).date()
    return monday.isoformat()

def group_controls_list_markup():
    groups = db.execute("SELECT chat_id,COALESCE(NULLIF(title,''),CAST(chat_id AS TEXT)) FROM watched_groups ORDER BY title,chat_id LIMIT 80").fetchall()
    rows = [[InlineKeyboardButton(f"🔔 {str(title)[:48]}", callback_data=f"gctrl_group:{int(cid)}")] for cid, title in groups]
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد قروبات مسجلة", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def group_controls_markup(chat_id):
    row = db.execute("SELECT COALESCE(NULLIF(title,''),CAST(chat_id AS TEXT)) FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    title = row[0] if row else str(chat_id)
    rows = []
    for feature, label in (("join_notices_enabled", "📥 إشعارات دخول الأعضاء"), ("leave_notices_enabled", "📤 إشعارات خروج الأعضاء")):
        enabled = group_feature_enabled(chat_id, feature)
        status = "🟢 مفعّل" if enabled else "⚪ متوقف"
        rows.append([InlineKeyboardButton(f"{status} — {label}", callback_data=f"gctrl_toggle:{chat_id}:{feature}")])
    rows.append([InlineKeyboardButton("⬅️ اختيار قروب آخر", callback_data="group_controls"), InlineKeyboardButton("🏠 الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def restricted_groups_markup(uid):
    groups = db.execute("SELECT chat_id,COALESCE(NULLIF(title,''),CAST(chat_id AS TEXT)) FROM watched_groups ORDER BY title,chat_id LIMIT 80").fetchall()
    rows = []
    for cid,title in groups:
        if is_owner(uid) or can_use_capability(uid, "reentry", int(cid)) or can_use_capability(uid, "join_approval", int(cid)):
            pending = db.execute("SELECT COUNT(*) FROM reentry_restrictions WHERE chat_id=? AND status IN ('pending','blocked')", (cid,)).fetchone()[0]
            requests = db.execute("SELECT COUNT(*) FROM join_request_events WHERE chat_id=? AND status='pending'", (cid,)).fetchone()[0]
            rows.append([InlineKeyboardButton(f"🚫 {str(title)[:32]} · مطرودون {pending} · طلبات {requests}", callback_data=f"restricted_group:{int(cid)}")])
    if is_owner(uid):
        global_count = db.execute("SELECT COUNT(*) FROM global_bans WHERE active=1").fetchone()[0]
        rows.append([InlineKeyboardButton(f"🌐 الحظر العام النشط ({global_count})", callback_data="restricted_global")])
    rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


async def owner_group_feature_command(update, context):
    """Owner-only per-group controls for stats and membership notices."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not is_owner(user.id):
        if msg: await msg.reply_text("⛔ هذه الإعدادات للمالك الأساسي فقط.")
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("استخدم هذا الأمر داخل القروب الذي تريد ضبطه.")
        return
    cmd = (msg.text or "").split()[0].lower().lstrip("/")
    mapping = {
        "joinalerts_on": ("join_notices_enabled", 1, "📥 تم تشغيل إشعارات دخول الأعضاء."),
        "joinalerts_off": ("join_notices_enabled", 0, "🔕 تم إيقاف إشعارات دخول الأعضاء."),
        "leavealerts_on": ("leave_notices_enabled", 1, "📤 تم تشغيل إشعارات خروج الأعضاء."),
        "leavealerts_off": ("leave_notices_enabled", 0, "🔕 تم إيقاف إشعارات خروج الأعضاء."),
    }
    field, enabled, response = mapping[cmd]
    register_group(chat, user)
    if field == "join_approval_enabled" and enabled:
        # Do not persist an enabled state until Telegram confirms the request-only link exists.
        try:
            _request_link = await create_managed_join_request_link(context.bot, chat.id)
            set_group_feature(chat.id, field, 1)
            response += f"\n\n🔗 رابط طلب الانضمام الجديد:\n{_request_link}\nوزّع هذا الرابط بدل الروابط القديمة. قد تحتاج الروابط التي أنشأها مسؤول آخر إلى إلغاء يدوي."
        except Exception as _create_link_exc:
            set_group_feature(chat.id, field, 0)
            log.warning("Could not enable join approval for chat %s: %s", chat.id, _create_link_exc, exc_info=True)
            response = "⚠️ لم يتم تفعيل قفل الدخول؛ بقي القروب على حالته السابقة.\nالسبب: " + str(_create_link_exc)[:700]
    else:
        set_group_feature(chat.id, field, enabled)
    if field == "join_approval_enabled" and not enabled:
        _managed_links = db.execute("SELECT invite_link FROM managed_join_invite_links WHERE chat_id=? AND active=1", (chat.id,)).fetchall()
        for (_managed_link,) in _managed_links:
            try:
                await context.bot.revoke_chat_invite_link(chat_id=chat.id, invite_link=_managed_link)
                db.execute("UPDATE managed_join_invite_links SET active=0,revoked_at=? WHERE chat_id=? AND invite_link=?", (now(), chat.id, _managed_link))
            except Exception as _link_exc:
                log.info("Could not revoke managed invite link chat=%s: %s", chat.id, _link_exc)
        db.commit()
    log_action(chat.id, user.id, "group_feature_toggle", f"{field}={enabled}")
    await msg.reply_text(response)

def all_message_groups_markup(context, action):
    selected = set(context.user_data.get("all_message_lock_selected", set()))
    groups = db.execute("SELECT chat_id,COALESCE(title,CAST(chat_id AS TEXT)) FROM watched_groups ORDER BY title,chat_id").fetchall()
    rows = []
    for chat_id, title in groups[:45]:
        allowed = is_owner(context.user_data.get("_lockall_uid", 0)) or can_use_capability(context.user_data.get("_lockall_uid", 0), "message_lock", int(chat_id))
        if not allowed:
            continue
        mark = "✅" if int(chat_id) in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {str(title or chat_id)[:40]}", callback_data=f"allmsg_toggle:{int(chat_id)}:{action}")])
    rows.append([InlineKeyboardButton("✅ تحديد الكل المتاح", callback_data=f"allmsg_all:{action}"), InlineKeyboardButton("🧹 إلغاء التحديد", callback_data=f"allmsg_none:{action}")])
    rows.append([InlineKeyboardButton("🔒 تنفيذ القفل/الفتح", callback_data=f"allmsg_apply:{action}")])
    return InlineKeyboardMarkup(rows)

async def lock_all_groups_text(update, context):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or msg.chat.type != ChatType.PRIVATE:
        return
    text = (msg.text or "").strip()
    if text not in {"قفل رسايل بالبوت", "قفل رسائل بالبوت", "فتح رسايل بالبوت", "فتح رسائل بالبوت", "قفل الدردشة بالبوت", "فتح الدردشة بالبوت", "قفل المحادثة بالبوت", "فتح المحادثة بالبوت"}:
        return
    action = "lock" if text.startswith("قفل") else "unlock"
    if not is_owner(user.id) and not capability_group_ids(user.id, "message_lock"):
        await msg.reply_text("⛔ هذه الواجهة تتطلب صلاحية قفل وفتح الرسائل التي يمنحها المالك الأساسي.")
        return
    context.user_data["_lockall_uid"] = user.id
    context.user_data["all_message_lock_action"] = action
    context.user_data["all_message_lock_selected"] = set()
    await msg.reply_text("اختر القروبات التي تريد " + ("قفل الرسائل فيها" if action == "lock" else "فتح الرسائل فيها") + ". ستطبق العملية على القروبات المحددة فقط.", reply_markup=all_message_groups_markup(context, action))

async def lock_all_messages_command(update, context):
    """Lock/unlock all member message types in the current group, using Telegram chat permissions."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if msg: await msg.reply_text("استخدم هذا الأمر داخل القروب.")
        return
    full_text = (msg.text or "").strip().lower()
    cmd = full_text.split()[0].lstrip("/") if full_text else ""
    lock = cmd == "lockall" or full_text in {"قفل رسايل", "قفل رسائل", "قفل الدردشة", "قفل المحادثة", "قفل الشات"}
    authorized = is_owner(user.id) or can_use_capability(user.id, "message_lock", chat.id)
    if not authorized:
        await msg.reply_text("⛔ لا تملك صلاحية قفل وفتح الرسائل. يتيح المالك الأساسي هذه الصلاحية من لوحة توزيع الصلاحيات.")
        return
    try:
        if lock:
            permissions = ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
                can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False)
            await context.bot.set_chat_permissions(chat_id=chat.id, permissions=permissions)
            result = "🔒 تم قفل إرسال الرسائل والوسائط للأعضاء في هذا القروب."
            action = "all_messages_locked"
        else:
            permissions = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True,
                can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)
            await context.bot.set_chat_permissions(chat_id=chat.id, permissions=permissions)
            result = "🔓 تم فتح إرسال الرسائل والوسائط في هذا القروب. راجع صلاحيات تيليجرام الخاصة بالقروب إذا كانت هناك قيود إضافية."
            action = "all_messages_unlocked"
        log_action(chat.id, user.id, action, "Telegram default chat permissions updated")
        await msg.reply_text(result)
    except Exception as exc:
        await msg.reply_text("❌ تعذر تغيير صلاحيات الرسائل. تأكد أن البوت مشرف ويملك صلاحية تقييد الأعضاء.\n" + str(exc)[:300])

async def group_stats_cmd(update, context):
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("استخدم الأمر داخل القروب لعرض إحصائيته.")
        return
    register_group(chat, update.effective_user)
    if not external_group_active(chat.id) and not is_owner(update.effective_user.id if update.effective_user else 0):
        await update.effective_message.reply_text("⛔ خدمات البوت متوقفة عن هذا القروب من المالك الأساسي."); return
    if (not group_feature_enabled(chat.id, "stats_enabled") or not external_group_cap_enabled(chat.id, "stats")) and not is_owner(update.effective_user.id if update.effective_user else 0):
        await update.effective_message.reply_text("⛔ إحصائيات القروب غير مفعّلة من المالك الأساسي."); return
    week = _week_start()
    row = db.execute("SELECT COALESCE(SUM(message_count),0), COUNT(*) FROM weekly_message_activity WHERE chat_id=? AND week_start=?", (chat.id, week)).fetchone()
    messages, active = row or (0, 0)
    known = db.execute("SELECT COUNT(*) FROM member_activity WHERE chat_id=?", (chat.id,)).fetchone()[0]
    joins = db.execute("SELECT COUNT(*) FROM weekly_membership_events WHERE chat_id=? AND event_type='join' AND substr(event_at,1,10)>=?", (chat.id, week)).fetchone()[0]
    leaves = db.execute("SELECT COUNT(*) FROM weekly_membership_events WHERE chat_id=? AND event_type='leave' AND substr(event_at,1,10)>=?", (chat.id, week)).fetchone()[0]
    rate = (active / known * 100) if known else 0
    top_rows = db.execute(
        "SELECT COALESCE(ma.display_name,'عضو'), wa.message_count FROM weekly_message_activity wa "
        "LEFT JOIN member_activity ma ON ma.chat_id=wa.chat_id AND ma.user_id=wa.user_id "
        "WHERE wa.chat_id=? AND wa.week_start=? ORDER BY wa.message_count DESC LIMIT 5", (chat.id, week)
    ).fetchall()
    top_text = "\n".join(f"{idx}. {name} — {count} رسالة" for idx, (name, count) in enumerate(top_rows, 1)) or "لا توجد بيانات كافية"
    await update.effective_message.reply_text(
        f"📊 إحصائية القروب لهذا الأسبوع\n👥 القروب: {chat.title or chat.id}\n"
        f"💬 الرسائل المرصودة: {messages:,}\n🗣️ الأعضاء المتفاعلون: {active:,}\n"
        f"📈 نسبة التفاعل التقديرية: {rate:.1f}% من الأعضاء المسجلين لدى البوت\n"
        f"🆕 الأعضاء المنضمون المرصودون: {joins}\n🚪 المغادرون المرصودون: {leaves}\n"
        f"👤 الأعضاء المسجلون في قاعدة البوت: {known}\n🏆 أكثر 5 أعضاء نشاطًا:\n{top_text}\n\n"
        "ℹ️ الإحصاءات تبدأ من وقت تفعيل الرصد، ولا تمثل سجلًا تاريخيًا كاملًا لتيليجرام."
    )

async def weekly_stats_worker(app):
    """Publish weekly group reports every Tuesday at 22:00 Asia/Riyadh; send copies to owner and delegates."""
    while True:
        try:
            now_local = _riyadh_now()
            # Tuesday is weekday 1; fixed local time is 22:00 in Riyadh.
            days_until_tuesday = (1 - now_local.weekday()) % 7
            target = now_local.replace(hour=22, minute=0, second=0, microsecond=0) + __import__("datetime").timedelta(days=days_until_tuesday)
            if target <= now_local:
                target += __import__("datetime").timedelta(days=7)
            await asyncio.sleep(max(30, (target - now_local).total_seconds()))
            now_local = _riyadh_now()
            current_week = _week_start(now_local)
            # Report the last fully completed Monday-Sunday week.
            previous_week = (now_local.date() - __import__("datetime").timedelta(days=now_local.weekday()+7)).isoformat()
            groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
            for chat_id, title in groups:
                if not group_feature_enabled(chat_id, "stats_enabled"):
                    continue
                sent = db.execute("SELECT 1 FROM weekly_stats_sent WHERE chat_id=? AND week_start=?", (chat_id, previous_week)).fetchone()
                if sent:
                    continue
                stats = db.execute("SELECT COALESCE(SUM(message_count),0), COUNT(*) FROM weekly_message_activity WHERE chat_id=? AND week_start=?", (chat_id, previous_week)).fetchone()
                messages, active = stats or (0, 0)
                known = db.execute("SELECT COUNT(*) FROM member_activity WHERE chat_id=?", (chat_id,)).fetchone()[0]
                joins = db.execute("SELECT COUNT(*) FROM weekly_membership_events WHERE chat_id=? AND event_type='join' AND substr(event_at,1,10)>=? AND substr(event_at,1,10)<?", (chat_id, previous_week, current_week)).fetchone()[0]
                leaves = db.execute("SELECT COUNT(*) FROM weekly_membership_events WHERE chat_id=? AND event_type='leave' AND substr(event_at,1,10)>=? AND substr(event_at,1,10)<?", (chat_id, previous_week, current_week)).fetchone()[0]
                rate = active / known * 100 if known else 0
                top_rows = db.execute(
                    "SELECT COALESCE(ma.display_name,'عضو'), wa.message_count FROM weekly_message_activity wa "
                    "LEFT JOIN member_activity ma ON ma.chat_id=wa.chat_id AND ma.user_id=wa.user_id "
                    "WHERE wa.chat_id=? AND wa.week_start=? ORDER BY wa.message_count DESC LIMIT 5",
                    (chat_id, previous_week)
                ).fetchall()
                top_text = "\n".join(f"{idx}. {html.escape(name)} — {count} رسالة" for idx, (name, count) in enumerate(top_rows, 1)) or "لا توجد بيانات كافية"
                try:
                    member_count = await app.bot.get_chat_member_count(chat_id)
                    member_count_text = f"{member_count:,}"
                except Exception:
                    member_count_text = "غير متاح من تيليجرام"
                stars_rows = db.execute("SELECT COALESCE(SUM(points),0) FROM member_reputation WHERE chat_id=?", (chat_id,)).fetchone()
                total_stars = int((stars_rows or (0,))[0] or 0)
                report = (f"📊 التقرير الأسبوعي للقروب\n👥 {html.escape(title or str(chat_id))}\n"
                    f"📅 الفترة: الاثنين إلى الأحد السابق\n👥 إجمالي أعضاء القروب الآن: {member_count_text}\n"
                    f"💬 الرسائل المرصودة خلال الأسبوع: {messages:,}\n🗣️ الأعضاء المتفاعلون: {active:,}\n📈 نسبة التفاعل التقديرية: {rate:.1f}%\n"
                    f"🆕 المنضمون المرصودون: {joins}\n🚪 المغادرون المرصودون: {leaves}\n"
                    f"⭐ مجموع نقاط التفاعل المسجلة: {total_stars:,}\n🏆 أبرز المتفاعلين:\n{top_text}\n\n"
                    "💙 شكرًا لكل من شارك وساعد زملاءه وأثرى القروب بمعلوماته. كل مساهمة نافعة تقرّبنا من النجاح والتفوق!\n"
                    "🌟 واصلوا التميز؛ فالتعاون والاجتهاد يصنعان فرقًا كبيرًا.\n\n"
                    "ℹ️ الأرقام مبنية على ما رصده البوت منذ تفعيل الإحصائيات، ولا تشمل سجلًا تاريخيًا لم يتم جمعه.")
                try:
                    await app.bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")
                    # Send a private copy to the primary owner and registered delegated owners.
                    recipients = set()
                    if OWNER_ID:
                        recipients.add(int(OWNER_ID))
                    recipients.update(int(row[0]) for row in db.execute("SELECT user_id FROM delegated_owners").fetchall())
                    for recipient_id in recipients:
                        try:
                            await app.bot.send_message(chat_id=recipient_id, text="📋 نسخة إدارية من التقرير الأسبوعي\n\n" + report, parse_mode="HTML")
                        except Exception as copy_exc:
                            log.info("تعذر إرسال نسخة التقرير الأسبوعي إلى %s: %s", recipient_id, copy_exc)
                    db.execute("INSERT OR IGNORE INTO weekly_stats_sent(chat_id,week_start,sent_at) VALUES(?,?,?)", (chat_id, previous_week, now()))
                    db.commit()
                except Exception as exc:
                    log.warning("تعذر إرسال التقرير الأسبوعي للقروب %s: %s", chat_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("خطأ في عامل الإحصائيات الأسبوعية: %s", exc)
            await asyncio.sleep(60)

async def daily_activity_settings_cmd(update, context):
    """Owner-controlled daily public engagement post; default threshold is five messages."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if not (is_owner(user.id) or is_delegated_owner(user.id)):
        await msg.reply_text("⛔ إعداد نشرة المتفاعلين متاح للمالك والمفوّض فقط.")
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("استخدم الأمر داخل القروب لتحديد إعداداته.\n/dailyactivity on — تشغيل النشرة\n/dailyactivity off — إيقافها\n/dailyactivity 10 — نشر أسماء من أرسلوا 10 رسائل أو أكثر\n/dailyactivity status — عرض الإعدادات")
        return
    if is_delegated_owner(user.id) and not is_owner(user.id):
        if not await can_access_group(context.bot, user.id, chat.id):
            await msg.reply_text("⛔ لا تملك صلاحية إدارة إعدادات هذا القروب.")
            return
    register_group(chat)
    row = db.execute("SELECT enabled,min_messages FROM member_daily_report_settings WHERE chat_id=?", (chat.id,)).fetchone()
    enabled, minimum = (int(row[0]), int(row[1])) if row else (1, 5)
    arg = (context.args[0].strip().lower() if context.args else "status")
    if arg in ("on", "تشغيل", "تفعيل"):
        enabled = 1
    elif arg in ("off", "إيقاف", "ايقاف", "تعطيل"):
        enabled = 0
    elif arg in ("status", "الحالة"):
        pass
    elif arg.isdigit():
        minimum = int(arg)
        if minimum < 1 or minimum > 10000:
            await msg.reply_text("حدد حد الرسائل بين 1 و10000.")
            return
    else:
        await msg.reply_text("الصيغة: /dailyactivity on أو /dailyactivity off أو /dailyactivity 10 أو /dailyactivity status")
        return
    db.execute("INSERT INTO member_daily_report_settings(chat_id,enabled,min_messages,updated_by,updated_at) VALUES(?,?,?,?,?) "
               "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,min_messages=excluded.min_messages,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
               (chat.id, enabled, minimum, user.id, now()))
    db.commit()
    log_action(chat.id, user.id, "daily_activity_report_settings", f"enabled={enabled}; min_messages={minimum}")
    await msg.reply_text(
        "🏆 إعداد نشرة المتفاعلين اليومية\n"
        f"📣 الحالة: {'مفعّلة' if enabled else 'متوقفة'}\n"
        f"💬 الحد الأدنى للرسائل: {minimum} رسالة يوميًا\n"
        "🕚 موعد النشر: 11:00 مساءً بتوقيت السعودية\n"
        "🧾 تتضمن النشرة: الاسم، عدد الرسائل، النقاط، اللقب، وترتيب التفاعل."
    )


async def daily_member_activity_worker(app):
    """Publish a daily member leaderboard in each opted-in group at 23:00 Riyadh time."""
    while True:
        try:
            now_local = _riyadh_now()
            target = now_local.replace(hour=23, minute=0, second=0, microsecond=0)
            if target <= now_local:
                target += __import__("datetime").timedelta(days=1)
            await asyncio.sleep(max(5, (target - now_local).total_seconds()))
            report_date = _riyadh_now().date().isoformat()
            groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
            for chat_id, title in groups:
                try:
                    if not group_feature_enabled(chat_id, "stats_enabled"):
                        continue
                    sent = db.execute("SELECT 1 FROM member_daily_reports_sent WHERE chat_id=? AND activity_date=?", (chat_id, report_date)).fetchone()
                    if sent:
                        continue
                    setting = db.execute("SELECT enabled,min_messages FROM member_daily_report_settings WHERE chat_id=?", (chat_id,)).fetchone()
                    enabled, minimum = (int(setting[0]), int(setting[1])) if setting else (1, 5)
                    if not enabled:
                        db.execute("INSERT OR IGNORE INTO member_daily_reports_sent(chat_id,activity_date,sent_at) VALUES(?,?,?)", (chat_id, report_date, now()))
                        db.commit()
                        continue
                    rows = db.execute(
                        "SELECT d.user_id,d.message_count,COALESCE(a.display_name,'عضو'),COALESCE(r.points,0) "
                        "FROM member_daily_activity d "
                        "LEFT JOIN member_activity a ON a.chat_id=d.chat_id AND a.user_id=d.user_id "
                        "LEFT JOIN member_reputation r ON r.chat_id=d.chat_id AND r.user_id=d.user_id "
                        "WHERE d.chat_id=? AND d.activity_date=? AND d.message_count>=? "
                        "ORDER BY d.message_count DESC,COALESCE(r.points,0) DESC LIMIT 10",
                        (chat_id, report_date, minimum)
                    ).fetchall()
                    totals = db.execute("SELECT COALESCE(SUM(message_count),0),COUNT(*) FROM member_daily_activity WHERE chat_id=? AND activity_date=?", (chat_id, report_date)).fetchone()
                    total_messages, active_members = totals or (0, 0)
                    lines = [
                        "🏆 <b>حصاد المتفاعلين اليومي</b>",
                        f"🌟 <b>{html.escape(title or str(chat_id))}</b>",
                        f"📅 التاريخ: {report_date}",
                        "━━━━━━━━━━━━━━━━━━",
                        f"💬 إجمالي الرسائل المرصودة: <b>{int(total_messages):,}</b>",
                        f"👥 الأعضاء المتفاعلون اليوم: <b>{int(active_members):,}</b>",
                        f"🎯 شرط دخول القائمة: <b>{minimum}</b> رسائل على الأقل",
                        "━━━━━━━━━━━━━━━━━━"
                    ]
                    if rows:
                        medals = ["🥇", "🥈", "🥉"]
                        for idx, (uid, count, name, points) in enumerate(rows, 1):
                            title_name = member_title_for(chat_id, int(points))
                            medal = medals[idx-1] if idx <= 3 else f"<b>{idx}.</b>"
                            safe_name = html.escape(name or "عضو")
                            mention = f'<a href="tg://user?id={int(uid)}">{safe_name}</a>'
                            lines.append(f"{medal} {mention}  ·  💬 <b>{int(count)}</b> رسالة  ·  ⭐ <b>{int(points)}</b> نقطة\n     🏷️ {html.escape(title_name)}")
                    else:
                        lines.append("🌱 لم يصل أي عضو إلى حد الرسائل المحدد اليوم. شاركوا غدًا لتظهر أسماؤكم في القائمة!")
                    lines.extend(["━━━━━━━━━━━━━━━━━━", "✨ شكرًا لكل من يثري القروب بمشاركة مفيدة واحترام الجميع.", "ℹ️ الإحصاءات تعتمد على الرسائل التي رصدها البوت منذ تفعيل التسجيل، وقد لا تشمل الرسائل التي لم تصل إليه."])
                    await app.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
                    db.execute("INSERT OR IGNORE INTO member_daily_reports_sent(chat_id,activity_date,sent_at) VALUES(?,?,?)", (chat_id, report_date, now()))
                    db.commit()
                except Exception as exc:
                    log.warning("تعذر نشر نشرة المتفاعلين اليومية في القروب %s: %s", chat_id, exc)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("خطأ في عامل نشرة المتفاعلين اليومية")
            await asyncio.sleep(60)


DEFAULT_MEMBER_TITLES = [
    (0, "عضو جديد"), (10, "عضو نشيط"), (50, "عضو متفاعل"),
    (150, "نجم القروب"), (400, "عضو مميز"), (1000, "أسطورة القروب")
]
MEMBER_POINTS_COOLDOWN = 600  # نقطة تلقائية واحدة كل 10 دقائق لكل عضو في كل قروب

def ensure_member_title_rules(chat_id):
    if not db.execute("SELECT 1 FROM member_title_rules WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone():
        db.executemany("INSERT OR IGNORE INTO member_title_rules(chat_id,min_points,title) VALUES(?,?,?)",
                       [(chat_id, pts, title) for pts, title in DEFAULT_MEMBER_TITLES])
        db.commit()

def member_title_for(chat_id, points):
    ensure_member_title_rules(chat_id)
    row = db.execute("SELECT title FROM member_title_rules WHERE chat_id=? AND min_points<=? ORDER BY min_points DESC LIMIT 1",
                      (chat_id, points)).fetchone()
    return row[0] if row else "عضو جديد"

def award_member_points(chat_id, user_id, points, event_type, awarded_by=0, reason=""):
    if not points:
        return
    db.execute("INSERT INTO member_reputation(chat_id,user_id,points,last_auto_award,updated_at) VALUES(?,?,?,0,?) "
               "ON CONFLICT(chat_id,user_id) DO UPDATE SET points=MAX(0,member_reputation.points+?),updated_at=excluded.updated_at",
               (chat_id, user_id, max(0, points), now(), points))
    db.execute("INSERT INTO member_points_events(chat_id,user_id,points,event_type,awarded_by,reason,created_at) VALUES(?,?,?,?,?,?,?)",
               (chat_id, user_id, points, event_type, awarded_by, (reason or "")[:250], now()))
    db.commit()

async def member_points_cmd(update, context):
    msg=update.effective_message; chat=update.effective_chat; user=update.effective_user
    if not msg or not chat or not user:
        return
    target=user
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target=msg.reply_to_message.from_user
    elif context.args and context.args[0].isdigit() and (is_owner(user.id) or is_delegated_owner(user.id)):
        target_id=int(context.args[0])
        row=db.execute("SELECT display_name FROM member_activity WHERE chat_id=? AND user_id=?",(chat.id,target_id)).fetchone()
        points_row=db.execute("SELECT points FROM member_reputation WHERE chat_id=? AND user_id=?",(chat.id,target_id)).fetchone()
        pts=points_row[0] if points_row else 0
        await msg.reply_text(f"⭐ نقاط العضو {target_id}: {pts}\n🏷️ اللقب: {member_title_for(chat.id,pts)}")
        return
    row=db.execute("SELECT points FROM member_reputation WHERE chat_id=? AND user_id=?",(chat.id,target.id)).fetchone()
    pts=row[0] if row else 0
    rank=db.execute("SELECT COUNT(*)+1 FROM member_reputation WHERE chat_id=? AND points>?",(chat.id,pts)).fetchone()[0]
    await msg.reply_text(f"⭐ نقاط {target.full_name}: {pts}\n🏷️ اللقب: {member_title_for(chat.id,pts)}\n🏆 ترتيبك في القروب: {rank}")

async def member_top_cmd(update, context):
    msg=update.effective_message; chat=update.effective_chat
    if not msg or not chat:
        return
    rows=db.execute("SELECT r.user_id,COALESCE(a.display_name,'عضو'),r.points FROM member_reputation r LEFT JOIN member_activity a ON a.chat_id=r.chat_id AND a.user_id=r.user_id WHERE r.chat_id=? ORDER BY r.points DESC,r.updated_at ASC LIMIT 20",(chat.id,)).fetchall()
    if not rows:
        await msg.reply_text("🏆 لا توجد نقاط أعضاء بعد. تُحتسب النقاط تدريجيًا مع المشاركة المفيدة.")
        return
    lines=["🏆 لوحة أفضل أعضاء القروب"]
    for i,(uid,name,pts) in enumerate(rows,1):
        lines.append(f"{i}. {name} — ⭐ {pts} — {member_title_for(chat.id,pts)}")
    await msg.reply_text("\n".join(lines))

async def give_member_points_cmd(update, context):
    msg=update.effective_message; user=update.effective_user; chat=update.effective_chat
    if not msg or not user or not chat:
        return
    if not (is_owner(user.id) or is_delegated_owner(user.id)):
        await msg.reply_text("⛔ منح أو خصم نقاط الأعضاء متاح للمالك والمفوّض فقط."); return
    target_id=None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id=msg.reply_to_message.from_user.id
        args=context.args
    elif len(context.args)>=2 and context.args[0].lstrip('-').isdigit():
        target_id=int(context.args[0]); args=context.args[1:]
    else:
        await msg.reply_text("الصيغة: رد على العضو واكتب /givepoints عدد السبب\nأو /givepoints ID عدد السبب"); return
    if not args or not args[0].lstrip('-').isdigit():
        await msg.reply_text("حدد عدد النقاط موجبًا للمنح أو سالبًا للخصم."); return
    amount=int(args[0])
    if amount==0 or abs(amount)>10000:
        await msg.reply_text("النقاط يجب أن تكون بين -10000 و10000 وألا تساوي صفرًا."); return
    reason=" ".join(args[1:])[:250] or "تعديل إداري"
    current=db.execute("SELECT points FROM member_reputation WHERE chat_id=? AND user_id=?",(chat.id,target_id)).fetchone()
    if amount<0 and (not current or current[0] < abs(amount)):
        await msg.reply_text("لا يمكن خصم نقاط أكثر من رصيد العضو الحالي."); return
    award_member_points(chat.id,target_id,amount,"admin_adjustment",user.id,reason)
    log_action(chat.id,user.id,"member_points_adjusted",f"target={target_id}; points={amount}; reason={reason}")
    total=db.execute("SELECT points FROM member_reputation WHERE chat_id=? AND user_id=?",(chat.id,target_id)).fetchone()[0]
    await msg.reply_text(f"✅ تم تعديل نقاط العضو {target_id} بمقدار {amount:+d}.\n⭐ الرصيد الحالي: {total}\n🏷️ اللقب الحالي: {member_title_for(chat.id,total)}")

async def set_member_title_cmd(update, context):
    msg=update.effective_message; user=update.effective_user; chat=update.effective_chat
    if not msg or not user or not chat:
        return
    if not (is_owner(user.id) or is_delegated_owner(user.id)):
        await msg.reply_text("⛔ إعداد الألقاب للمالك والمفوّض فقط."); return
    if len(context.args)<2 or not context.args[0].isdigit():
        await msg.reply_text("الصيغة: /setmembertitle الحد_الأدنى_للنقاط اسم_اللقب\nمثال: /setmembertitle 250 قائد التفاعل"); return
    threshold=int(context.args[0]); title=" ".join(context.args[1:]).strip()[:60]
    if threshold<0 or threshold>10000000 or not title:
        await msg.reply_text("تحقق من عدد النقاط واسم اللقب."); return
    ensure_member_title_rules(chat.id)
    db.execute("INSERT INTO member_title_rules(chat_id,min_points,title) VALUES(?,?,?) ON CONFLICT(chat_id,min_points) DO UPDATE SET title=excluded.title",(chat.id,threshold,title)); db.commit()
    log_action(chat.id,user.id,"member_title_rule_set",f"threshold={threshold}; title={title}")
    await msg.reply_text(f"✅ تم ضبط اللقب «{title}» ابتداءً من {threshold} نقطة.")

async def message_filter(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    uid = msg.from_user.id if msg.from_user else 0
    # Activation is handled in the Arabic command layer; do not let anti-spam/locks
    # delete the activation instruction before its confirmation is delivered.
    if is_bot_activation_phrase(msg.text or ""):
        return
    register_group(chat)
    if not external_group_active(chat.id):
        return
    if msg.from_user and not msg.from_user.is_bot:
        week_key = _week_start()
        db.execute("INSERT INTO weekly_message_activity(chat_id,week_start,user_id,message_count) VALUES(?,?,?,1) ON CONFLICT(chat_id,week_start,user_id) DO UPDATE SET message_count=weekly_message_activity.message_count+1", (chat.id, week_key, msg.from_user.id))
        activity_date = _riyadh_now().date().isoformat()
        db.execute("INSERT INTO member_daily_activity(chat_id,activity_date,user_id,message_count,last_message_at) VALUES(?,?,?,1,?) "
                   "ON CONFLICT(chat_id,activity_date,user_id) DO UPDATE SET message_count=member_daily_activity.message_count+1,last_message_at=excluded.last_message_at",
                   (chat.id, activity_date, msg.from_user.id, now()))
        db.commit()
        # نقطة تفاعل تلقائية بحد أقصى مرة كل 10 دقائق للعضو في القروب، لتقليل استغلال الرسائل المتكررة.
        if msg.text and len(msg.text.strip()) >= 3 and not msg.text.strip().startswith("/"):
            ensure_member_title_rules(chat.id)
            reputation = db.execute("SELECT last_auto_award FROM member_reputation WHERE chat_id=? AND user_id=?", (chat.id, uid)).fetchone()
            current_epoch = time.time()
            if not reputation or current_epoch - float(reputation[0] or 0) >= MEMBER_POINTS_COOLDOWN:
                db.execute("INSERT INTO member_reputation(chat_id,user_id,points,last_auto_award,updated_at) VALUES(?,?,1,?,?) "
                           "ON CONFLICT(chat_id,user_id) DO UPDATE SET points=member_reputation.points+1,last_auto_award=excluded.last_auto_award,updated_at=excluded.updated_at",
                           (chat.id, uid, current_epoch, now()))
                db.execute("INSERT INTO member_points_events(chat_id,user_id,points,event_type,awarded_by,reason,created_at) VALUES(?,?,1,'message_activity',0,'مشاركة نصية مع فترة تهدئة',?)", (chat.id, uid, now()))
                db.commit()
    # الردود الآلية: تهيئة الردود الافتراضية مرة واحدة لكل قروب، مع تهدئة لمنع الإزعاج.
    if msg.from_user and not msg.from_user.is_bot and msg.text and not msg.text.startswith("/"):
        ensure_auto_replies(chat.id)
        enabled_row = db.execute("SELECT enabled FROM auto_reply_settings WHERE chat_id=?", (chat.id,)).fetchone()
        if enabled_row and enabled_row[0]:
            normalized = normalize_auto_reply(msg.text)
            bot_username = getattr(context.bot, "username", "") or ""
            direct_call = is_direct_bot_call(msg.text, bot_username)
            rules = db.execute("SELECT trigger_text,reply_text FROM auto_replies WHERE chat_id=? AND enabled=1 ORDER BY LENGTH(trigger_text) DESC", (chat.id,)).fetchall()
            for trigger, reply in rules:
                needle = normalize_auto_reply(trigger)
                if not needle:
                    continue
                # The short nickname must only fire when the bot is directly addressed.
                if needle == "بوت" and not direct_call:
                    continue
                if needle in normalized:
                    key = (chat.id, trigger)
                    now_ts = time.monotonic()
                    if now_ts - _auto_reply_last.get(key, -1e9) >= _AUTO_REPLY_COOLDOWN_SECONDS:
                        try:
                            await msg.reply_text(reply)
                            _auto_reply_last[key] = now_ts
                        except Exception as exc:
                            log.warning("تعذر إرسال الرد الآلي في %s: %s", chat.id, exc)
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
            changed = (old_name != current_name) or (old_username != current_username)
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
    # Smart member summon: only explicit summon words trigger a mass mention, with per-group cooldown.
    if msg.from_user and not msg.from_user.is_bot and msg.text:
        summon_text = normalized_arabic_text(msg.text)
        summon_requested = bool(re.search(r"(?:^|\s)(?:تاك|تاق|منشن|استدعاء)(?:$|\s)", summon_text))
        if summon_requested:
            last_tag = float(context.chat_data.get("last_auto_mass_tag", 0) or 0)
            authorized_tag = is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "tag", chat.id)
            if external_group_info(chat.id) and external_group_info(chat.id)[0]:
                authorized_tag = external_group_cap_enabled(chat.id, "tag") and await is_telegram_group_admin(context.bot, uid, chat.id)
            if authorized_tag and time.monotonic() - last_tag >= 60:
                context.chat_data["last_auto_mass_tag"] = time.monotonic()
                try:
                    count, chunks = await send_member_mass_mention(context.bot, chat.id)
                    if count:
                        log_action(chat.id, uid, "smart_mass_mention", f"members={count}; chunks={chunks}")
                    else:
                        await msg.reply_text("لا توجد أسماء أعضاء مسجلة بعد. سيبدأ البوت بالتعرف على الأعضاء من الرسائل والانضمامات الجديدة.")
                except Exception as exc:
                    log.warning("Smart mass mention failed chat=%s user=%s: %s", chat.id, uid, exc)
                    await msg.reply_text("تعذر إرسال التاق. تحقق من صلاحيات البوت وحدود تيليجرام.")
                return
    if msg.from_user and not msg.from_user.is_bot and archive_is_enabled(chat.id):
        body = msg.text or msg.caption or "[رسالة غير نصية]"
        media_file_id = ""
        content_type = "text" if msg.text else ("captioned_media" if msg.caption else "other")
        for _attr, _kind in (("photo", "photo"), ("video", "video"), ("document", "document"),
                             ("audio", "audio"), ("voice", "voice"), ("animation", "animation"),
                             ("video_note", "video_note"), ("sticker", "sticker"), ("contact", "contact"),
                             ("location", "location"), ("venue", "venue"), ("poll", "poll"), ("dice", "dice"),
                             ("game", "game"), ("paid_media", "paid_media")):
            _obj = getattr(msg, _attr, None)
            if _obj:
                content_type = _kind
                if _attr == "photo":
                    media_file_id = getattr(_obj[-1], "file_id", "") if _obj else ""
                else:
                    media_file_id = getattr(_obj, "file_id", "")
                break
        _origin = getattr(msg, "forward_origin", None)
        forward_origin = ""
        if _origin is not None:
            try:
                _origin_user = getattr(_origin, "sender_user", None)
                _origin_chat = getattr(_origin, "sender_chat", None)
                if _origin_user:
                    forward_origin = f"user:{_origin_user.id}"
                elif _origin_chat:
                    forward_origin = f"chat:{_origin_chat.id}:{getattr(_origin_chat, 'title', '')}"
                else:
                    forward_origin = str(getattr(_origin, "type", type(_origin).__name__))[:250]
            except Exception:
                forward_origin = type(_origin).__name__
        message_link = ""
        if getattr(chat, "username", None):
            message_link = f"https://t.me/{chat.username}/{msg.message_id}"
        elif str(chat.id).startswith("-100"):
            message_link = f"https://t.me/c/{str(chat.id)[4:]}/{msg.message_id}"
        db.execute(
            "INSERT INTO message_archive(chat_id,chat_title,user_id,username,display_name,message_id,message_text,created_at,content_type,forward_origin,media_file_id,message_link) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat.id, chat.title or "", uid, msg.from_user.username or "", msg.from_user.full_name or "",
             msg.message_id, body[:3000], now(), content_type, forward_origin, media_file_id, message_link)
        )
        db.commit()
    # Apply abusive-language warnings to all human senders (including admins); owner can still recover permissions.
    if (msg.from_user and not msg.from_user.is_bot and not is_owner(uid)
            and external_group_cap_enabled(chat.id, "protection")):
        abusive_word = detect_abusive_language(msg.text or msg.caption or "")
        if abusive_word:
            try:
                await msg.delete()
            except Exception as exc:
                log.info("Abusive message deletion failed chat=%s user=%s: %s", chat.id, uid, exc)
            await register_warning_and_enforce(context, chat, msg.from_user, f"لفظ مسيء: {abusive_word}", abusive=True)
            return

    # مالك البوت والطاقم المخوّل ومشرفو Telegram الفعليون معفيون من فلاتر الحذف التلقائي.
    # يمنع هذا حذف روابط/صور/فيديو المشرفين المعتمدين، مع بقاء الأعضاء العاديين تحت الحماية.
    if (is_manager(uid) or is_delegated_owner(uid) or is_protection_manager(uid)
            or can_use_capability(uid, "protection", chat.id)
            or group_role(chat.id, uid) in ("مشرف", "مشرف عام", "مدير", "مدير عام")):
        return
    if msg.from_user and not msg.from_user.is_bot:
        try:
            if await is_telegram_group_admin(context.bot, uid, chat.id):
                return
        except Exception as exc:
            # Fail closed: if Telegram admin status cannot be checked, continue normal filtering.
            log.debug("تعذر التحقق من صلاحية مشرف Telegram %s في %s: %s", uid, chat.id, exc)
    s = get_settings(chat.id)
    # External group owner may disable ordinary protection for this group only.
    if not external_group_cap_enabled(chat.id, "protection"):
        return
    if msg.from_user and not msg.from_user.is_bot and s.get("links", 0) and message_has_link(msg):
        try:
            await msg.delete()
        except Exception as exc:
            log.info("Link deletion failed chat=%s user=%s: %s", chat.id, uid, exc)
        await register_warning_and_enforce(context, chat, msg.from_user, "إرسال رابط أثناء قفل الروابط")
        log_action(chat.id, uid, "link_warning", "تم تسجيل إنذار للرابط المخالف")
        return
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
                await register_warning_and_enforce(context, chat, msg.from_user, "السبام وتكرار الرسائل (7 رسائل خلال 8 ثوانٍ)")
                await notify_owners(context, f"🧯 رصد إغراق رسائل\nالقروب: {chat.title or chat.id}\nالعضو: {msg.from_user.full_name} (ID: {uid})\nالإجراء: حذف الرسالة وتسجيل إنذار بعد رصد 7 رسائل خلال 8 ثوانٍ.", "security")
            return
    delete = False
    reason = ""

    # Links are handled above so they receive a warning and are recorded in the violations list.
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

async def notify_reentry_restriction(context, chat, member, reason):
    """Keep a departed/kicked member banned until a primary/delegated owner approves."""
    chat_id = chat.id
    username = member.username or ""
    display_name = member.full_name or "عضو بلا اسم"
    db.execute("""INSERT INTO reentry_restrictions
        (chat_id,user_id,display_name,username,reason,created_at,status)
        VALUES(?,?,?,?,?,?, 'pending')
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
        display_name=excluded.display_name, username=excluded.username,
        reason=excluded.reason, created_at=excluded.created_at, status='pending'""",
        (chat_id, member.id, display_name, username, reason, now()))
    db.commit()
    log_action(chat_id, member.id, "reentry_blocked", reason)
    warning_row = db.execute("SELECT warning_count FROM member_security_warnings WHERE chat_id=? AND user_id=?", (chat_id, member.id)).fetchone()
    warning_count = warning_row[0] if warning_row else 0
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ السماح له بالعودة", callback_data=f"reentry_approve:{chat_id}:{member.id}"),
         InlineKeyboardButton("⛔ إبقاؤه ممنوعًا", callback_data=f"reentry_keep:{chat_id}:{member.id}")],
        [InlineKeyboardButton(f"⚠️ تسجيل إنذار ({warning_count}/3)", callback_data=f"reentry_warn:{chat_id}:{member.id}")]
    ])
    text = (
        "🚨 تنبيه أمني: تم منع عضو من العودة حتى موافقة المالك أو المالك المفوّض.\n\n"
        f"👥 القروب: {chat.title or chat_id}\n"
        f"👤 الاسم الحالي: {display_name}\n"
        f"🔎 اسم المستخدم: @{username if username else 'غير متوفر'}\n"
        f"🆔 المعرّف الثابت: {member.id}\n"
        f"📌 السبب: {reason}\n"
        "ℹ️ يتم التعرف على العضو بواسطة المعرّف الثابت، حتى لو غيّر اسمه أو اسم المستخدم."
    )
    recipients = {OWNER_ID} if OWNER_ID else set()
    _reentry_candidates = {r[0] for r in db.execute("SELECT DISTINCT user_id FROM staff_capabilities WHERE capability='reentry'").fetchall()}
    recipients.update(candidate for candidate in _reentry_candidates if can_use_capability(candidate, "reentry", chat_id))
    for recipient in recipients:
        pref = db.execute("SELECT enabled FROM notification_preferences WHERE user_id=? AND event='membership'", (recipient,)).fetchone()
        if pref and not pref[0]:
            continue
        try:
            await context.bot.send_message(chat_id=recipient, text=text, reply_markup=keyboard)
        except Exception as exc:
            log.info("تعذر إرسال تنبيه منع العودة إلى %s: %s", recipient, exc)

async def chat_join_request_handler(update, context):
    """Persist Telegram join requests and route approval to owner-authorized staff."""
    req = getattr(update, "chat_join_request", None)
    if not req or not req.chat or not req.from_user:
        return
    chat, user = req.chat, req.from_user
    # Do not attribute group ownership/registration to the requesting member.
    register_group(chat)
    prior = db.execute("SELECT status,reason FROM reentry_restrictions WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
    approval_on = group_feature_enabled(chat.id, "join_approval_enabled")
    restricted = bool(prior and prior[0] in ("pending", "blocked"))
    if approval_on and not restricted:
        # If a direct invite was rejected by the approval gate, lift only that Telegram ban
        # so the person can submit a proper join request; no membership is granted here.
        try:
            await context.bot.unban_chat_member(chat_id=chat.id, user_id=user.id, only_if_banned=True)
        except Exception as exc:
            log.debug("No approval-gate ban to lift for join requester %s in %s: %s", user.id, chat.id, exc)
    db.execute("""INSERT INTO join_request_events(chat_id,user_id,display_name,username,status,requested_at,decided_at,decided_by)
        VALUES(?,?,?,?, 'pending', ?, '', 0) ON CONFLICT(chat_id,user_id) DO UPDATE SET
        display_name=excluded.display_name, username=excluded.username, status='pending', requested_at=excluded.requested_at, decided_at='', decided_by=0""",
        (chat.id, user.id, user.full_name or "عضو", user.username or "", now()))
    db.commit()
    if not approval_on and not restricted:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user.id)
            db.execute("UPDATE join_request_events SET status='approved',decided_at=?,decided_by=? WHERE chat_id=? AND user_id=?", (now(), OWNER_ID or 0, chat.id, user.id))
            db.commit()
        except Exception as exc:
            log.warning("Auto-approval of join request failed chat=%s user=%s: %s", chat.id, user.id, exc)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ موافقة على الدخول", callback_data=f"joinreq_approve:{chat.id}:{user.id}"),
         InlineKeyboardButton("❌ رفض الطلب", callback_data=f"joinreq_deny:{chat.id}:{user.id}")]
    ])
    text = ("🔐 طلب انضمام يحتاج مراجعة\n"
            f"القروب: {chat.title or chat.id} (ID: {chat.id})\n"
            f"العضو: {user.full_name or 'عضو'}\n"
            f"اسم المستخدم: @{user.username or 'غير متوفر'}\nالمعرّف: {user.id}\n"
            + (f"⚠️ تنبيه: لهذا العضو سجل سابق: {prior[1] or prior[0]}. فك الحظر وحده لا يُعد موافقة على العودة.\n" if restricted else "")
            + "اختر الموافقة أو الرفض. لا يُقبل الطلب إلا من مسؤول مخوّل.")
    recipients = {OWNER_ID} if OWNER_ID else set()
    _join_candidates = {r[0] for r in db.execute("SELECT DISTINCT user_id FROM staff_capabilities WHERE capability='join_approval'").fetchall()}
    _reentry_candidates = {r[0] for r in db.execute("SELECT DISTINCT user_id FROM staff_capabilities WHERE capability='reentry'").fetchall()}
    recipients.update(candidate for candidate in _join_candidates if can_use_capability(candidate, "join_approval", chat.id))
    recipients.update(candidate for candidate in _reentry_candidates if can_use_capability(candidate, "reentry", chat.id))
    for recipient in recipients:
        try:
            await context.bot.send_message(chat_id=recipient, text=text, reply_markup=keyboard)
        except Exception as exc:
            log.info("Join request notification failed recipient=%s chat=%s: %s", recipient, chat.id, exc)


async def my_chat_member_handler(update, context):
    """Register bot add/remove events so external groups appear even before /start or a message."""
    cm = getattr(update, "my_chat_member", None)
    if not cm or not cm.chat or cm.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    chat = cm.chat
    actor = getattr(cm, "from_user", None) or update.effective_user
    old_status = getattr(cm.old_chat_member, "status", "left")
    new_status = getattr(cm.new_chat_member, "status", "left")
    bot_joined = new_status in ("member", "administrator", "restricted") and old_status in ("left", "kicked")
    bot_left = new_status in ("left", "kicked") and old_status in ("member", "administrator", "restricted")

    if bot_joined:
        register_group(chat, actor)
        actor_id = int(getattr(actor, "id", 0) or 0)
        ext = external_group_info(chat.id)
        external = bool(ext and ext[0])
        # A previously disabled group stays disabled; adding the bot again does not bypass
        # the primary owner's remote kill switch. New registrations default to enabled.
        enabled = external_group_active(chat.id)
        role_text = "قروب خارجي" if external else "قروب تابع للمالك الأساسي"
        bot_admin_text = "مشرف" if new_status == "administrator" else "عضو فقط؛ بعض وظائف الحماية تحتاج رفع البوت إلى مشرف"
        # Include a verified public link when the group has a username. For private groups,
        # do not create or disclose a fresh invite link automatically; report that it is unavailable.
        group_link = "غير متاح (قد يكون القروب خاصًا أو لا يملك رابطًا عامًا)"
        try:
            chat_details = await context.bot.get_chat(chat.id)
            public_username = getattr(chat_details, "username", None)
            if public_username:
                group_link = f"https://t.me/{public_username}"
            else:
                invite = getattr(chat_details, "invite_link", None)
                if invite:
                    group_link = invite
        except Exception as link_exc:
            log.debug("Could not resolve group link for %s: %s", chat.id, link_exc)
        notice = (
            f"📥 تمت إضافة البوت إلى {role_text}\n"
            f"القروب: {(chat.title or str(chat.id))[:150]}\nالمعرّف: {chat.id}\n"
            f"رابط القروب: {group_link}\n"
            f"أضافه: {(getattr(actor, 'full_name', '') or 'غير معروف')[:100]} (ID: {actor_id or 'غير معروف'})"
            f"{' — @' + actor.username if getattr(actor, 'username', None) else ''}\n"
            f"صلاحية البوت في تيليجرام: {bot_admin_text}\n"
            f"حالة الخدمة: {'مفعّلة' if enabled else 'متوقفة بقرار سابق من المالك'}\n"
            "الذكاء الاصطناعي للقروبات الخارجية معطّل افتراضيًا."
        )
        # Group-registration details (especially links) are sent only to the primary owner.
        # Delegated owners can receive these details only through a separately authorized workflow.
        if OWNER_ID:
            try:
                await context.bot.send_message(chat_id=OWNER_ID, text=notice)
            except Exception as notify_exc:
                log.info("Could not notify primary owner about external group %s: %s", chat.id, notify_exc)
        log_action(chat.id, actor_id, "bot_added_to_group", f"external={int(external)}; status={new_status}")
        if global_join_approval_enabled() and new_status == "administrator":
            try:
                invite_link = await create_managed_join_request_link(context.bot, chat.id)
                if OWNER_ID:
                    await context.bot.send_message(chat_id=OWNER_ID, text=f"🔐 قفل الدخول المركزي مفعّل. رابط طلب الانضمام للقروب الجديد {chat.title or chat.id}:\n{invite_link}\nوزّع هذا الرابط بدل روابط الدخول المباشر.")
            except Exception as join_link_exc:
                log.warning("Global join approval link creation failed for new group %s: %s", chat.id, join_link_exc)
                if OWNER_ID:
                    try:
                        await context.bot.send_message(chat_id=OWNER_ID, text=f"⚠️ قفل الدخول المركزي مفعّل، لكن تعذر تجهيز قفل كامل/رابط طلب انضمام للقروب الجديد {chat.title or chat.id} ({chat.id}). السبب: {join_link_exc}")
                    except Exception:
                        pass
        return

    if bot_left:
        # Retain the group record for the owner's dashboard, but stop service until reviewed.
        db.execute("UPDATE external_group_control SET enabled=0,updated_at=? WHERE chat_id=?", (now(), chat.id))
        db.commit()
        actor_id = int(getattr(actor, "id", 0) or 0)
        log_action(chat.id, actor_id, "bot_removed_from_group", f"old={old_status}; new={new_status}")
        notice = (
            "⚠️ تمت إزالة البوت من قروب؛ تم إيقاف خدماته لهذا القروب في السجل.\n"
            f"القروب: {(chat.title or str(chat.id))[:150]}\nالمعرّف: {chat.id}\n"
            f"الإجراء بواسطة: {(getattr(actor, 'full_name', '') or 'غير معروف')[:100]} (ID: {actor_id or 'غير معروف'})\n"
            "يمكن للمالك الأساسي مراجعة القروب من أيقونة إدارة القروبات الخارجية."
        )
        await notify_owners(context, notice, "groups")


async def chat_member_handler(update, context):
    cm = update.chat_member
    if not cm or not cm.chat:
        return
    chat_id = cm.chat.id
    register_group(cm.chat, update.effective_user)
    # Define transition values before using them in membership statistics.
    old = cm.old_chat_member.status
    new = cm.new_chat_member.status
    member = cm.new_chat_member.user
    if member and not member.is_bot and new in ("member", "administrator", "restricted") and old in ("left", "kicked"):
        # A previous kick is remembered by stable Telegram ID. Removing a Telegram ban
        # alone does not approve re-entry; the owner-authorized decision is stored separately.
        if new != "administrator":
            restriction = db.execute("SELECT status,reason FROM reentry_restrictions WHERE chat_id=? AND user_id=?", (chat_id, member.id)).fetchone()
            if restriction and restriction[0] in ("pending", "blocked"):
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.id)
                    log_action(chat_id, member.id, "reentry_reblocked", restriction[1] or restriction[0])
                    await notify_reentry_restriction(context, cm.chat, member, restriction[1] or "عضو مطرود سابقًا؛ يحتاج موافقة المسؤول")
                except Exception as exc:
                    log.warning("Reentry enforcement failed chat=%s user=%s: %s", chat_id, member.id, exc)
                return
            if group_feature_enabled(chat_id, "join_approval_enabled"):
                request_row = db.execute("SELECT status FROM join_request_events WHERE chat_id=? AND user_id=?", (chat_id, member.id)).fetchone()
                if not request_row or request_row[0] != "approved":
                    try:
                        log_action(chat_id, member.id, "join_approval_required", "direct join blocked; join request required")
                        await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.id)
                        await notify_owners(context, f"🔐 تم منع دخول غير معتمد\nالقروب: {cm.chat.title or chat_id}\nالعضو: {member.full_name} (ID: {member.id})\nالسبب: الموافقة المسبقة مفعّلة؛ استخدم رابط طلب الانضمام.", "membership")
                    except Exception as exc:
                        log.warning("Join approval enforcement failed chat=%s user=%s: %s", chat_id, member.id, exc)
                    return
                db.execute("UPDATE join_request_events SET status='joined' WHERE chat_id=? AND user_id=?", (chat_id, member.id))
                db.commit()
        if global_ban_applies(member.id, chat_id):
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.id)
                log_action(chat_id, member.id, "global_ban_reentry", "منع عودة عضو محظور جماعيًا")
                await context.bot.send_message(chat_id=chat_id, text=f"🚫 تم منع {html.escape(member.full_name or 'العضو')} من العودة بسبب حظر إداري مسجل.", parse_mode="HTML")
            except Exception as exc:
                log.warning("Global-ban reentry block failed chat=%s user=%s: %s", chat_id, member.id, exc)
            return
    if member and not member.is_bot and not external_group_active(chat_id):
        return
    if member and not member.is_bot:
        if old in ("left", "kicked") and new in ("member", "administrator", "restricted"):
            db.execute("INSERT INTO weekly_membership_events(chat_id,event_type,event_at) VALUES(?,?,?)", (chat_id, "join", _riyadh_now().isoformat()))
            db.commit()
            if group_feature_enabled(chat_id, "join_notices_enabled"):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"📥 انضم عضو جديد إلى القروب: {html.escape(member.full_name or 'عضو')}\n🆔 ID: {member.id}", parse_mode="HTML")
                except Exception as exc:
                    log.info("Join notice failed chat=%s user=%s: %s", chat_id, member.id, exc)
        elif old in ("member", "administrator", "restricted") and new in ("left", "kicked"):
            db.execute("INSERT INTO weekly_membership_events(chat_id,event_type,event_at) VALUES(?,?,?)", (chat_id, "leave", _riyadh_now().isoformat()))
            db.commit()
            if group_feature_enabled(chat_id, "leave_notices_enabled"):
                try:
                    reason = "تمت إزالته/حظره" if new == "kicked" else "غادر القروب"
                    await context.bot.send_message(chat_id=chat_id, text=f"📤 العضو {html.escape(member.full_name or 'عضو')} {reason}.\n🆔 ID: {member.id}", parse_mode="HTML")
                except Exception as exc:
                    log.info("Leave notice failed chat=%s user=%s: %s", chat_id, member.id, exc)
    s = get_settings(chat_id)
    if member and old in ("member", "administrator", "restricted") and new in ("left", "kicked") and not member.is_bot:
        reason = "غادر القروب بنفسه" if new == "left" else "تم حظره/طرده من القروب"
        log_action(chat_id, member.id, "member_leave", f"{member.full_name} ({new})")
        if new == "left":
            # Voluntary departure must not automatically ban a member or block future requests.
            try:
                await notify_owners(
                    context,
                    f"🚪 غادر عضو القروب طوعًا\nالقروب: {cm.chat.title or chat_id}\n"
                    f"العضو: {member.full_name} (ID: {member.id})\n"
                    "لم يفرض البوت أي حظر تلقائي، ويمكن للعضو العودة وفق إعدادات تيليجرام.",
                    "membership"
                )
            except Exception as exc:
                log.debug("Voluntary departure notification failed chat=%s user=%s: %s", chat_id, member.id, exc)
        else:
            # A member removed only because they used a direct link while join approval is on
            # must be able to request access through the dedicated request link; do not classify
            # that enforcement as a manual expulsion requiring a second approval.
            since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            approval_enforcement = db.execute("SELECT 1 FROM logs WHERE chat_id=? AND user_id=? AND action='join_approval_required' AND created_at>=? ORDER BY id DESC LIMIT 1", (chat_id, member.id, since)).fetchone()
            if not approval_enforcement:
                try:
                    await notify_reentry_restriction(context, cm.chat, member, reason)
                except Exception as exc:
                    log.warning("Could not record an existing kick/ban for %s in %s: %s", member.id, chat_id, exc)
    elif member and old in ("left", "kicked") and new in ("member", "administrator", "restricted") and not member.is_bot:
        db.execute(
            "INSERT INTO member_activity(chat_id,user_id,display_name,message_count,stars,custom_title,username) "
            "VALUES(?,?,?,0,0,'',?) ON CONFLICT(chat_id,user_id) DO UPDATE SET "
            "display_name=excluded.display_name, username=excluded.username",
            (chat_id, member.id, member.full_name or "عضو", member.username or "")
        )
        db.commit()
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
    """Record new members and send welcome independently of owner notifications.

    A failure to DM the owner must never prevent the group welcome from being sent.
    If no custom welcome is configured, send a safe default so the welcome feature
    does not appear to do nothing after selecting a group in the panel.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.new_chat_members:
        return

    try:
        register_group(chat)
        if not external_group_active(chat.id):
            return
        settings = get_settings(chat.id)
    except Exception:
        log.exception("WELCOME_SETUP_FAILED chat_id=%s", getattr(chat, "id", None))
        settings = {"entry": 0, "verify_new_members": 0}

    try:
        welcome_row = db.execute(
            "SELECT message FROM welcome_messages WHERE chat_id=?", (chat.id,)
        ).fetchone()
        configured_welcome = welcome_row[0].strip() if welcome_row and welcome_row[0] else ""
        if not external_group_cap_enabled(chat.id, "welcome"):
            configured_welcome = ""
    except Exception:
        log.exception("WELCOME_CONFIG_READ_FAILED chat_id=%s", chat.id)
        configured_welcome = ""

    group_title = chat.title or "القروب"
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        if group_feature_enabled(chat.id, "join_approval_enabled"):
            approval_row = db.execute("SELECT status FROM join_request_events WHERE chat_id=? AND user_id=?", (chat.id, member.id)).fetchone()
            if not approval_row or approval_row[0] not in ("approved", "joined"):
                # The chat_member handler enforces the ban; this avoids welcoming an unapproved join.
                continue
        if global_ban_applies(member.id, chat.id):
            try:
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=member.id)
                log_action(chat.id, member.id, "global_ban_reentry", "منع عودة عضو محظور جماعيًا")
            except Exception as exc:
                log.warning("Global ban enforcement failed chat=%s user=%s: %s", chat.id, member.id, exc)
            continue

        display_name = member.full_name or "عضو"
        try:
            db.execute(
                "INSERT INTO member_activity(chat_id,user_id,display_name,message_count,stars,custom_title,username) "
                "VALUES(?,?,?,0,0,'',?) ON CONFLICT(chat_id,user_id) DO UPDATE SET "
                "display_name=excluded.display_name, username=excluded.username",
                (chat.id, member.id, display_name, member.username or "")
            )
            db.commit()
        except Exception:
            log.exception("MEMBER_ACTIVITY_SAVE_FAILED chat_id=%s member_id=%s", chat.id, member.id)

        # Send the welcome first: unrelated owner-DM issues must not block it.
        if configured_welcome:
            welcome_text = configured_welcome.replace("{name}", display_name).replace("{group}", group_title)
            try:
                await context.bot.send_message(chat_id=chat.id, text=welcome_text[:4000])
                log.info("WELCOME_SENT chat_id=%s member_id=%s custom=1", chat.id, member.id)
            except Exception as exc:
                log.warning(
                    "WELCOME_SEND_FAILED chat_id=%s member_id=%s bot_must_be_able_to_send_messages error=%r",
                    chat.id, member.id, exc
                )
                try:
                    await notify_owners(
                        context,
                        f"⚠️ تعذر إرسال الترحيب\nالقروب: {group_title} ({chat.id})\n"
                        f"العضو: {display_name} (ID: {member.id})\n"
                        "تحقق من أن البوت لم يُحظر وأن لديه صلاحية إرسال الرسائل.",
                        "security"
                    )
                except Exception:
                    log.exception("WELCOME_FAILURE_OWNER_ALERT_FAILED chat_id=%s", chat.id)
        elif external_group_cap_enabled(chat.id, "welcome"):
            default_welcome = (
                f"👋 أهلًا وسهلًا {html.escape(display_name)} في {html.escape(group_title)}!\n"
                "نتمنى لك تجربة موفقة. يرجى الالتزام بقوانين القروب واحترام الجميع. 💙"
            )
            try:
                await context.bot.send_message(chat_id=chat.id, text=default_welcome, parse_mode="HTML")
                log.info("DEFAULT_WELCOME_SENT chat_id=%s member_id=%s", chat.id, member.id)
            except Exception as exc:
                log.warning("DEFAULT_WELCOME_SEND_FAILED chat_id=%s member_id=%s: %s", chat.id, member.id, exc)
        else:
            log.info("WELCOME_DISABLED chat_id=%s member_id=%s", chat.id, member.id)

        try:
            await notify_owners(
                context,
                f"👋 دخول عضو جديد\nالمجموعة: {group_title}\nالعضو: {display_name}\nالمعرّف: {member.id}",
                "membership"
            )
        except Exception:
            log.exception("NEW_MEMBER_OWNER_ALERT_FAILED chat_id=%s member_id=%s", chat.id, member.id)

        if settings.get("verify_new_members", 0):
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat.id, user_id=member.id,
                    permissions=ChatPermissions(
                        can_send_messages=False, can_send_audios=False,
                        can_send_documents=False, can_send_photos=False,
                        can_send_videos=False, can_send_video_notes=False,
                        can_send_voice_notes=False, can_send_polls=False,
                        can_send_other_messages=False, can_add_web_page_previews=False
                    )
                )
                db.execute(
                    "INSERT OR REPLACE INTO pending_verifications(chat_id,user_id,created_at) VALUES(?,?,?)",
                    (chat.id, member.id, now())
                )
                db.commit()
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"👤 أهلًا {display_name}! أكمل التحقق خلال دقيقتين للمشاركة في القروب.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ أنا إنسان — تحقق", callback_data=f"verify_member:{chat.id}:{member.id}")
                    ]])
                )
                context.application.create_task(verification_timeout(context.bot, chat.id, member.id))
            except Exception as exc:
                try:
                    db.execute("DELETE FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat.id, member.id))
                    db.commit()
                except Exception:
                    log.exception("VERIFY_STATE_CLEANUP_FAILED chat_id=%s member_id=%s", chat.id, member.id)
                log.warning("تعذر بدء التحقق للعضو %s في %s: %s", member.id, chat.id, exc)
                try:
                    await notify_owners(
                        context,
                        f"⚠️ تعذر بدء التحقق من عضو جديد\nالقروب: {group_title} ({chat.id})\n"
                        f"العضو: {display_name} (ID: {member.id})\nتحقق من صلاحيات البوت كمشرف.",
                        "security"
                    )
                except Exception:
                    log.exception("VERIFY_FAILURE_OWNER_ALERT_FAILED chat_id=%s", chat.id)

    if settings.get("entry", 0):
        try:
            await msg.delete()
        except Exception as exc:
            log.info("ENTRY_SERVICE_DELETE_FAILED chat_id=%s error=%r", chat.id, exc)

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
    if context.user_data.get("awaiting_staff_permissions_id"):
        # Do not consume a mistyped command as a numeric user ID. Clear this
        # pending state and tell the owner how to run the command correctly.
        raw_probe = (msg.text or "").strip().casefold().lstrip("/").rstrip("/").strip()
        if raw_probe == "aigroups" or raw_probe.startswith("aigroups ") or raw_probe == "ai groups":
            context.user_data.pop("awaiting_staff_permissions_id", None)
            await msg.reply_text("ℹ️ ألغيت انتظار رقم المستخدم حتى لا يتعارض مع الأمر. لإدارة الذكاء الاصطناعي أرسل الأمر كما هو مع الشرطة المائلة في البداية:\n/aigroups\nثم اختر أحد الأمثلة:\n/aigroups list\n/aigroups all on\n/aigroups all off")
            return
        if not (is_owner(user.id) or is_delegated_owner(user.id)):
            context.user_data.pop("awaiting_staff_permissions_id", None)
            await msg.reply_text("⛔ إدارة صلاحيات الطاقم للمالك أو المالك المفوّض فقط.")
            return
        raw_id = (msg.text or "").strip()
        # Avoid trapping the owner in a stale numeric-ID prompt: command-like input cancels the prompt.
        normalized_command = raw_id.casefold().lstrip("/").rstrip("/").strip()
        if raw_id.startswith("/") or normalized_command.startswith(("aigroups", "panel", "help", "start", "cancel", "ai ")):
            context.user_data.pop("awaiting_staff_permissions_id", None)
            await msg.reply_text("ℹ️ ألغيت انتظار رقم المستخدم. أعد إرسال الأمر المطلوب الآن، مثل /aigroups list أو /aigroups all off.")
            return
        if not raw_id.isdigit() or int(raw_id) <= 0:
            context.user_data.pop("awaiting_staff_permissions_id", None)
            await msg.reply_text("❌ أُلغيت عملية إدخال رقم المستخدم لأن المدخل غير رقمي. أعد الأمر المطلوب من جديد، أو استخدم /cancel لإلغاء العمليات.")
            return
        target_uid = int(raw_id)
        if target_uid == OWNER_ID or is_delegated_owner(target_uid):
            await msg.reply_text("⛔ لا يمكن تعديل صلاحيات المالك الأساسي أو المالك المفوّض من هذه اللوحة.")
            context.user_data.pop("awaiting_staff_permissions_id", None)
            return
        context.user_data.pop("awaiting_staff_permissions_id", None)
        await msg.reply_text(
            f"👥 إدارة المشرفين والمدراء والصلاحيات\n\nالمستخدم: {user_display_name(target_uid)}\nالمعرّف: {target_uid}\n\nاختر الرتبة أو الصلاحيات المطلوبة. الصلاحيات ذات علامة ✅ مفعّلة لجميع القروبات المسجلة.",
            reply_markup=staff_permissions_markup(target_uid)
        )
        return
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 إدارة المالك المفوّض", callback_data="delegated_owner")]])
        )
        return
    # Handle an active broadcast draft BEFORE pending admin actions. Otherwise a
    # stale awaiting_admin_action flag can intercept the ad and demand a numeric ID.
    if context.user_data.get("awaiting_broadcast") and (is_owner(user.id) or is_delegated_owner(user.id)):
        context.user_data.pop("awaiting_admin_action", None)
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
        permission_for_action = {"ban":"ban", "unban":"unban", "kick":"kick", "mute":"mute", "unmute":"unmute", "delete":"delete", "pin":"pin"}.get(action)
        if not await can_access_group(context.bot, user.id, chat_id) or not permission_for_action or not has_group_permission(chat_id, user.id, permission_for_action):
            context.user_data.pop("awaiting_admin_action", None)
            await msg.reply_text("⛔ لا تملك الصلاحية المطلوبة لهذا الإجراء في هذا القروب.")
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
                db.execute("INSERT OR REPLACE INTO group_mutes(chat_id,user_id,muted_by,muted_at) VALUES(?,?,?,?)", (chat_id, target_id, user.id, now())); db.commit()
            elif action == "unmute":
                await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
                db.execute("DELETE FROM group_mutes WHERE chat_id=? AND user_id=?", (chat_id, target_id)); db.commit()
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

async def _render_trigger_latest_deploy():
    """Trigger a Render deploy of the latest commit from the service's configured repository."""
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        raise RuntimeError("يلزم ضبط RENDER_API_KEY وRENDER_SERVICE_ID في متغيرات البيئة.")
    import urllib.request
    import urllib.error

    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {RENDER_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def _request():
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"رفض Render الطلب (HTTP {exc.code}): {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"تعذر الاتصال بخدمة Render: {exc}") from exc

    status, body = await asyncio.to_thread(_request)
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}
    deploy_id = payload.get("id") or payload.get("deploy", {}).get("id") or ""
    return status, deploy_id


def update_manager_markup():
    configured = bool(RENDER_API_KEY and RENDER_SERVICE_ID)
    rows = []
    if configured:
        rows.append([InlineKeyboardButton("🚀 نشر آخر تحديث من GitHub", callback_data="update_deploy_latest")])
    rows.append([InlineKeyboardButton("🔄 تحديث الحالة", callback_data="update_manager")])
    rows.append([InlineKeyboardButton("⬅️ العودة للرئيسية", callback_data="home")])
    return InlineKeyboardMarkup(rows)


_ACTIVE_CALLBACK_IDS: set[str] = set()
_ANSWERED_CALLBACK_IDS: set[str] = set()


async def safe_callback_answer(q, *args, **kwargs):
    """Answer a callback query once, preserving branch-specific alerts."""
    query_id = str(getattr(q, "id", ""))
    try:
        await q.answer(*args, **kwargs)
    except BadRequest as exc:
        log.debug("Callback query already answered or no longer valid: %s", exc)
    finally:
        # Track only queries managed by callback(); calls elsewhere do not leak IDs.
        if query_id and query_id in _ACTIVE_CALLBACK_IDS:
            _ANSWERED_CALLBACK_IDS.add(query_id)


async def _monitor_group_card(bot, chat_id):
    """Build a live monitoring card from Telegram API and locally observed activity."""
    row = db.execute("SELECT title,added_at FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        return None
    saved_title, added_at = row
    title = saved_title or str(chat_id)
    live_title = title
    member_count_text = "غير متاح"
    bot_state = "تعذر التحقق"
    bot_permissions = "غير متاحة"
    try:
        chat = await bot.get_chat(chat_id)
        live_title = getattr(chat, "title", None) or title
        try:
            count = await bot.get_chat_member_count(chat_id)
            member_count_text = f"{count:,}"
        except Exception as exc:
            log.info("تعذر جلب عدد أعضاء القروب %s: %s", chat_id, exc)
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat_id, me.id)
            status = getattr(member, "status", "unknown")
            bot_state = "موجود ومشرف" if status == "administrator" else ("موجود كمالك" if status == "creator" else ("موجود" if status not in ("left", "kicked") else "غير موجود/محظور"))
            if status in ("administrator", "creator"):
                perms = []
                if getattr(member, "can_delete_messages", False): perms.append("حذف")
                if getattr(member, "can_restrict_members", False): perms.append("تقييد/كتم")
                if getattr(member, "can_pin_messages", False): perms.append("تثبيت")
                if getattr(member, "can_invite_users", False): perms.append("دعوات")
                bot_permissions = "، ".join(perms) or "صلاحيات إدارية محدودة/غير ظاهرة"
            else:
                bot_permissions = "ليس مشرفًا؛ قد تتعطل أوامر الإدارة"
        except Exception as exc:
            log.info("تعذر التحقق من حالة البوت في القروب %s: %s", chat_id, exc)
    except Exception as exc:
        log.info("تعذر فتح بيانات القروب %s: %s", chat_id, exc)
        bot_state = "تعذر الوصول عبر تيليجرام"
    archive_on = bool(db.execute("SELECT 1 FROM archive_settings WHERE chat_id=? AND enabled=1", (chat_id,)).fetchone())
    total_messages = db.execute("SELECT COALESCE(SUM(message_count),0) FROM weekly_message_activity WHERE chat_id=?", (chat_id,)).fetchone()[0]
    known_members = db.execute("SELECT COUNT(*) FROM member_activity WHERE chat_id=?", (chat_id,)).fetchone()[0]
    week = _week_start()
    week_row = db.execute("SELECT COALESCE(SUM(message_count),0),COUNT(*) FROM weekly_message_activity WHERE chat_id=? AND week_start=?", (chat_id, week)).fetchone()
    week_messages, active_members = week_row or (0, 0)
    incidents = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=? AND action IN ('ban','kick','mute','unmute','delete','anti_flood','delete_edit')", (chat_id,)).fetchone()[0]
    return (live_title, member_count_text, bot_state, bot_permissions, added_at or "غير معروف", archive_on,
            int(total_messages or 0), int(known_members or 0), int(week_messages or 0), int(active_members or 0), int(incidents or 0))

async def _show_monitor_group(q, context, chat_id):
    if not is_owner(q.from_user.id):
        await safe_callback_answer(q, "متابعة القروبات الكاملة للمالك الأساسي فقط.", show_alert=True)
        return
    card = await _monitor_group_card(context.bot, chat_id)
    if not card:
        await q.edit_message_text("القروب غير موجود في قائمة القروبات المسجلة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القروبات", callback_data="monitor_groups")]]))
        return
    title, count, bot_state, perms, added, archived, all_messages, known, week_messages, active, incidents = card
    archive_label = "⏸️ إيقاف أرشفة الرسائل" if archived else "🗃️ تفعيل أرشفة الرسائل الجديدة"
    text = (
        f"👁️ بطاقة متابعة القروب\n\n👥 القروب: {title}\n🆔 المعرف: {chat_id}\n"
        f"👤 عدد الأعضاء الحالي: {count}\n🤖 حالة البوت: {bot_state}\n🔐 صلاحيات البوت: {perms}\n"
        f"🗓️ تاريخ التسجيل: {added}\n\n📊 بيانات الرصد المحلي\n"
        f"💬 إجمالي الرسائل المرصودة: {all_messages:,}\n📅 رسائل الأسبوع الحالي: {week_messages:,}\n"
        f"🗣️ المتفاعلون هذا الأسبوع: {active:,}\n👥 أعضاء ظهروا في الرصد: {known:,}\n"
        f"🛡️ إجراءات إشراف مسجلة: {incidents:,}\n📨 أرشفة الرسائل الجديدة: {'مفعّلة' if archived else 'متوقفة'}\n\n"
        "مهم: عدد الأعضاء يُجلب مباشرة من تيليجرام عند فتح البطاقة، أما الرسائل والنشاط والأعضاء المعروفون فهي بيانات رصد منذ تشغيل البوت فقط وليست سجلًا تاريخيًا كاملًا."
    )
    buttons = [
        [InlineKeyboardButton("🔄 تحديث البيانات", callback_data=f"monitor_group:{chat_id}")],
        [InlineKeyboardButton("📨 عرض آخر الرسائل المسجلة", callback_data=f"monitor_archive:{chat_id}")],
        [InlineKeyboardButton(archive_label, callback_data=f"monitor_archive_toggle:{chat_id}")],
        [InlineKeyboardButton("📋 سجل عمليات القروب", callback_data=f"monitor_logs:{chat_id}")],
        [InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="monitor_groups"), InlineKeyboardButton("🏠 الرئيسية", callback_data="home")],
    ]
    await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(buttons))


MOTIVATION_FALLBACKS = [
    ("معلومة تحفيزية", "💡 معلومة تحفيزية\nالعادات الصغيرة التي تكررها باستمرار غالبًا تصنع فرقًا أكبر من الجهد الكبير المتقطع. اختر خطوة واحدة مفيدة اليوم وداوم عليها."),
    ("حكمة", "✨ حكمة اليوم\nلا تقِس تقدمك بسرعة الآخرين؛ قِسْه بما تعلمته وما أنجزته مقارنة بالأمس."),
    ("مثل", "🌿 مثل عربي: «من جدّ وجد»\nالمعنى: الاجتهاد والمثابرة من أسباب الوصول إلى الهدف، مع التخطيط والاستمرار."),
    ("معلومة تحفيزية", "💡 تذكير اليوم\nتقسيم الهدف الكبير إلى خطوات صغيرة يجعل البدء أسهل، ويساعدك على متابعة تقدمك بوضوح.\n🎯 تحدي اليوم: خصص 15 دقيقة لمهمة كنت تؤجلها."),
]


def motivation_groups_markup():
    groups = db.execute("SELECT chat_id, COALESCE(title, CAST(chat_id AS TEXT)) FROM watched_groups ORDER BY title, chat_id").fetchall()
    selected = {int(r[0]) for r in db.execute("SELECT chat_id FROM motivation_groups WHERE enabled=1").fetchall()}
    rows = []
    for chat_id, title in groups[:45]:
        mark = "✅" if int(chat_id) in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {str(title or chat_id)[:42]}", callback_data=f"mot_group_toggle:{int(chat_id)}")])
    if not rows:
        rows.append([InlineKeyboardButton("لا توجد قروبات مسجلة لدى البوت", callback_data="noop")])
    rows.append([InlineKeyboardButton("✅ تحديد جميع القروبات", callback_data="mot_groups_all"), InlineKeyboardButton("🧹 إلغاء التحديد", callback_data="mot_groups_none")])
    rows.append([InlineKeyboardButton(f"📌 القروبات المحددة: {len(selected)}", callback_data="mot_groups_refresh")])
    rows.append([InlineKeyboardButton("⬅️ رجوع للمحتوى التحفيزي", callback_data="motivation_home")])
    return InlineKeyboardMarkup(rows)


def motivation_draft_markup(draft_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اعتماد ونشر", callback_data=f"mot_draft:{draft_id}:approve"), InlineKeyboardButton("✏️ تعديل النص", callback_data=f"mot_draft:{draft_id}:edit")],
        [InlineKeyboardButton("🔄 محتوى بديل", callback_data=f"mot_draft:{draft_id}:alt"), InlineKeyboardButton("❌ رفض", callback_data=f"mot_draft:{draft_id}:reject")],
        [InlineKeyboardButton("👥 تحديد القروبات", callback_data="mot_groups_refresh")],
    ])


def motivation_admin_markup():
    selected = db.execute("SELECT COUNT(*) FROM motivation_groups WHERE enabled=1").fetchone()[0]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👥 تحديد قروبات النشر ({selected})", callback_data="mot_groups_refresh")],
        [InlineKeyboardButton("📨 عرض مسودة اليوم", callback_data="motivation_show_draft")],
        [InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")],
    ])


async def generate_motivation_content():
    prompt = (
        "أنشئ رسالة عربية قصيرة واحدة فقط لقروبات تيليجرام، اختر نوعًا واحدًا بالتناوب أو عشوائيًا من: "
        "معلومة تحفيزية، حكمة، مثل عربي/شعبي. اجعلها نافعة ومحترمة ومناسبة للطلاب والعامة. "
        "إن كان مثلًا فاذكر المثل ومعناه باختصار، ولا تنسب قولًا لشخص دون تحقق. تجنب الادعاءات العلمية غير المؤكدة، "
        "ولا تضف مقدمة عن أنك ذكاء اصطناعي. اجعل الناتج بين 40 و100 كلمة، وبصيغة جاهزة للنشر."
    )
    recent = [str(r[0]).strip() for r in db.execute(
        "SELECT content FROM motivation_drafts ORDER BY id DESC LIMIT 14"
    ).fetchall() if r and r[0]]
    if recent:
        prompt += "\n\nلا تكرر أي رسالة من هذه الرسائل السابقة، وأنشئ زاوية جديدة مختلفة في الفكرة والصياغة:\n" + "\n---\n".join(recent[:10])[:5000]
    result = await openai_assistant_response(prompt, "إعداد المحتوى التحفيزي اليومي")
    if result and not result.startswith("⚠️"):
        text = result.strip()[:3500]
        if not any(text.casefold() == old.casefold() for old in recent):
            kind = "حكمة" if "حكمة" in text[:80] else ("مثل" if "مثل" in text[:100] else "معلومة تحفيزية")
            return kind, text
    recent_set = {x.casefold() for x in recent}
    choices = [item for item in MOTIVATION_FALLBACKS if item[1].casefold() not in recent_set]
    return random.choice(choices or MOTIVATION_FALLBACKS)


async def show_motivation_draft(target, draft_id=None):
    if draft_id is None:
        row = db.execute("SELECT id,content_date,content_type,content,status FROM motivation_drafts ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = db.execute("SELECT id,content_date,content_type,content,status FROM motivation_drafts WHERE id=?", (draft_id,)).fetchone()
    if not row:
        await target.reply_text("لا توجد مسودة محفوظة حاليًا. سيجهز البوت المحتوى اليومي الساعة 7:00 صباحًا بتوقيت السعودية.")
        return
    did, content_date, content_type, content, status = row
    state = {"pending":"بانتظار موافقتك", "approved":"تم اعتماد المحتوى", "rejected":"مرفوض"}.get(status, status)
    text = f"🌟 المحتوى التحفيزي اليومي\n📅 التاريخ: {content_date}\n🏷️ النوع: {content_type}\n📌 الحالة: {state}\n\n{content}"
    await target.reply_text(text, reply_markup=motivation_draft_markup(did) if status == "pending" else motivation_admin_markup())


async def motivation_cmd(update, context):
    user = update.effective_user
    if not user or not is_owner(user.id):
        await update.effective_message.reply_text("⛔ ميزة المحتوى اليومي وإدارة القروبات للمالك الأساسي فقط.")
        return
    await update.effective_message.reply_text(
        "🌟 إدارة المحتوى التحفيزي اليومي\n\nيعرض الذكاء الاصطناعي مسودة يومية الساعة 7:00 صباحًا بتوقيت السعودية. لن يتم النشر قبل موافقتك. اختر القروبات التي تريد نشر المحتوى فيها.",
        reply_markup=motivation_admin_markup()
    )
    await show_motivation_draft(update.effective_message)


async def evening_adhkar_worker(app):
    """Send one evening-adhkar post per enabled group/date, retrying failed groups every 10 minutes."""
    tz = ZoneInfo("Asia/Riyadh")
    while True:
        try:
            local_now = datetime.now(tz)
            today = local_now.date().isoformat()
            # Evening window allows retries after transient API failures or process restarts.
            in_window = (local_now.hour, local_now.minute) >= (18, 0) and (local_now.hour, local_now.minute) < (23, 30)
            if in_window:
                targets = db.execute(
                    "SELECT w.chat_id, COALESCE(NULLIF(w.title,''), CAST(w.chat_id AS TEXT)) "
                    "FROM watched_groups w JOIN adhkar_group_settings s ON s.chat_id=w.chat_id "
                    "WHERE s.enabled=1 ORDER BY w.title,w.chat_id"
                ).fetchall()
                for chat_id, title in targets:
                    try:
                        sent = db.execute("SELECT status,attempted_at FROM adhkar_posts_sent WHERE chat_id=? AND post_date=?", (int(chat_id), today)).fetchone()
                        if sent and sent[0] in ("sent", "sending", "unknown"):
                            # At-most-once safety: a stale "sending" or ambiguous network result
                            # may mean Telegram accepted the post before the process lost its
                            # response. Do not resend to the same group on the same date.
                            continue
                        # Retry only definitive failures, no more often than every 10 minutes.
                        if sent and sent[0] == "failed" and sent[1]:
                            try:
                                last_attempt = datetime.fromisoformat(sent[1])
                                if last_attempt.tzinfo is None:
                                    last_attempt = last_attempt.replace(tzinfo=tz)
                                if (local_now - last_attempt).total_seconds() < 600:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        attempt_stamp = local_now.isoformat(timespec="seconds")
                        db.execute(
                            "INSERT INTO adhkar_posts_sent(chat_id,post_date,status,attempted_at,sent_at,error) VALUES(?,?, 'sending', ?, '', '') "
                            "ON CONFLICT(chat_id,post_date) DO UPDATE SET status='sending',attempted_at=excluded.attempted_at,error=''",
                            (int(chat_id), today, attempt_stamp))
                        db.commit()
                        try:
                            await app.bot.send_message(chat_id=int(chat_id), text=format_evening_adhkar_message(), parse_mode="HTML", disable_web_page_preview=True)
                            db.execute("UPDATE adhkar_posts_sent SET status='sent',sent_at=?,error='' WHERE chat_id=? AND post_date=?", (now(), int(chat_id), today))
                            db.commit()
                            log_action(int(chat_id), 0, "evening_adhkar_sent", f"date={today}; group={title}")
                        except asyncio.CancelledError:
                            raise
                        except Exception as send_exc:
                            # Network timeouts/read errors are ambiguous: Telegram may have
                            # accepted the message even if the client never received its reply.
                            # Mark these as unknown and do not retry today to avoid duplicates.
                            ambiguous = type(send_exc).__name__ in {
                                "NetworkError", "TimedOut", "ReadTimeout", "ConnectTimeout",
                                "WriteTimeout", "RemoteProtocolError",
                            }
                            status = "unknown" if ambiguous else "failed"
                            db.execute("UPDATE adhkar_posts_sent SET status=?,error=? WHERE chat_id=? AND post_date=?", (status, str(send_exc)[:500], int(chat_id), today))
                            db.commit()
                            log.warning("تعذر تأكيد إرسال أذكار المساء للقروب %s (status=%s): %s", chat_id, status, send_exc)
                        await asyncio.sleep(0.15)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("خطأ أثناء إرسال أذكار المساء للقروب %s", chat_id)
            # Poll more frequently in the evening so failures can be retried; otherwise sleep until 18:00.
            local_now = datetime.now(tz)
            if (local_now.hour, local_now.minute) < (18, 0):
                next_run = local_now.replace(hour=18, minute=0, second=0, microsecond=0)
                await asyncio.sleep(max(1, (next_run - local_now).total_seconds()))
            elif (local_now.hour, local_now.minute) >= (23, 30):
                next_run = (local_now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
                await asyncio.sleep(max(1, (next_run - local_now).total_seconds()))
            else:
                await asyncio.sleep(600)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("تعذر تنفيذ جدولة أذكار المساء")
            await asyncio.sleep(60)


async def bot_promo_worker(app):
    """Post a short service announcement to registered groups at 10:00 and 20:00 Riyadh time."""
    tz = ZoneInfo("Asia/Riyadh")
    promo = (
        "🤖 بوت الحماية والمساعدة — خدمة مجانية لقروبات تيليجرام\n\n"
        "🛡️ أدوات حماية وإشراف وتنظيم، مع صلاحيات منفصلة للمالك والمفوّض والمشرفين.\n"
        "✨ لإضافة البوت: https://t.me/khaled211_bot ثم ارفعه مشرفًا وفعّله من داخل القروب."
    )
    while True:
        try:
            local_now = datetime.now(tz)
            slot = "10" if local_now.hour == 10 else ("20" if local_now.hour == 20 else "")
            # A short window avoids sending late ads after an extended outage/restart.
            if slot and local_now.minute < 8:
                local_date = local_now.date().isoformat()
                groups = db.execute(
                    "SELECT chat_id FROM watched_groups ORDER BY chat_id"
                ).fetchall()
                for (chat_id_raw,) in groups:
                    chat_id = int(chat_id_raw)
                    cur = db.execute(
                        "INSERT OR IGNORE INTO bot_promo_delivery(chat_id,local_date,slot,status,attempted_at) VALUES(?,?,?,'sending',?)",
                        (chat_id, local_date, slot, now()),
                    )
                    db.commit()
                    if cur.rowcount != 1:
                        continue
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=promo, disable_web_page_preview=True)
                        db.execute(
                            "UPDATE bot_promo_delivery SET status='sent',sent_at=?,error='' WHERE chat_id=? AND local_date=? AND slot=?",
                            (now(), chat_id, local_date, slot),
                        )
                        db.commit()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        db.execute(
                            "UPDATE bot_promo_delivery SET status='failed',error=? WHERE chat_id=? AND local_date=? AND slot=?",
                            (str(exc)[:500], chat_id, local_date, slot),
                        )
                        db.commit()
                        log.warning("Promo announcement failed for group %s: %s", chat_id, exc)
                    await asyncio.sleep(0.12)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error in bot promo worker")
            await asyncio.sleep(60)


async def motivation_daily_worker(app):
    """Prepare and send one daily draft to the primary owner at 07:00 Asia/Riyadh."""
    tz = ZoneInfo("Asia/Riyadh")
    while True:
        try:
            current = datetime.now(tz)
            next_run = current.replace(hour=7, minute=0, second=0, microsecond=0)
            if next_run <= current:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(1, (next_run - current).total_seconds()))
            local_day = datetime.now(tz).date().isoformat()
            exists = db.execute("SELECT id FROM motivation_drafts WHERE content_date=?", (local_day,)).fetchone()
            if exists:
                continue
            kind, content = await generate_motivation_content()
            db.execute("INSERT OR IGNORE INTO motivation_drafts(content_date,content_type,content,status,created_at) VALUES(?,?,?,?,?)",
                       (local_day, kind, content, "pending", now()))
            db.commit()
            row = db.execute("SELECT id,content_type,content,status FROM motivation_drafts WHERE content_date=?", (local_day,)).fetchone()
            if not row or not OWNER_ID:
                continue
            did, kind, content, status = row
            if status == "pending":
                text = f"🌅 محتوى اليوم للمراجعة — {local_day}\n🏷️ النوع: {kind}\n\n{content}\n\nهل تعتمد هذا المحتوى للنشر في القروبات المحددة؟"
                try:
                    await app.bot.send_message(chat_id=OWNER_ID, text=text, reply_markup=motivation_draft_markup(did))
                except Exception as exc:
                    log.warning("تعذر إرسال مسودة التحفيز للمالك؛ يمكن استعادتها عبر /motivation: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("تعذر إعداد المحتوى التحفيزي اليومي")
            await asyncio.sleep(60)


async def _callback_impl(update, context):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    if data == "activation_help":
        await safe_callback_answer(q)
        await q.message.reply_text(
            "🛡️ <b>طريقة تفعيل بوت الحماية</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ أضف @khaled211_bot إلى القروب.\n"
            "2️⃣ ارفعه إلى مشرف وامنحه صلاحيات الحذف وإدارة الرسائل التي يحتاجها.\n"
            "3️⃣ أرسل داخل القروب الأمر التالي كما هو:\n"
            "<code>تفعيل @khaled211_bot</code>\n\n"
            "✅ يجب أن يكون مرسل الأمر مالك القروب أو مشرفًا فعليًا.\n"
            "🔐 بعد التحقق من الصلاحيات، يسجل البوت القروب ويفعّل الحماية الأساسية.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 فتح البوت", url="https://t.me/khaled211_bot")]]),
            disable_web_page_preview=True,
        )
        return

    if data == "bulk_delete_help":
        chat_id = q.message.chat_id if q.message else 0
        if not chat_id or not has_group_permission(chat_id, uid, "delete"):
            await safe_callback_answer(q, "لا تملك صلاحية مسح الرسائل في هذا القروب.", show_alert=True); return
        await safe_callback_answer(q, "اكتب الأمر متبوعًا بعدد من 1 إلى 500.", show_alert=True)
        await q.message.reply_text("🗑️ مسح الرسائل\nاكتب العدد المطلوب من الرسائل التي رصدها البوت، مثال:\nمسح رسائل 500\nالحد المسموح من 1 إلى 500. يعتمد الحذف على صلاحيات البوت والرسائل التي رصدها منذ تشغيله، وقد يرفض تيليجرام حذف الرسائل الأقدم من 48 ساعة.")
        return

    if data == "joinapproval_global_toggle":
        db.execute("UPDATE global_join_approval_settings SET enabled=0,updated_at=?,updated_by=? WHERE id=1", (now(), uid))
        db.execute("UPDATE group_feature_settings SET join_approval_enabled=0,updated_at=? WHERE join_approval_enabled<>0", (now(),))
        db.commit()
        await safe_callback_answer(q, "تم إلغاء قفل الدخول المركزي؛ الدخول المباشر متاح.", show_alert=True)
        return
    if data == "group_controls":
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "join_approval", 0)):
            await safe_callback_answer(q, "هذه الإعدادات للمالك أو للمفوّض/المستخدم المصرح له.", show_alert=True)
            return
        await q.edit_message_text(
            "🔔 إعدادات إشعارات دخول وخروج الأعضاء فقط. تم حذف الإحصائيات والتحليل وقفل الدخول.",
            reply_markup=group_controls_list_markup(),
        )
        return

    if data.startswith("gctrl_group:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True)
            return
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "join_approval", chat_id)):
            await safe_callback_answer(q, "لا تملك صلاحية تعديل إشعارات هذا القروب.", show_alert=True)
            return
        row = db.execute("SELECT COALESCE(NULLIF(title,''),CAST(chat_id AS TEXT)) FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        await q.edit_message_text(
            f"🔔 إشعارات القروب: {row[0] if row else chat_id}\nاختر إشعارات الدخول أو الخروج لتفعيلها أو إيقافها.",
            reply_markup=group_controls_markup(chat_id),
        )
        return

    if data.startswith("gctrl_toggle:"):
        try:
            _, chat_raw, feature = data.split(":", 2)
            chat_id = int(chat_raw)
        except ValueError:
            await safe_callback_answer(q, "بيانات الإعداد غير صحيحة.", show_alert=True)
            return
        if feature not in {"join_notices_enabled", "leave_notices_enabled"}:
            await safe_callback_answer(q, "هذا الإعداد لم يعد متاحًا.", show_alert=True)
            return
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "join_approval", chat_id)):
            await safe_callback_answer(q, "لا تملك صلاحية تعديل إشعارات هذا القروب.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True)
            return
        new_state = not group_feature_enabled(chat_id, feature)
        set_group_feature(chat_id, feature, new_state)
        log_action(chat_id, uid, "membership_notice_toggle", f"{feature}={int(new_state)}")
        await safe_callback_answer(q, "تم تحديث الإشعار.")
        await q.edit_message_text(
            "🔔 تم تحديث إشعارات القروب.",
            reply_markup=group_controls_markup(chat_id),
        )
        return

    if data.startswith("gctrl_link:"):
        await safe_callback_answer(q, "تم إلغاء إنشاء روابط قفل الدخول؛ الدخول المباشر متاح.", show_alert=True)
        return

    if data == "restricted_members":
        if not (is_owner(uid) or capability_group_ids(uid, "reentry") or capability_group_ids(uid, "join_approval")):
            await safe_callback_answer(q, "تحتاج إلى صلاحية مراجعة المطرودين أو طلبات الانضمام.", show_alert=True); return
        await q.edit_message_text("🚫 المحظورون والمرفوضون وطلبات العودة\nاختر قروبًا لمراجعة سجلات الطرد وطلبات الانضمام. يعتمد التعرف على العضو على Telegram ID الثابت.", reply_markup=restricted_groups_markup(uid)); return

    if data.startswith("restricted_group:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "قروب غير صحيح.", show_alert=True); return
        if not (is_owner(uid) or can_use_capability(uid, "reentry", chat_id) or can_use_capability(uid, "join_approval", chat_id)):
            await safe_callback_answer(q, "لا تملك صلاحية مراجعة هذا القروب.", show_alert=True); return
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = title_row[0] if title_row else str(chat_id)
        bans = db.execute("SELECT user_id,display_name,username,reason,created_at,status FROM reentry_restrictions WHERE chat_id=? ORDER BY created_at DESC LIMIT 12", (chat_id,)).fetchall()
        requests = db.execute("SELECT user_id,display_name,username,status,requested_at FROM join_request_events WHERE chat_id=? ORDER BY requested_at DESC LIMIT 12", (chat_id,)).fetchall()
        lines = [f"🚫 سجل القروب: {title or chat_id}", "", "المطرودون/طلبات العودة:"]
        rows = []
        for user_id,name,username,reason,created,status in bans:
            lines.append(f"• {name or user_id} — ID {user_id} — {status} — {reason or 'بدون سبب'}")
            if status in ("pending", "blocked") and (is_owner(uid) or can_use_capability(uid, "reentry", chat_id)):
                rows.append([InlineKeyboardButton(f"✅ مراجعة/السماح {str(name or user_id)[:22]}", callback_data=f"reentry_approve:{chat_id}:{user_id}"), InlineKeyboardButton("⛔ إبقاء المنع", callback_data=f"reentry_keep:{chat_id}:{user_id}")])
        lines += ["", "طلبات الانضمام الأخيرة:"]
        for user_id,name,username,status,requested in requests:
            lines.append(f"• {name or user_id} — ID {user_id} — {status}")
            if status == "pending" and (is_owner(uid) or can_use_capability(uid, "join_approval", chat_id) or can_use_capability(uid, "reentry", chat_id)):
                rows.append([InlineKeyboardButton(f"✅ قبول {str(name or user_id)[:20]}", callback_data=f"joinreq_approve:{chat_id}:{user_id}"), InlineKeyboardButton("❌ رفض", callback_data=f"joinreq_deny:{chat_id}:{user_id}")])
        if not bans and not requests: lines.append("لا توجد سجلات مسجلة لهذا القروب حتى الآن.")
        rows.append([InlineKeyboardButton("⬅️ القروبات", callback_data="restricted_members"), InlineKeyboardButton("🏠 الرئيسية", callback_data="home")])
        await q.edit_message_text("\n".join(lines)[:3800], reply_markup=InlineKeyboardMarkup(rows)); return

    if data == "restricted_global":
        if not is_owner(uid):
            await safe_callback_answer(q, "الحظر العام للمالك الأساسي فقط.", show_alert=True); return
        bans = db.execute("SELECT user_id,display_name,username,reason,scope,created_at FROM global_bans WHERE active=1 ORDER BY created_at DESC LIMIT 25").fetchall()
        text = "🌐 الحظر العام النشط\n\n" + ("\n".join(f"• {n or u} — ID {u} — @{un or 'بدون اسم'} — {scope} — {reason or 'بدون سبب'}" for u,n,un,reason,scope,created in bans) if bans else "لا توجد حالات حظر عام نشطة.")
        rows = [[InlineKeyboardButton(f"🔓 فك الحظر: {(name or target_id)} ({target_id})", callback_data=f"massban_unban:{target_id}")] for target_id,name,username,reason,scope,created in bans]
        rows.append([InlineKeyboardButton("⬅️ العودة", callback_data="restricted_members")])
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("joinreq_approve:") or data.startswith("joinreq_deny:"):
        try: action, chat_raw, user_raw = data.split(":", 2); chat_id, target_id = int(chat_raw), int(user_raw)
        except ValueError: await safe_callback_answer(q, "بيانات الطلب غير صحيحة.", show_alert=True); return
        if not (is_owner(uid) or can_use_capability(uid, "join_approval", chat_id) or can_use_capability(uid, "reentry", chat_id)):
            await safe_callback_answer(q, "لا تملك صلاحية الموافقة على هذا الطلب.", show_alert=True); return
        reqrow = db.execute("SELECT status FROM join_request_events WHERE chat_id=? AND user_id=?", (chat_id, target_id)).fetchone()
        if not reqrow or reqrow[0] not in ("pending", "approved"):
            await safe_callback_answer(q, "الطلب لم يعد معلقًا.", show_alert=True); return
        if action == "joinreq_approve":
            restriction = db.execute("SELECT status FROM reentry_restrictions WHERE chat_id=? AND user_id=?", (chat_id, target_id)).fetchone()
            if restriction and restriction[0] in ("pending", "blocked"):
                await safe_callback_answer(q, "هذا عضو مطرود سابقًا. وافق أولًا على عودته من زر مراجعة المطرودين.", show_alert=True); return
            try:
                await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=target_id)
                db.execute("UPDATE join_request_events SET status='approved',decided_at=?,decided_by=? WHERE chat_id=? AND user_id=?", (now(), uid, chat_id, target_id))
                db.commit(); log_action(chat_id, uid, "join_request_approved", f"target={target_id}")
                await q.edit_message_text((q.message.text or "طلب انضمام") + f"\n\n✅ تمت الموافقة بواسطة {uid}.")
            except Exception as exc:
                log.warning("Join request approval failed chat=%s user=%s: %s", chat_id, target_id, exc)
                await safe_callback_answer(q, "تعذرت الموافقة. تحقق من صلاحيات البوت وحالة الطلب.", show_alert=True)
        else:
            try: await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=target_id)
            except Exception as exc: log.info("Decline join request failed chat=%s user=%s: %s", chat_id, target_id, exc)
            db.execute("UPDATE join_request_events SET status='denied',decided_at=?,decided_by=? WHERE chat_id=? AND user_id=?", (now(), uid, chat_id, target_id))
            db.commit(); log_action(chat_id, uid, "join_request_denied", f"target={target_id}")
            await q.edit_message_text((q.message.text or "طلب انضمام") + f"\n\n❌ تم رفض الطلب بواسطة {uid}.")
        return

    if data.startswith("allmsg_"):
        if not (is_owner(uid) or can_use_capability(uid, "message_lock", 0) or capability_group_ids(uid, "message_lock") or context.user_data.get("_lockall_uid") == uid):
            await safe_callback_answer(q, "لا تملك صلاحية قفل وفتح الرسائل.", show_alert=True); return
        if data.startswith("allmsg_open:"):
            action = data.split(":", 1)[1]
            if action not in ("lock", "unlock"):
                await safe_callback_answer(q, "نوع العملية غير صحيح.", show_alert=True); return
            context.user_data["_lockall_uid"] = uid
            context.user_data["all_message_lock_action"] = action
            context.user_data["all_message_lock_selected"] = set()
            await q.edit_message_text("🔒 إدارة قفل الرسائل\n\nاختر القروبات التي تريد " + ("قفل الرسائل فيها" if action == "lock" else "فتح الرسائل فيها") + ". ستطبق العملية على القروبات المحددة فقط.", reply_markup=all_message_groups_markup(context, action))
            await safe_callback_answer(q)
            return
        parts = data.split(":")
        action = parts[-1] if len(parts) > 1 else context.user_data.get("all_message_lock_action", "lock")
        selected = set(context.user_data.get("all_message_lock_selected", set()))
        if data.startswith("allmsg_toggle:"):
            try: gid = int(parts[1])
            except Exception: await safe_callback_answer(q, "قروب غير صحيح.", show_alert=True); return
            if not (is_owner(uid) or can_use_capability(uid, "message_lock", gid)):
                await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
            selected.symmetric_difference_update({gid})
            context.user_data["all_message_lock_selected"] = selected
            await q.edit_message_reply_markup(reply_markup=all_message_groups_markup(context, action))
            await safe_callback_answer(q); return
        if data.startswith("allmsg_all:"):
            ids = [int(r[0]) for r in db.execute("SELECT chat_id FROM watched_groups").fetchall() if is_owner(uid) or can_use_capability(uid,"message_lock",int(r[0]))]
            context.user_data["all_message_lock_selected"] = set(ids)
            await q.edit_message_reply_markup(reply_markup=all_message_groups_markup(context, action)); await safe_callback_answer(q, f"تم تحديد {len(ids)} قروب."); return
        if data.startswith("allmsg_none:"):
            context.user_data["all_message_lock_selected"] = set()
            await q.edit_message_reply_markup(reply_markup=all_message_groups_markup(context, action)); await safe_callback_answer(q, "تم إلغاء التحديد."); return
        if data.startswith("allmsg_apply:"):
            if not selected:
                await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل.", show_alert=True); return
            ok, failed = 0, []
            for gid in sorted(selected):
                if not (is_owner(uid) or can_use_capability(uid, "message_lock", gid)):
                    failed.append((gid,"لا توجد صلاحية")); continue
                try:
                    if action == "lock":
                        await context.bot.set_chat_permissions(chat_id=gid, permissions=ChatPermissions(
                            can_send_messages=False, can_send_audios=False, can_send_documents=False,
                            can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                            can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                            can_add_web_page_previews=False))
                        log_action(gid,uid,"all_messages_locked","bulk selection")
                    else:
                        await context.bot.set_chat_permissions(chat_id=gid, permissions=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True))
                        log_action(gid,uid,"all_messages_unlocked","bulk selection")
                    ok += 1
                except Exception as exc: failed.append((gid,str(exc)[:120]))
            summary = f"تم التنفيذ في {ok} قروب، وتعذر في {len(failed)}."
            if failed: summary += "\n\n" + "\n".join(f"{g}: {e}" for g,e in failed[:10])
            await q.edit_message_text(summary); await safe_callback_answer(q); return

    # Daily motivational content: primary owner only; publishing is always approval-gated.
    if data.startswith("motivation_") or data.startswith("mot_groups_") or data.startswith("mot_group_toggle:") or data.startswith("mot_draft:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذه الميزة للمالك الأساسي فقط.", show_alert=True); return
        if data == "motivation_home":
            await q.message.reply_text("🌟 إدارة المحتوى التحفيزي اليومي\nاختر القروبات المستهدفة، أو اعرض مسودة اليوم.", reply_markup=motivation_admin_markup())
            await safe_callback_answer(q); return
        if data in ("mot_groups_refresh",):
            await q.message.reply_text("👥 اختر القروبات التي تريد نشر المحتوى المعتمد فيها. القروبات غير المحددة لن تستقبل المحتوى.", reply_markup=motivation_groups_markup())
            await safe_callback_answer(q); return
        if data == "mot_groups_all":
            groups = db.execute("SELECT chat_id FROM watched_groups").fetchall()
            for (gid,) in groups:
                db.execute("INSERT INTO motivation_groups(chat_id,enabled,updated_at) VALUES(?,1,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=1,updated_at=excluded.updated_at", (gid, now()))
            db.commit()
            await q.edit_message_reply_markup(reply_markup=motivation_groups_markup())
            await safe_callback_answer(q, f"تم تحديد {len(groups)} قروب."); return
        if data == "mot_groups_none":
            db.execute("UPDATE motivation_groups SET enabled=0,updated_at=?", (now(),)); db.commit()
            await q.edit_message_reply_markup(reply_markup=motivation_groups_markup())
            await safe_callback_answer(q, "تم إلغاء تحديد جميع القروبات."); return
        if data.startswith("mot_group_toggle:"):
            try: gid = int(data.split(":",1)[1])
            except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
            if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (gid,)).fetchone():
                await safe_callback_answer(q, "هذا القروب غير مسجل لدى البوت.", show_alert=True); return
            db.execute("INSERT INTO motivation_groups(chat_id,enabled,updated_at) VALUES(?,1,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=1-enabled,updated_at=excluded.updated_at", (gid, now()))
            db.commit(); await q.edit_message_reply_markup(reply_markup=motivation_groups_markup()); await safe_callback_answer(q); return
        if data == "motivation_show_draft":
            await show_motivation_draft(q.message); await safe_callback_answer(q); return
        if data.startswith("mot_draft:"):
            try: _, raw_id, action = data.split(":",2); did = int(raw_id)
            except ValueError: await safe_callback_answer(q, "بيانات المسودة غير صحيحة.", show_alert=True); return
            draft = db.execute("SELECT content_date,content_type,content,status FROM motivation_drafts WHERE id=?", (did,)).fetchone()
            if not draft: await safe_callback_answer(q, "المسودة غير موجودة.", show_alert=True); return
            content_date, kind, content, status = draft
            if status != "pending": await safe_callback_answer(q, "هذه المسودة لم تعد بانتظار الموافقة.", show_alert=True); return
            if action == "edit":
                context.user_data["awaiting_motivation_edit"] = did
                await q.message.reply_text("✏️ أرسل النص الجديد للمحتوى في رسالة واحدة. لن يُنشر حتى تعتمد النسخة المعدلة. للإلغاء أرسل /cancel.")
                await safe_callback_answer(q); return
            if action == "alt":
                new_kind, new_content = await generate_motivation_content()
                db.execute("UPDATE motivation_drafts SET content_type=?,content=?,created_at=? WHERE id=? AND status='pending'", (new_kind,new_content,now(),did)); db.commit()
                await q.message.reply_text(f"🔄 محتوى بديل للمراجعة — {content_date}\n🏷️ النوع: {new_kind}\n\n{new_content}", reply_markup=motivation_draft_markup(did))
                await safe_callback_answer(q, "تم إعداد محتوى بديل."); return
            if action == "reject":
                db.execute("UPDATE motivation_drafts SET status='rejected',approved_by=?,approved_at=? WHERE id=? AND status='pending'", (uid,now(),did)); db.commit()
                await q.edit_message_text(f"❌ تم رفض محتوى {content_date}. لم يُنشر في أي قروب.")
                await safe_callback_answer(q, "تم الرفض."); return
            if action == "approve":
                groups = [int(r[0]) for r in db.execute("SELECT chat_id FROM motivation_groups WHERE enabled=1 ORDER BY chat_id").fetchall()]
                if not groups:
                    await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل قبل النشر.", show_alert=True)
                    await q.message.reply_text("👥 لا توجد قروبات محددة للنشر. اختر القروبات أولًا:", reply_markup=motivation_groups_markup()); return
                db.execute("UPDATE motivation_drafts SET status='approved',approved_by=?,approved_at=? WHERE id=? AND status='pending'", (uid,now(),did)); db.commit()
                sent, failed = [], []
                for gid in groups:
                    prior = db.execute("SELECT status FROM motivation_posts WHERE draft_id=? AND chat_id=?", (did,gid)).fetchone()
                    if prior and prior[0] == "sent":
                        sent.append(gid); continue
                    try:
                        await context.bot.send_message(chat_id=gid, text=f"🌟 {kind}\n\n{content}\n\n✨ بوت حماية ومساعدة القروبات")
                        db.execute("INSERT INTO motivation_posts(draft_id,chat_id,status,posted_at,error) VALUES(?,?,?,?,?) ON CONFLICT(draft_id,chat_id) DO UPDATE SET status='sent',posted_at=excluded.posted_at,error=''", (did,gid,"sent",now(),"")); db.commit(); sent.append(gid)
                    except Exception as exc:
                        err = str(exc)[:400]
                        db.execute("INSERT INTO motivation_posts(draft_id,chat_id,status,posted_at,error) VALUES(?,?,?,?,?) ON CONFLICT(draft_id,chat_id) DO UPDATE SET status='failed',posted_at=excluded.posted_at,error=excluded.error", (did,gid,"failed",now(),err)); db.commit(); failed.append((gid,err))
                summary = f"✅ تم اعتماد المحتوى.\nنجح النشر في {len(sent)} قروب.\nتعذر النشر في {len(failed)} قروب."
                if failed: summary += "\n\nالقروبات التي تعذر النشر فيها:\n" + "\n".join(f"• {gid}: {err[:100]}" for gid,err in failed[:10])
                await q.edit_message_text(summary, reply_markup=motivation_admin_markup())
                await safe_callback_answer(q, "تمت معالجة النشر."); return
            await safe_callback_answer(q, "الإجراء غير معروف.", show_alert=True); return

    # Primary-owner-only unified permissions and group assignment workflow.
    if data == "unified_permissions":
        if not is_owner(uid):
            await safe_callback_answer(q, "توزيع الصلاحيات الشاملة للمالك الأساسي فقط.", show_alert=True); return
        context.user_data["awaiting_unified_target_id"] = True
        await q.message.reply_text("🛡️ توزيع الصلاحيات والقروبات\nأرسل Telegram ID للمفوّض أو المدير أو المشرف الذي تريد إدارته.\nيمكنك منح أو سحب الرتب، وتحديد جميع القروبات أو قروبات محددة. للإلغاء أرسل /cancel.")
        return
    if data.startswith("unifiedmanage:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: target_uid = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف المستخدم غير صحيح.", show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID: await safe_callback_answer(q, "لا يمكن تعديل حساب المالك الأساسي.", show_alert=True); return
        await q.edit_message_text(f"🛡️ توزيع الصلاحيات والقروبات\nالمستخدم: {user_display_name(target_uid)}\nID: {target_uid}\n\nاختر الرتبة والصلاحيات. المنح والسحب يسجلان في سجل الإدارة.\nملاحظة: صفة المالك المفوّض تمنح صلاحيات تشغيلية واسعة على القروبات المسجلة؛ لتقييد المستخدم بقروبات محددة، استخدم رتبة مدير/مشرف وصلاحيات القروبات المحددة.", reply_markup=unified_permissions_markup(target_uid)); return
    if data.startswith("unifiedrole:"):
        if not is_owner(uid): await safe_callback_answer(q, "منح الرتب العامة للمالك الأساسي فقط.", show_alert=True); return
        try: _, raw_target, role = data.split(":",2); target_uid=int(raw_target)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID or role not in ("delegated","manager","protection"):
            await safe_callback_answer(q, "الرتبة أو المستخدم غير مسموح.", show_alert=True); return
        if role == "delegated":
            exists = db.execute("SELECT 1 FROM delegated_owners WHERE user_id=?", (target_uid,)).fetchone()
            if exists:
                db.execute("DELETE FROM delegated_owners WHERE user_id=?", (target_uid,))
                action = "تم سحب صفة المالك المفوّض"
            else:
                db.execute("INSERT INTO delegated_owners(user_id,added_by,added_at,display_name,username) VALUES(?,?,?,?,?)", (target_uid,uid,now(),user_display_name(target_uid),""))
                action = "تم منح صفة المالك المفوّض"
        else:
            table = "managers" if role == "manager" else "protection_managers"
            exists = db.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (target_uid,)).fetchone()
            if exists:
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (target_uid,)); action = "تم سحب الرتبة"
            else:
                db.execute(f"INSERT OR REPLACE INTO {table}(user_id,added_by,added_at) VALUES(?,?,?)", (target_uid,uid,now())); action = "تم منح الرتبة"
        db.commit(); log_action(0,uid,"unified_role_changed",f"target={target_uid}; role={role}; action={action}")
        await safe_callback_answer(q, action, show_alert=True)
        await q.edit_message_text(f"{action} للمستخدم {target_uid}.", reply_markup=unified_permissions_markup(target_uid)); return
    if data.startswith("unifiedcap:"):
        if not is_owner(uid): await safe_callback_answer(q, "منح الصلاحيات العامة للمالك الأساسي فقط.", show_alert=True); return
        try: _, raw_target, cap = data.split(":",2); target_uid=int(raw_target)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID or cap not in STAFF_CAPABILITY_LABELS:
            await safe_callback_answer(q, "المستخدم أو الصلاحية غير مسموح.", show_alert=True); return
        exists=db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0",(target_uid,cap)).fetchone()
        if exists:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0",(target_uid,cap))
            action="تم سحب الصلاحية من جميع القروبات"
        else:
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?",(target_uid,cap))
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)",(target_uid,cap,0,uid,now()))
            action="تم منح الصلاحية لجميع القروبات"
        db.commit(); log_action(0,uid,"unified_capability_changed",f"target={target_uid}; cap={cap}; action={action}")
        await safe_callback_answer(q,action); await q.edit_message_text(f"{action} للمستخدم {target_uid}.",reply_markup=unified_permissions_markup(target_uid)); return
    if data.startswith("unifiedgroups:"):
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        try: target_uid=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q,"معرّف المستخدم غير صحيح.",show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID: await safe_callback_answer(q,"لا يمكن تعديل حساب المالك الأساسي.",show_alert=True); return
        await q.edit_message_text(f"📍 اختر قروبًا لتحديد صلاحيات المستخدم {target_uid}.",reply_markup=unified_group_list_markup(target_uid)); return
    if data.startswith("unifiedgroup:"):
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        try: _,raw_target,raw_chat=data.split(":",2); target_uid=int(raw_target); chat_id=int(raw_chat)
        except ValueError: await safe_callback_answer(q,"بيانات غير صحيحة.",show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID: await safe_callback_answer(q,"لا يمكن تعديل حساب المالك الأساسي.",show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone(): await safe_callback_answer(q,"القروب غير مسجل.",show_alert=True); return
        title=db.execute("SELECT title FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone()
        await q.edit_message_text(f"صلاحيات {target_uid} في القروب: {title[0] if title else chat_id}",reply_markup=unified_group_markup(target_uid,chat_id)); return
    if data.startswith("unifiedgcap:"):
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        try: _,raw_target,raw_chat,cap=data.split(":",3); target_uid=int(raw_target); chat_id=int(raw_chat)
        except ValueError: await safe_callback_answer(q,"بيانات غير صحيحة.",show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID or cap not in STAFF_CAPABILITY_LABELS or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone(): await safe_callback_answer(q,"القروب أو الصلاحية غير صحيحة.",show_alert=True); return
        explicit=db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)).fetchone()
        global_grant=db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0",(target_uid,cap)).fetchone()
        if global_grant:
            denied=db.execute("SELECT 1 FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)).fetchone()
            if denied:
                db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)); action="تم السماح بالصلاحية في هذا القروب"
            else:
                db.execute("INSERT OR REPLACE INTO staff_capability_denials(user_id,capability,chat_id,revoked_by,revoked_at) VALUES(?,?,?,?,?)",(target_uid,cap,chat_id,uid,now())); action="تم استثناء هذا القروب من الصلاحية العامة"
        elif explicit:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)); action="تم سحب الصلاحية المحددة"
        else:
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)",(target_uid,cap,chat_id,uid,now())); action="تم منح الصلاحية لهذا القروب"
        db.commit(); log_action(chat_id,uid,"unified_group_capability_changed",f"target={target_uid}; cap={cap}; action={action}")
        await safe_callback_answer(q,action); await q.edit_message_text(f"{action}.",reply_markup=unified_group_markup(target_uid,chat_id)); return
    if data.startswith("unifiedgrole:"):
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        try: _,raw_target,raw_chat,role=data.split(":",3); target_uid=int(raw_target); chat_id=int(raw_chat)
        except ValueError: await safe_callback_answer(q,"بيانات غير صحيحة.",show_alert=True); return
        if target_uid <= 0 or target_uid == OWNER_ID or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone() or role not in ("مشرف","مشرف عام","مدير","مدير عام","remove"):
            await safe_callback_answer(q,"القروب أو الرتبة غير صحيحة.",show_alert=True); return
        if role == "remove": db.execute("DELETE FROM group_roles WHERE chat_id=? AND user_id=?",(chat_id,target_uid)); action="تمت إزالة رتبة القروب"
        else: db.execute("INSERT OR REPLACE INTO group_roles(chat_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",(chat_id,target_uid,role,uid,now())); action=f"تم تعيين رتبة {role}"
        db.commit(); log_action(chat_id,uid,"unified_group_role_changed",f"target={target_uid}; role={role}; action={action}")
        await safe_callback_answer(q,action); await q.edit_message_text(f"{action}.",reply_markup=unified_group_markup(target_uid,chat_id)); return

    if data == "warnings_grant_id":
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection")):
            await safe_callback_answer(q,"ليس لديك صلاحية منح الإنذارات.",show_alert=True); return
        groups=db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title LIMIT 100").fetchall()
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            groups=[(cid,title) for cid,title in groups if can_use_capability(uid,"protection",cid)]
        rows=[[InlineKeyboardButton(f"📍 {title or chat_id}",callback_data=f"warnings_grant_group:{chat_id}")] for chat_id,title in groups[:60]]
        rows.append([InlineKeyboardButton("⬅️ مركز الإنذارات",callback_data="warnings_center")])
        await q.edit_message_text("اختر القروب الذي تريد منح الإنذار فيه:",reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("warnings_grant_group:"):
        try: target_chat=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q,"معرّف القروب غير صحيح.",show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection",target_chat)):
            await safe_callback_answer(q,"لا تملك صلاحية منح الإنذارات في هذا القروب.",show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(target_chat,)).fetchone(): await safe_callback_answer(q,"القروب غير مسجل.",show_alert=True); return
        context.user_data["awaiting_warning_grant"] = target_chat
        await q.message.reply_text(f"أرسل الآن ID العضو في القروب {target_chat}، ويمكنك إضافة السبب بعد المعرّف.\nمثال: 123456789 رابط مخالف\nأرسل /cancel للإلغاء.")
        return
    if data == "warnings_log_search":
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection")):
            await safe_callback_answer(q,"ليس لديك صلاحية عرض سجل الإنذارات.",show_alert=True); return
        context.user_data["awaiting_warning_log_search"] = True
        await q.message.reply_text("أرسل معرّف القروب ثم معرّف العضو:\nCHAT_ID USER_ID\nمثال: -1001234567890 123456789\nأرسل /cancel للإلغاء.")
        return
    if data == "warnings_group_list":
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection")):
            await safe_callback_answer(q,"ليس لديك صلاحية عرض المخالفين.",show_alert=True); return
        groups=db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title LIMIT 100").fetchall()
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            groups=[(chat_id,title) for chat_id,title in groups if can_use_capability(uid,"protection",chat_id)]
        rows=[[InlineKeyboardButton(f"⚠️ {title or chat_id}",callback_data=f"warnings_group:{chat_id}")] for chat_id,title in groups[:60]]
        rows.append([InlineKeyboardButton("⬅️ مركز الإنذارات",callback_data="warnings_center")])
        await q.edit_message_text("اختر القروب لعرض أعلى الأعضاء في سجل المخالفات.",reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("warnings_group:"):
        try: target_chat=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q,"معرّف القروب غير صحيح.",show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection",target_chat)):
            await safe_callback_answer(q,"لا تملك صلاحية هذا القروب.",show_alert=True); return
        rows=db.execute("SELECT user_id,display_name,warning_count,last_reason FROM member_violations WHERE chat_id=? ORDER BY warning_count DESC,last_at DESC LIMIT 15",(target_chat,)).fetchall()
        body=[f"⚠️ سجل مخالفات القروب {target_chat}"]
        body.extend([f"• {name or 'عضو'} — ID: {target} — إنذارات: {count} — السبب: {(reason or 'غير محدد')[:100]}" for target,name,count,reason in rows])
        if not rows: body.append("لا توجد مخالفات مسجلة.")
        await q.edit_message_text("\n".join(body)[:3900],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الإنذارات",callback_data="warnings_center")]])); return

    if data == "archive_search":
        if not (is_owner(uid) or capability_group_ids(uid, "archive")):
            await safe_callback_answer(q, "لا تملك صلاحية البحث في سجل التحويلات والملفات في أي قروب.", show_alert=True); return
        context.user_data["awaiting_archive_search"] = True
        await q.message.reply_text("🔎 أرسل ID العضو أو @username للبحث في القروبات المصرح لك بها فقط. البحث يشمل الرسائل التي وصلت للبوت بعد تفعيل الأرشفة. أرسل /cancel للإلغاء.")
        return

    if data == "warnings_center":
        allowed = (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid)
                   or is_protection_manager(uid) or can_use_capability(uid, "protection"))
        if not allowed:
            await safe_callback_answer(q, "ليس لديك صلاحية إدارة الإنذارات.", show_alert=True)
            return
        text = (
            "⚠️ <b>مركز الإنذارات التفاعلية</b>\n\n"
            "اختر طريقة منح الإنذار:\n\n"
            "<b>داخل القروب:</b> رد على رسالة العضو واكتب:\n"
            "<code>/warn سبب المخالفة</code>\n\n"
            "أو استخدم رقم العضو:\n"
            "<code>/warn USER_ID سبب المخالفة</code>\n\n"
            "<b>من الخاص مع البوت:</b> أرسل:\n"
            "<code>/warn CHAT_ID USER_ID سبب المخالفة</code>\n\n"
            "يسجل البوت الإنذار في سجل العضو، وينشر كرتًا تفاعليًا في القروب. "
            "عند الإنذار الثالث يحاول كتم العضو 3 أيام، ثم يفك الكتم تلقائيًا عند انتهاء المدة.\n\n"
            "تأكد من امتلاك البوت صلاحية تقييد الأعضاء، ومن امتلاكك صلاحية الإنذارات في القروب المستهدف."
        )
        buttons = [
            [InlineKeyboardButton("➕ منح إنذار برقم القروب والعضو", callback_data="warnings_grant_id")],
            [InlineKeyboardButton("📋 البحث في سجل إنذارات عضو", callback_data="warnings_log_search")],
            [InlineKeyboardButton("👥 عرض المخالفين حسب القروب", callback_data="warnings_group_list")],
            [InlineKeyboardButton("📋 سجل العمليات", callback_data="logs")],
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")],
        ]
        await safe_callback_answer(q, "تم فتح مركز الإنذارات.")
        try:
            await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await context.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("warnreview:"):
        try: _, raw_chat, raw_user = data.split(":", 2); target_chat=int(raw_chat); target_user=int(raw_user)
        except (ValueError, TypeError): await safe_callback_answer(q,"بيانات المخالفة غير صحيحة.",show_alert=True); return
        authorized = is_owner(uid) or is_delegated_owner(uid) or is_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection",target_chat)
        if not authorized: await safe_callback_answer(q,"مراجعة المخالفة للإدارة المخوّلة فقط.",show_alert=True); return
        row=db.execute("SELECT display_name,warning_count,abuse_count,last_reason,reasons_json,last_at FROM member_violations WHERE chat_id=? AND user_id=?",(target_chat,target_user)).fetchone()
        if not row: await safe_callback_answer(q,"لا توجد مخالفة مسجلة.",show_alert=True); return
        name,count,abuse,last_reason,reasons_json,last_at=row
        try: reasons=json.loads(reasons_json or "[]")
        except Exception: reasons=[]
        lines=[f"🔎 مراجعة مخالفة العضو {name or target_user}",f"ID: {target_user} | القروب: {target_chat}",f"عدد الإنذارات: {count} | الألفاظ المسيئة: {abuse}",f"آخر سبب: {last_reason or 'غير محدد'}",f"آخر تحديث: {last_at or 'غير محدد'}","", "تفاصيل المخالفات المسجلة:"]
        lines.extend([f"• {item.get('at','')}: {item.get('reason','')}" for item in reasons[-10:]])
        await q.message.reply_text("\n".join(lines)[:3900],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ إلغاء آخر إنذار",callback_data=f"warnremove_last:{target_chat}:{target_user}")],[InlineKeyboardButton("📋 سجل الإنذارات",callback_data=f"warnlog:{target_chat}:{target_user}")]])); return

    if data.startswith("warnremove_last:"):
        try: _, raw_chat, raw_user = data.split(":", 2); target_chat=int(raw_chat); target_user=int(raw_user)
        except (ValueError, TypeError): await safe_callback_answer(q,"بيانات الإنذار غير صحيحة.",show_alert=True); return
        authorized = is_owner(uid) or is_delegated_owner(uid) or is_manager(uid) or is_protection_manager(uid) or can_use_capability(uid,"protection",target_chat)
        if not authorized: await safe_callback_answer(q,"إلغاء الإنذار متاح للإدارة المخوّلة فقط.",show_alert=True); return
        row=db.execute("SELECT warning_count,abuse_count,last_reason,reasons_json FROM member_violations WHERE chat_id=? AND user_id=?",(target_chat,target_user)).fetchone()
        if not row or int(row[0] or 0)<=0: await safe_callback_answer(q,"لا يوجد إنذار لإلغائه.",show_alert=True); return
        count,abuse,last_reason,reasons_json=row
        try: reasons=json.loads(reasons_json or "[]")
        except Exception: reasons=[]
        removed=reasons.pop() if reasons else {"reason":last_reason or "إنذار سابق","at":now()}
        removed_reason=str(removed.get("reason","") if isinstance(removed,dict) else removed)
        if "لفظ مسيء" in removed_reason and int(abuse or 0)>0: abuse=int(abuse)-1
        new_count=max(0,int(count)-1)
        new_last=(reasons[-1].get("reason","") if reasons and isinstance(reasons[-1],dict) else "تم إلغاء آخر إنذار إداريًا")
        db.execute("UPDATE member_violations SET warning_count=?,abuse_count=?,last_reason=?,reasons_json=?,last_at=? WHERE chat_id=? AND user_id=?",(new_count,abuse,new_last,json.dumps(reasons,ensure_ascii=False),now(),target_chat,target_user))
        db.commit(); log_action(target_chat,uid,"warning_removed",f"target={target_user}; removed_reason={removed_reason[:150]}; remaining={new_count}")
        active=db.execute("SELECT status FROM automatic_mutes WHERE chat_id=? AND user_id=?",(target_chat,target_user)).fetchone()
        extra="\nتنبيه: إذا كان العضو مكتومًا بالفعل، فلن يُفك الكتم تلقائيًا بهذا الإجراء؛ راجع حالة الكتم يدويًا." if active and active[0]=="active" else ""
        await safe_callback_answer(q,"تم إلغاء آخر إنذار.",show_alert=True)
        await q.message.reply_text(f"↩️ تم إلغاء آخر إنذار للعضو {target_user}. المتبقي: {new_count}.{extra}")
        return

    if data.startswith("warnclear:"):
        try:
            _, raw_chat, raw_user = data.split(":", 2)
            target_chat, target_user = int(raw_chat), int(raw_user)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "بيانات الإنذار غير صحيحة.", show_alert=True); return
        authorized = is_owner(uid) or is_delegated_owner(uid) or is_manager(uid) or is_protection_manager(uid) or can_use_capability(uid, "protection", target_chat)
        if not authorized:
            await safe_callback_answer(q, "إلغاء الإنذارات متاح للإدارة المخوّلة فقط.", show_alert=True); return
        db.execute("UPDATE member_violations SET warning_count=0,abuse_count=0,last_reason='تمت تصفير الإنذارات إداريًا',reasons_json='[]',last_at=? WHERE chat_id=? AND user_id=?", (now(),target_chat,target_user))
        db.commit()
        log_action(target_chat, uid, "member_warnings_cleared", f"target={target_user}")
        await safe_callback_answer(q, "تم تصفير سجل الإنذارات.", show_alert=True)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    if data.startswith("warnlog:"):
        try:
            _, raw_chat, raw_user = data.split(":", 2)
            target_chat, target_user = int(raw_chat), int(raw_user)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "بيانات الإنذار غير صحيحة.", show_alert=True); return
        authorized = is_owner(uid) or is_delegated_owner(uid) or is_manager(uid) or is_protection_manager(uid) or can_use_capability(uid, "protection", target_chat)
        if not authorized:
            await safe_callback_answer(q, "سجل الإنذارات متاح للإدارة المخوّلة فقط.", show_alert=True); return
        row = db.execute("SELECT display_name,username,warning_count,abuse_count,last_reason,reasons_json,last_at FROM member_violations WHERE chat_id=? AND user_id=?", (target_chat,target_user)).fetchone()
        if not row:
            await safe_callback_answer(q, "لا يوجد سجل إنذارات لهذا العضو.", show_alert=True); return
        name, username, count, abuse, reason, reasons_json, last_at = row
        try: reasons = json.loads(reasons_json or "[]")
        except Exception: reasons = []
        lines = [f"📋 سجل الإنذارات — {name or target_user} (ID: {target_user})", f"إجمالي الإنذارات: {count} | الألفاظ المسيئة: {abuse}", f"آخر سبب: {reason or 'غير محدد'}", f"آخر تحديث: {last_at or 'غير محدد'}", "", "آخر المخالفات:"]
        for item in reasons[-8:]: lines.append(f"• {item.get('at','')}: {item.get('reason','')}")
        await safe_callback_answer(q, "تم فتح السجل.")
        await context.bot.send_message(chat_id=q.message.chat_id, text="\n".join(lines)[:3900])
        return

    if data.startswith("copy_user_id:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True)
            return
        await safe_callback_answer(q, f"ID: {target_id} — يمكنك نسخه يدويًا من الرسالة.", show_alert=True)
        return

    if data == "security_review_latest":
        if not (is_owner(uid) or can_use_capability(uid, "security_review")):
            await safe_callback_answer(q, "تقارير المراجعة الأمنية للمالك أو من منحه الصلاحية فقط.", show_alert=True); return
        row = db.execute("SELECT week_start,created_at,report FROM security_review_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            await q.edit_message_text("لم تُجرَ مراجعة أمنية أسبوعية بعد. سيُرسل التقرير تلقائيًا في موعد المراجعة الأسبوعية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return
        await q.edit_message_text(f"🛡️ آخر مراجعة أمنية — {row[0]}\nوقت التقرير: {row[1]}\n\n{row[2][:3500]}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    # Owner-only external group control center: scoped features and remote kill switch.
    if data == "external_groups":
        if not is_owner(uid):
            await safe_callback_answer(q, "إدارة القروبات الخارجية للمالك الأساسي فقط.", show_alert=True); return
        await q.edit_message_text("🌐 إدارة القروبات الخارجية\n\nاختر قروبًا لعرض معلوماته، وصلاحياته المتاحة، وحالة تفعيله. الذكاء الاصطناعي معطل افتراضيًا للقروبات الخارجية.", reply_markup=external_groups_markup()); return
    if data.startswith("external_group:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: ext_chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        row = db.execute("""SELECT e.title,e.enabled,e.ai_enabled,e.added_by,e.added_by_name,e.added_by_username,e.created_at
            FROM external_group_control e WHERE e.chat_id=? AND e.is_external=1""", (ext_chat_id,)).fetchone()
        if not row:
            await safe_callback_answer(q, "القروب الخارجي غير مسجل.", show_alert=True); return
        title, enabled, ai_enabled, added_by, added_name, added_username, created_at = row
        telegram_owner_label = "غير متاح عبر Telegram API"
        try:
            admins = await context.bot.get_chat_administrators(ext_chat_id)
            creator = next((admin.user for admin in admins if getattr(admin, "status", "") == "creator"), None)
            if creator:
                telegram_owner_label = f"{html.escape(creator.full_name or 'مالك القروب')} (ID: {creator.id})" + (f" — @{html.escape(creator.username)}" if creator.username else "")
        except Exception as owner_exc:
            log.debug("Could not resolve external group creator for %s: %s", ext_chat_id, owner_exc)
        caps = db.execute("SELECT capability,enabled FROM external_group_capabilities WHERE chat_id=? ORDER BY capability", (ext_chat_id,)).fetchall()
        cap_text = "\n".join(f"• {EXTERNAL_CAPABILITY_LABELS.get(cap,cap)}: {'مفعّل' if on else 'معطّل'}" for cap,on in caps)
        text = (f"🌐 معلومات القروب الخارجي\n\nالقروب: {html.escape(title or str(ext_chat_id))}\nID: {ext_chat_id}\n"
                f"مالك القروب في Telegram: {telegram_owner_label}\n"
                f"أضاف البوت: {html.escape(added_name or 'غير معروف')} (ID: {added_by or 'غير معروف'})"
                f"{' — @'+html.escape(added_username) if added_username else ''}\nتاريخ التسجيل: {created_at}\n"
                f"حالة البوت: {'مفعّل' if enabled else 'متوقف'}\nالذكاء الاصطناعي: {'مفعّل يدويًا' if ai_enabled else 'معطّل افتراضيًا'}\n\nالصلاحيات المتاحة:\n{cap_text}")
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=external_group_manage_markup(ext_chat_id)); return
    if data.startswith("external_toggle:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: ext_chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        row = db.execute("SELECT enabled FROM external_group_control WHERE chat_id=? AND is_external=1", (ext_chat_id,)).fetchone()
        if not row: await safe_callback_answer(q, "القروب غير موجود.", show_alert=True); return
        new_enabled = 0 if row[0] else 1
        if new_enabled:
            try:
                bot_member = await context.bot.get_chat_member(chat_id=ext_chat_id, user_id=context.bot.id)
                if getattr(bot_member, "status", "left") not in ("member", "administrator", "creator"):
                    await safe_callback_answer(q, "لا يمكن تفعيل الخدمة لأن البوت ليس عضوًا في هذا القروب.", show_alert=True); return
            except Exception:
                await safe_callback_answer(q, "تعذر التحقق من وجود البوت في القروب. أضف البوت أولًا ثم أعد المحاولة.", show_alert=True); return
        db.execute("UPDATE external_group_control SET enabled=?,updated_at=? WHERE chat_id=?", (new_enabled, now(), ext_chat_id)); db.commit()
        log_action(ext_chat_id, uid, "external_group_toggle", f"enabled={new_enabled}")
        await safe_callback_answer(q, "تم إيقاف خدمات البوت عن القروب." if not new_enabled else "تم تفعيل البوت للقروب.", show_alert=True)
        await q.edit_message_text("🌐 إدارة القروب الخارجي", reply_markup=external_group_manage_markup(ext_chat_id)); return
    if data.startswith("external_ai:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: ext_chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        row = db.execute("SELECT ai_enabled FROM external_group_control WHERE chat_id=? AND is_external=1", (ext_chat_id,)).fetchone()
        if not row: await safe_callback_answer(q, "القروب غير موجود.", show_alert=True); return
        enabled = 0 if row[0] else 1
        db.execute("UPDATE external_group_control SET ai_enabled=?,updated_at=? WHERE chat_id=?", (enabled, now(), ext_chat_id))
        db.execute("""INSERT INTO external_group_capabilities(chat_id,capability,enabled,granted_by,granted_at)
            VALUES(?,'ai',?,?,?) ON CONFLICT(chat_id,capability) DO UPDATE SET enabled=excluded.enabled,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
            (ext_chat_id, enabled, uid, now()))
        db.commit(); log_action(ext_chat_id, uid, "external_ai_permission", f"enabled={enabled}")
        await q.edit_message_text("🌐 إدارة القروب الخارجي", reply_markup=external_group_manage_markup(ext_chat_id)); return
    if data.startswith("external_cap:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: _, cid_raw, cap = data.split(":",2); ext_chat_id = int(cid_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if cap not in EXTERNAL_CAPABILITY_LABELS or cap == "ai":
            await safe_callback_answer(q, "الصلاحية غير متاحة.", show_alert=True); return
        exists = db.execute("SELECT is_external FROM external_group_control WHERE chat_id=?", (ext_chat_id,)).fetchone()
        if not exists or not exists[0]: await safe_callback_answer(q, "القروب غير خارجي أو غير مسجل.", show_alert=True); return
        old = db.execute("SELECT enabled FROM external_group_capabilities WHERE chat_id=? AND capability=?", (ext_chat_id,cap)).fetchone()
        enabled = 0 if old and old[0] else 1
        db.execute("""INSERT INTO external_group_capabilities(chat_id,capability,enabled,granted_by,granted_at)
            VALUES(?,?,?,?,?) ON CONFLICT(chat_id,capability) DO UPDATE SET enabled=excluded.enabled,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
            (ext_chat_id, cap, enabled, uid, now()))
        db.commit(); log_action(ext_chat_id, uid, "external_capability", f"{cap}={enabled}")
        await q.edit_message_text("🌐 إدارة القروب الخارجي", reply_markup=external_group_manage_markup(ext_chat_id)); return
    if data == "external_support_inbox":
        if not is_owner(uid): await safe_callback_answer(q, "صندوق الدعم للمالك الأساسي فقط.", show_alert=True); return
        await q.edit_message_text("🆘 صندوق الدعم الفني\nاختر طلبًا لقراءة التفاصيل والرد.", reply_markup=support_tickets_markup()); return
    if data.startswith("support_ticket:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: ticket_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "رقم الطلب غير صحيح.", show_alert=True); return
        ticket = db.execute("""SELECT chat_id,group_title,requester_id,requester_name,requester_username,message,status,created_at,answer
            FROM external_support_tickets WHERE id=?""", (ticket_id,)).fetchone()
        if not ticket: await safe_callback_answer(q, "الطلب غير موجود.", show_alert=True); return
        cid,title,requester_id,requester_name,requester_username,message,status,created_at,answer = ticket
        body = (f"🆘 طلب الدعم #{ticket_id}\nالقروب: {html.escape(title or str(cid))} ({cid})\n"
                f"المرسل: {html.escape(requester_name or 'مستخدم')} (ID: {requester_id})"
                f"{' — @'+html.escape(requester_username) if requester_username else ''}\nالحالة: {status}\nالتاريخ: {created_at}\n\n"
                f"المشكلة:\n{html.escape(message[:2500])}")
        if answer: body += f"\n\nالرد السابق:\n{html.escape(answer[:1500])}"
        await q.edit_message_text(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ الرد على صاحب القروب", callback_data=f"support_reply:{ticket_id}")],
            [InlineKeyboardButton("⬅️ صندوق الدعم", callback_data="external_support_inbox")]])); return
    if data.startswith("support_reply:"):
        if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        try: ticket_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "رقم الطلب غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM external_support_tickets WHERE id=?", (ticket_id,)).fetchone():
            await safe_callback_answer(q, "الطلب غير موجود.", show_alert=True); return
        context.user_data["support_reply_ticket_id"] = ticket_id
        await q.message.reply_text(f"✍️ أرسل الرد على طلب الدعم #{ticket_id}. أرسل /cancel للإلغاء."); return
    if data.startswith("group_panel_support:"):
        try: support_chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (support_chat_id,)).fetchone() or not await is_telegram_group_admin(context.bot, uid, support_chat_id):
            await safe_callback_answer(q, "الدعم متاح لمالك القروب ومشرفيه الفعليين فقط.", show_alert=True); return
        bot_username = getattr(context.bot, "username", "") or ""
        if not bot_username:
            bot_username = (await context.bot.get_me()).username or ""
        link = f"https://t.me/{bot_username}?start=support_{support_chat_id}"
        await q.message.reply_text("🆘 لفتح طلب دعم خاص، اضغط الزر التالي ثم أرسل وصف المشكلة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("فتح الدعم الفني", url=link)]])); return
    if data.startswith(("group_panel_violators:", "external_violators:")):
        external_view = data.startswith("external_violators:")
        try: viol_chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if external_view:
            if not is_owner(uid): await safe_callback_answer(q, "للمالك الأساسي فقط.", show_alert=True); return
        elif not await can_access_group(context.bot, uid, viol_chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
        rows = db.execute("SELECT user_id,display_name,username,warning_count,abuse_count,last_reason,last_at FROM member_violations WHERE chat_id=? ORDER BY warning_count DESC,last_at DESC LIMIT 25", (viol_chat_id,)).fetchall()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (viol_chat_id,)).fetchone()
        text = f"⚠️ قائمة المخالفين — {title_row[0] if title_row else viol_chat_id}\n\n"
        if not rows: text += "لا توجد مخالفات مسجلة."
        else:
            for target_id, name, username, count, abuse_count, reason, when in rows:
                text += f"• {name or 'عضو'}{' (@'+username+')' if username else ''} — ID: {target_id}\n  الإنذارات: {count} | إنذارات الإساءة: {abuse_count}/3 | السبب: {reason} | آخر مخالفة: {when}\n"
        back = f"external_group:{viol_chat_id}" if external_view else f"group_panel_home:{viol_chat_id}"
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=back)]])); return

    if data == "bulkdel_menu":
        if not can_bulk_delete(uid) and not capability_group_ids(uid, "bulk_delete"):
            await safe_callback_answer(q, "لا تملك صلاحية مسح الرسائل. اطلب من المالك منحك صلاحية المسح للقروب المطلوب.", show_alert=True); return
        groups = bulk_delete_groups_for_user(uid)
        if not groups:
            await q.edit_message_text("لا توجد قروبات ضمن صلاحيات مسح الرسائل الممنوحة لك.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return
        context.user_data["bulk_delete_selected_groups"] = []
        context.user_data["bulk_delete_groups_snapshot"] = {int(cid): str(title or cid) for cid, title in groups}
        await q.edit_message_text("🗑️ مسح الرسائل\nحدد القروبات التي تريد المسح منها. الحد الأقصى 500 رسالة لكل قروب محدد.", reply_markup=bulk_delete_groups_markup(groups, set()))
        return
    if data.startswith("bulkdel_toggle:"):
        try: cid = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        groups = bulk_delete_groups_for_user(uid)
        if not any(int(gid) == cid for gid, _ in groups):
            await safe_callback_answer(q, "القروب خارج نطاق صلاحياتك.", show_alert=True); return
        selected = set(int(x) for x in context.user_data.get("bulk_delete_selected_groups", []))
        selected.remove(cid) if cid in selected else selected.add(cid)
        context.user_data["bulk_delete_selected_groups"] = sorted(selected)
        await q.edit_message_text(f"🗑️ حدد القروبات المطلوبة. المحدد: {len(selected)} — الحد الأقصى 500 رسالة لكل قروب.", reply_markup=bulk_delete_groups_markup(groups, selected)); return
    if data in ("bulkdel_all", "bulkdel_none"):
        groups = bulk_delete_groups_for_user(uid)
        selected = [int(cid) for cid, _ in groups] if data == "bulkdel_all" else []
        context.user_data["bulk_delete_selected_groups"] = selected
        await q.edit_message_text(f"🗑️ حدد القروبات المطلوبة. المحدد: {len(selected)} — الحد الأقصى 500 رسالة لكل قروب.", reply_markup=bulk_delete_groups_markup(groups, set(selected))); return
    if data == "bulkdel_groups_done":
        selected = [int(x) for x in context.user_data.get("bulk_delete_selected_groups", [])]
        allowed = {int(cid) for cid, _ in bulk_delete_groups_for_user(uid)}
        selected = [cid for cid in selected if cid in allowed]
        if not selected:
            await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل ضمن صلاحياتك.", show_alert=True); return
        context.user_data["bulk_delete_selected_groups"] = selected
        context.user_data["bulk_delete_stage"] = "count"
        await q.message.reply_text("🗑️ أرسل عدد الرسائل المطلوب مسحها من كل قروب محدد، من 1 إلى 500.\n\nسيطلب البوت تأكيدًا قبل تنفيذ المسح. أرسل /cancel للإلغاء.")
        return
    if data == "bulkdel_confirm":
        selected = [int(x) for x in context.user_data.get("bulk_delete_selected_groups", [])]
        count = int(context.user_data.get("bulk_delete_count", 0) or 0)
        allowed = {int(cid) for cid, _ in bulk_delete_groups_for_user(uid)}
        selected = [cid for cid in selected if cid in allowed]
        if not selected or not 1 <= count <= 500:
            context.user_data.pop("bulk_delete_stage", None)
            await safe_callback_answer(q, "انتهت صلاحية العملية أو تغيّرت الصلاحيات. افتح الأيقونة مجددًا.", show_alert=True); return
        await safe_callback_answer(q, "بدأ المسح. قد يستغرق وقتًا بحسب عدد القروبات.")
        results = []
        bot_me = await context.bot.get_me()
        for cid in selected:
            title = context.user_data.get("bulk_delete_groups_snapshot", {}).get(cid, str(cid))
            if not can_bulk_delete(uid, cid):
                results.append(f"⛔ {title}: لم تعد الصلاحية متاحة."); continue
            try:
                bot_member = await context.bot.get_chat_member(chat_id=cid, user_id=bot_me.id)
                if bot_member.status != "administrator" or not getattr(bot_member, "can_delete_messages", False):
                    results.append(f"⚠️ {title}: البوت يحتاج صلاحية حذف الرسائل."); continue
                ids = list(recent_message_ids[cid])[-count:]
                deleted = failed = 0
                for mid in reversed(ids):
                    try:
                        await context.bot.delete_message(chat_id=cid, message_id=mid)
                        deleted += 1
                    except Exception as exc:
                        failed += 1
                        log.debug("Bulk-delete skipped message %s in %s: %s", mid, cid, exc)
                log_action(cid, uid, "bulk_delete_panel", f"requested={count};deleted={deleted};failed={failed}")
                results.append(f"🗑️ {title}\nتم الحذف: {deleted} | تعذر: {failed} | الرسائل المرصودة المتاحة: {len(ids)}")
            except Exception as exc:
                log.warning("Bulk delete failed for group %s: %s", cid, exc)
                results.append(f"⚠️ {title}: تعذر تنفيذ المسح أو التحقق من الصلاحيات.")
        for key in ("bulk_delete_stage", "bulk_delete_count", "bulk_delete_selected_groups", "bulk_delete_groups_snapshot"):
            context.user_data.pop(key, None)
        summary = "🧾 تقرير عملية مسح الرسائل\n\n" + "\n\n".join(results)
        await q.message.reply_text(summary[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")]]))
        return
    if data == "bulkdel_cancel":
        for key in ("bulk_delete_stage", "bulk_delete_count", "bulk_delete_selected_groups", "bulk_delete_groups_snapshot"):
            context.user_data.pop(key, None)
        await q.edit_message_text("تم إلغاء عملية مسح الرسائل.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data == "massban_menu":
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "الحظر الجماعي يتطلب صلاحية يمنحها المالك الأساسي.", show_alert=True); return
        await q.edit_message_text("🚫 الحظر الجماعي\n\nاختر نطاق الحظر. الحظر الشامل يطبّق على جميع القروبات المسجلة، والجزئي على القروبات التي تحددها. يمكنك إرسال ID أو @username معروفًا في قاعدة بيانات الأعضاء.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 حظر شامل — جميع القروبات", callback_data="massban_start:all")],
            [InlineKeyboardButton("📍 حظر جزئي — قروبات محددة", callback_data="massban_start:selected")],
            [InlineKeyboardButton("📋 المحظورون", callback_data="massban_list")],
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return
    if data.startswith("massban_start:"):
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        scope = data.split(":",1)[1]
        if scope == "all":
            context.user_data["mass_ban_state"] = {"scope":"all","reason":"حظر جماعي من لوحة البوت"}
            await q.message.reply_text("🚫 أرسل رقم ID للعضو أو @username الذي تريد حظره من جميع القروبات. أرسل /cancel للإلغاء."); return
        if scope == "selected":
            context.user_data["massban_selected_groups"] = []
            groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
            await q.edit_message_text("اختر القروبات التي تريد تطبيق الحظر عليها:", reply_markup=massban_groups_markup(groups,set())); return
        await safe_callback_answer(q, "نطاق غير صحيح.", show_alert=True); return
    if data.startswith("massban_group_toggle:"):
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: cid = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        selected = set(context.user_data.get("massban_selected_groups", []))
        if cid in selected: selected.remove(cid)
        else: selected.add(cid)
        context.user_data["massban_selected_groups"] = list(selected)
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        await q.edit_message_text(f"اختر القروبات التي تريد تطبيق الحظر عليها. المحدد: {len(selected)}", reply_markup=massban_groups_markup(groups,selected)); return
    if data in ("massban_groups_all","massban_groups_none"):
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        selected = [r[0] for r in db.execute("SELECT chat_id FROM watched_groups").fetchall()] if data=="massban_groups_all" else []
        context.user_data["massban_selected_groups"] = selected
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        await q.edit_message_text(f"اختر القروبات التي تريد تطبيق الحظر عليها. المحدد: {len(selected)}", reply_markup=massban_groups_markup(groups,set(selected))); return
    if data == "massban_groups_done":
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        selected = context.user_data.get("massban_selected_groups", [])
        if not selected: await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل.", show_alert=True); return
        context.user_data["mass_ban_state"] = {"scope":"selected","groups":selected,"reason":"حظر جزئي من لوحة البوت"}
        await q.message.reply_text("🚫 أرسل رقم ID أو @username للعضو المطلوب حظره من القروبات المحددة. أرسل /cancel للإلغاء."); return
    if data == "massban_list":
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        bans = db.execute("SELECT user_id,display_name,username,scope,active FROM global_bans ORDER BY created_at DESC LIMIT 30").fetchall()
        rows = []
        for target_id,name,username,scope,active in bans:
            if active:
                rows.append([InlineKeyboardButton(f"🔓 فك الحظر: {(name or target_id)} ({target_id})", callback_data=f"massban_unban:{target_id}")])
        rows.append([InlineKeyboardButton("⬅️ الحظر الجماعي", callback_data="massban_menu")])
        await q.edit_message_text("📋 قائمة المحظورين\nاضغط على العضو لفك الحظر عنه من نطاق الحظر المسجل.", reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("massban_unban:"):
        if not (is_owner(uid) or can_use_capability(uid, "mass_ban")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: target_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        row = db.execute("SELECT scope FROM global_bans WHERE user_id=?", (target_id,)).fetchone()
        if not row: await safe_callback_answer(q, "العضو غير موجود في القائمة.", show_alert=True); return
        group_ids = [r[0] for r in db.execute("SELECT chat_id FROM global_ban_groups WHERE user_id=?", (target_id,)).fetchall()] if row[0]=="selected" else [r[0] for r in db.execute("SELECT chat_id FROM watched_groups").fetchall()]
        db.execute("UPDATE global_bans SET active=0 WHERE user_id=?", (target_id,))
        db.commit()
        unbanned = failed = 0
        for cid in group_ids:
            try:
                await context.bot.unban_chat_member(chat_id=cid, user_id=target_id, only_if_banned=True)
                unbanned += 1
            except Exception as exc:
                failed += 1
                log.debug("Global unban failed chat=%s target=%s: %s", cid, target_id, exc)
        await q.edit_message_text(f"✅ تم تعطيل الحظر المسجل للعضو {target_id}.\nنجح فك الحظر في {unbanned} قروب، وتعذر في {failed}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المحظورون", callback_data="massban_list")]])); return

    if data in {"university_portal", "university_portal_back"} or data.startswith("portal_quote:"):
        await safe_callback_answer(q, "تمت إزالة البوابة الجامعية من البوت.", show_alert=True)
        return
    if data == "coursework_portal_help":
        await q.message.reply_text(
            "📋 الأعمال الفصلية محفوظة داخل البوت. اختر صفحة لعرضها أو تحريرها:",
            reply_markup=coursework_menu_markup(),
        )
        return
    if data == "coursework:choose_edit":
        await q.message.reply_text("اختر الصفحة التي تريد تحريرها:", reply_markup=coursework_menu_markup()); return
    if data.startswith("coursework:page:"):
        try:
            page_no = int(data.rsplit(":", 1)[1])
            if page_no not in (1, 2, 3, 4): raise ValueError
        except ValueError:
            await safe_callback_answer(q, "رقم الصفحة غير صحيح.", show_alert=True); return
        row = db.execute("SELECT content, updated_at FROM coursework_pages WHERE user_id=? AND page_no=?", (uid, page_no)).fetchone()
        content = row[0] if row and row[0] else "لا يوجد محتوى محفوظ في هذه الصفحة حتى الآن. اضغط «تعديل محتوى الصفحة» لإضافة بياناتك."
        updated = row[1] if row else "لم تُحرر بعد"
        await q.message.reply_text(f"📋 الأعمال الفصلية — الصفحة {page_no}\nآخر تحديث: {updated}\n\n{content[:3500]}", reply_markup=coursework_edit_markup(page_no)); return
    if data.startswith("coursework:edit:"):
        try:
            page_no = int(data.rsplit(":", 1)[1])
            if page_no not in (1, 2, 3, 4): raise ValueError
        except ValueError:
            await safe_callback_answer(q, "رقم الصفحة غير صحيح.", show_alert=True); return
        context.user_data["awaiting_coursework_page"] = page_no
        await q.message.reply_text(f"✏️ أرسل الآن النص الذي تريد حفظه في الصفحة {page_no}. سيتم حفظه لحسابك فقط.\nأرسل /cancel للإلغاء."); return
    if data.startswith("coursework:clear:"):
        try:
            page_no = int(data.rsplit(":", 1)[1])
            if page_no not in (1, 2, 3, 4): raise ValueError
        except ValueError:
            await safe_callback_answer(q, "رقم الصفحة غير صحيح.", show_alert=True); return
        db.execute("DELETE FROM coursework_pages WHERE user_id=? AND page_no=?", (uid, page_no)); db.commit()
        await q.message.reply_text(f"🧹 تم مسح محتوى الصفحة {page_no} من سجلك.", reply_markup=coursework_menu_markup()); return
    if data == "student_achievements_help":
        await q.message.reply_text(
            "🏆 لم يعد هناك رابط بوابة خارجي. لمتابعة الدراسة استخدم /studyadd لإضافة جلسة مذاكرة و/studyschedule لعرض الجدول المحفوظ."
        )
        return

    if data == "calendar:back":
        await q.edit_message_text("🤖 قائمة الخدمات الشخصية والطلابية:", reply_markup=assistant_menu_markup()); return
    if data == "calendar:noop":
        return
    if data.startswith("calendar:day:"):
        try:
            _, _, year_raw, month_raw, day_raw = data.split(":", 4)
            chosen = datetime(int(year_raw), int(month_raw), int(day_raw))
        except (ValueError, TypeError):
            await safe_callback_answer(q, "التاريخ غير صحيح.", show_alert=True); return
        result = await calendar_date_text(chosen.strftime("%Y-%m-%d"))
        if result:
            await q.message.reply_text(result, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ العودة للتقويم", callback_data=f"calendar:month:{chosen.year}:{chosen.month}")], ]))
        else:
            await safe_callback_answer(q, "تعذر جلب تفاصيل التاريخ الآن.", show_alert=True)
        return
    if data in ("calendar:home", "calendar:current"):
        dt = datetime.now(ZoneInfo("Asia/Riyadh"))
        grid_text, grid_markup = await calendar_grid_view(dt.year, dt.month)
        if not grid_text:
            await q.edit_message_text("⚠️ تعذر جلب التقويم الشبكي الآن. تحقق من اتصال الخدمة ثم حاول مجددًا.", reply_markup=grid_markup); return
        await q.edit_message_text(grid_text[:3900], reply_markup=grid_markup); return
    if data.startswith("calendar:month:"):
        try:
            _, _, year_raw, month_raw = data.split(":", 3); year, month = int(year_raw), int(month_raw)
            if not 1900 <= year <= 2100 or not 1 <= month <= 12: raise ValueError
        except ValueError:
            await safe_callback_answer(q, "التاريخ غير صحيح.", show_alert=True); return
        grid_text, grid_markup = await calendar_grid_view(year, month)
        if not grid_text:
            await q.edit_message_text("⚠️ تعذر جلب التقويم الشبكي الآن. حاول لاحقًا.", reply_markup=grid_markup); return
        await q.edit_message_text(grid_text[:3900], reply_markup=grid_markup); return
    if data == "calendar:search":
        context.user_data["awaiting_calendar_search"] = True
        await q.message.reply_text("🔎 أرسل تاريخًا ميلاديًا للتحويل مثل 2026-10-16 أو 16/10/2026، أو اكتب اسم مناسبة مثل رمضان أو عرفة للبحث في التقويم.\nللإلغاء أرسل /cancel.")
        return
    if data.startswith("adhkar_auto_page:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإعداد للمالك الأساسي فقط.", show_alert=True); return
        try:
            page = max(0, int(data.split(":", 1)[1]))
        except (ValueError, IndexError):
            await safe_callback_answer(q, "رقم الصفحة غير صحيح.", show_alert=True); return
        await q.edit_message_text(
            "🌙 <b>إعداد أذكار المساء المجدولة</b>\n\n"
            "حدد القروبات التي ينشر فيها البوت أذكار المساء مرة واحدة يوميًا عند الساعة 6:00 مساءً بتوقيت السعودية. "
            "القروبات الجديدة لا تُفعّل تلقائيًا.",
            parse_mode="HTML", reply_markup=adhkar_auto_settings_markup(page))
        await safe_callback_answer(q)
        return

    if data == "adhkar_auto_settings":
        if not is_owner(uid):
            await safe_callback_answer(q, "إعداد النشر المجدول لأذكار المساء متاح للمالك الأساسي فقط.", show_alert=True); return
        enabled_count = db.execute("SELECT COUNT(*) FROM adhkar_group_settings WHERE enabled=1").fetchone()[0]
        total_groups = db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0]
        await q.edit_message_text(
            "🌙 <b>إعداد أذكار المساء المجدولة</b>\n\n"
            "اختر القروبات التي ينشر فيها البوت الأذكار مرة واحدة يوميًا عند الساعة 6:00 مساءً بتوقيت السعودية. "
            "يسجل البوت نجاح كل قروب لمنع تكرار النشر في اليوم نفسه. القروبات الجديدة لا تُفعّل تلقائيًا.",
            parse_mode="HTML", reply_markup=adhkar_auto_settings_markup(0))
        await safe_callback_answer(q, f"مفعّلة في {enabled_count} من {total_groups} قروب.")
        return
    if data == "adhkar_auto_all_on":
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإعداد للمالك الأساسي فقط.", show_alert=True); return
        groups = db.execute("SELECT chat_id FROM watched_groups").fetchall()
        stamp = now()
        for (chat_id,) in groups:
            db.execute("INSERT INTO adhkar_group_settings(chat_id,enabled,updated_at,updated_by) VALUES(?,1,?,?) "
                       "ON CONFLICT(chat_id) DO UPDATE SET enabled=1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                       (int(chat_id), stamp, uid))
        db.commit()
        log_action(0, uid, "evening_adhkar_all_on", f"groups={len(groups)}")
        await q.edit_message_text(f"⏳ تم تفعيل أذكار المساء لـ {len(groups)} قروب. سيبدأ النشر في الموعد المحدد، مع منع التكرار لكل قروب في اليوم نفسه.", reply_markup=adhkar_auto_settings_markup(0))
        await safe_callback_answer(q, "تم حفظ الإعداد.")
        return
    if data == "adhkar_auto_all_off":
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإعداد للمالك الأساسي فقط.", show_alert=True); return
        db.execute("UPDATE adhkar_group_settings SET enabled=0,updated_at=?,updated_by=?", (now(), uid)); db.commit()
        log_action(0, uid, "evening_adhkar_all_off", "disabled all registered groups")
        await q.edit_message_text("⏸️ تم إيقاف النشر التلقائي لأذكار المساء في جميع القروبات المسجلة.", reply_markup=adhkar_auto_settings_markup())
        await safe_callback_answer(q, "تم إيقاف النشر.")
        return
    if data.startswith("adhkar_auto_toggle:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإعداد للمالك الأساسي فقط.", show_alert=True); return
        try:
            parts = data.split(":")
            target_chat_id = int(parts[1])
            page = max(0, int(parts[2])) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        group_exists = db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (target_chat_id,)).fetchone()
        if not group_exists:
            await safe_callback_answer(q, "هذا القروب غير موجود في السجل.", show_alert=True); return
        row = db.execute("SELECT enabled FROM adhkar_group_settings WHERE chat_id=?", (target_chat_id,)).fetchone()
        new_enabled = 0 if row and int(row[0]) else 1
        db.execute("INSERT INTO adhkar_group_settings(chat_id,enabled,updated_at,updated_by) VALUES(?,?,?,?) "
                   "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                   (target_chat_id, new_enabled, now(), uid))
        db.commit()
        log_action(target_chat_id, uid, "evening_adhkar_group_toggle", f"enabled={new_enabled}")
        await q.edit_message_text(f"{'✅ تم تفعيل' if new_enabled else '⏸️ تم إيقاف'} أذكار المساء للقروب المحدد.", reply_markup=adhkar_auto_settings_markup(page))
        await safe_callback_answer(q, "تم حفظ الإعداد.")
        return

    if data == "adhkar:back":
        await q.edit_message_text("🤖 قائمة الخدمات الشخصية والطلابية:", reply_markup=assistant_menu_markup()); return
    if data == "adhkar:home":
        await q.edit_message_text("🤲 الأذكار والأدعية — اختر القسم:", reply_markup=adhkar_markup())
        return
    if data.startswith("adhkar:"):
        key = data.split(":", 1)[1]
        if key not in ADHKAR:
            await safe_callback_answer(q, "القسم غير موجود.", show_alert=True); return
        title, items = ADHKAR[key]
        body = title + "\n\n" + "\n\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        await q.edit_message_text(body[:3900], reply_markup=adhkar_markup()); return

    if data == "staff_permissions":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "إدارة الطاقم للمالك والمفوّض فقط.", show_alert=True); return
        context.user_data["awaiting_staff_permissions_id"] = True
        await q.message.reply_text("👥 أرسل رقم المستخدم الذي تريد ترقيته أو تعديل صلاحياته.\nيمكنك معرفة الرقم من أمر /id، وللإلغاء أرسل /cancel.")
        return

    if data.startswith("staffmanage:"):
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا الإجراء للمالك والمفوّض فقط.", show_alert=True); return
        try: target_uid = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم مستخدم غير صحيح.", show_alert=True); return
        if target_uid == OWNER_ID or is_delegated_owner(target_uid):
            await safe_callback_answer(q, "لا يمكن تعديل صلاحيات المالكين من هذه اللوحة.", show_alert=True); return
        await q.edit_message_text(f"👥 إدارة الطاقم\nالمستخدم: {user_display_name(target_uid)}\nالمعرّف: {target_uid}\n\nاختر الرتبة والصلاحيات. المدير العام يملك صلاحيات تشغيلية واسعة؛ أما الصلاحيات المنفردة فتمنح وصولًا محدودًا.", reply_markup=staff_permissions_markup(target_uid))
        return

    if data.startswith("staffrole:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "منح وسحب الرتب العامة يتطلب موافقة المالك الأساسي.", show_alert=True); return
        try: _, target_raw, role = data.split(":", 2); target_uid = int(target_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if target_uid == OWNER_ID or is_delegated_owner(target_uid) or role not in ("manager", "protection"):
            await safe_callback_answer(q, "لا يمكن تعديل هذا الحساب أو الرتبة.", show_alert=True); return
        table = "managers" if role == "manager" else "protection_managers"
        exists = db.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (target_uid,)).fetchone()
        if exists:
            db.execute(f"DELETE FROM {table} WHERE user_id=?", (target_uid,)); action = "تم سحب الرتبة"
        else:
            db.execute(f"INSERT OR REPLACE INTO {table}(user_id,added_by,added_at) VALUES(?,?,?)", (target_uid, uid, now())); action = "تم منح الرتبة"
        db.commit(); log_action(0, uid, "staff_role_changed", f"target={target_uid}; role={role}; action={action}")
        await safe_callback_answer(q, action)
        await q.edit_message_text(f"{action} للمستخدم {target_uid}.", reply_markup=staff_permissions_markup(target_uid)); return

    if data.startswith("staffcap:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإجراء للمالك والمفوّض فقط.", show_alert=True); return
        try: _, target_raw, cap = data.split(":", 2); target_uid = int(target_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if target_uid == OWNER_ID or (is_delegated_owner(target_uid) and cap != "message_lock") or cap not in STAFF_CAPABILITY_LABELS:
            await safe_callback_answer(q, "حساب أو صلاحية غير مسموحة.", show_alert=True); return
        exists = db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone()
        if exists:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap))
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target_uid, cap))
            action = "تم سحب الصلاحية"
        else:
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target_uid, cap))
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)", (target_uid, cap, 0, uid, now()))
            action = "تم منح الصلاحية لجميع القروبات"
        db.commit(); log_action(0, uid, "staff_capability_changed", f"target={target_uid}; capability={cap}; action={action}")
        await safe_callback_answer(q, action)
        await q.edit_message_text(f"{action} للمستخدم {target_uid}.", reply_markup=staff_permissions_markup(target_uid)); return

    if data.startswith("staffgroups:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإجراء للمالك والمفوّض فقط.", show_alert=True); return
        try: target_uid = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم مستخدم غير صحيح.", show_alert=True); return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
        rows = [[InlineKeyboardButton("🏘️ " + ((title or str(cid))[:45]), callback_data=f"staffgroup:{target_uid}:{cid}")] for cid, title in groups[:60]]
        if not rows: rows = [[InlineKeyboardButton("لا توجد قروبات مسجلة", callback_data="noop")]]
        rows.append([InlineKeyboardButton("⬅️ إدارة الموظف", callback_data=f"staffmanage:{target_uid}")])
        await q.edit_message_text(f"📍 اختر قروبًا لإدارة صلاحيات {target_uid}:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("staffgroup:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإجراء للمالك والمفوّض فقط.", show_alert=True); return
        try: _, target_raw, chat_raw = data.split(":", 2); target_uid = int(target_raw); chat_id = int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        group = db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if not group: await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        await q.edit_message_text(f"🔐 صلاحيات المستخدم {target_uid} في القروب {chat_id}\nاضغط على الصلاحية لتبديلها. إذا كانت الصلاحية عامة، فإن الضغط يسجل استثناءً لهذا القروب.", reply_markup=staff_group_capabilities_markup(target_uid, chat_id)); return

    if data.startswith("staffgrole:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "رفع الرتب من صلاحية المالك والمفوّض فقط.", show_alert=True); return
        try: _, target_raw, chat_raw, role = data.split(":", 3); target_uid = int(target_raw); chat_id = int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if target_uid == OWNER_ID or is_delegated_owner(target_uid):
            await safe_callback_answer(q, "لا يمكن تغيير رتبة المالك أو المالك المفوّض.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        if role == "remove":
            db.execute("DELETE FROM group_roles WHERE chat_id=? AND user_id=?", (chat_id, target_uid))
            action = "تمت إزالة رتبة العضو من هذا القروب"
        elif role in GROUP_ROLE_PERMISSIONS:
            db.execute("INSERT OR REPLACE INTO group_roles(chat_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)", (chat_id, target_uid, role, uid, now()))
            action = f"تم رفع العضو إلى {role} داخل هذا القروب"
        else:
            await safe_callback_answer(q, "الرتبة غير معروفة.", show_alert=True); return
        db.commit(); log_action(chat_id, uid, "group_role_managed_from_panel", f"target={target_uid}; role={role}")
        await safe_callback_answer(q, action)
        await q.edit_message_text(f"{action}.\n\nالمستخدم: {target_uid}\nالقروب: {chat_id}", reply_markup=staff_group_capabilities_markup(target_uid, chat_id)); return

    if data.startswith("staffgcap:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإجراء للمالك والمفوّض فقط.", show_alert=True); return
        try: _, target_raw, chat_raw, cap = data.split(":", 3); target_uid = int(target_raw); chat_id = int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if cap not in STAFF_CAPABILITY_LABELS or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "قروب أو صلاحية غير صحيحة.", show_alert=True); return
        global_grant = db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone()
        local_grant = db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id)).fetchone()
        denied = db.execute("SELECT 1 FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id)).fetchone()
        if global_grant and not denied:
            db.execute("INSERT OR REPLACE INTO staff_capability_denials(user_id,capability,chat_id,revoked_by,revoked_at) VALUES(?,?,?,?,?)", (target_uid, cap, chat_id, uid, now()))
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id))
        elif denied:
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id))
        elif local_grant:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?", (target_uid, cap, chat_id))
        else:
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)", (target_uid, cap, chat_id, uid, now()))
        db.commit(); log_action(chat_id, uid, "staff_group_capability_changed", f"target={target_uid}; capability={cap}")
        await safe_callback_answer(q, "تم تحديث صلاحية القروب.")
        await q.edit_message_text(f"🔐 صلاحيات المستخدم {target_uid} في القروب {chat_id}:", reply_markup=staff_group_capabilities_markup(target_uid, chat_id)); return

    if data == "owner_ai_report":
        await safe_callback_answer(q, "تمت إزالة تحليل القروبات من لوحة البوت.", show_alert=True)
        return

    if data == "monitor_groups" or data.startswith("monitor_groups_page:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "مركز المراقبة الشامل للمالك الأساسي فقط.", show_alert=True); return
        try: page = int(data.split(":", 1)[1]) if ":" in data else 0
        except ValueError: page = 0
        groups = db.execute("SELECT chat_id,title,added_at FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
        if not groups:
            await q.edit_message_text("لا توجد قروبات مسجلة بعد. أضف البوت إلى القروب واجعله يستقبل الرسائل ثم أرسل /start داخل القروب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return
        per_page = 20
        pages = max(1, (len(groups) + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        rows = []
        for chat_id, title, _added in groups[page*per_page:(page+1)*per_page]:
            archived = bool(db.execute("SELECT 1 FROM archive_settings WHERE chat_id=? AND enabled=1", (chat_id,)).fetchone())
            rows.append([InlineKeyboardButton(f"{'🟢' if archived else '⚪'} {((title or str(chat_id))[:45])}", callback_data=f"monitor_group:{chat_id}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"monitor_groups_page:{page-1}"))
        if page < pages - 1: nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"monitor_groups_page:{page+1}"))
        if nav: rows.append(nav)
        rows += [[InlineKeyboardButton("🔄 تحديث القائمة", callback_data=f"monitor_groups_page:{page}")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text(f"👁️ مراقبة القروبات المسجلة\n\nإجمالي القروبات المسجلة في قاعدة البيانات: {len(groups)}\nالصفحة {page+1} من {pages}\n🟢 = أرشفة الرسائل مفعّلة | ⚪ = الأرشفة متوقفة\nاختر قروبًا لعرض العدد الحالي للأعضاء وحالة البوت والإحصائيات.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("monitor_group:"):
        try: monitor_chat_id = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        await _show_monitor_group(q, context, monitor_chat_id); return

    if data.startswith("monitor_archive_toggle:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "تفعيل الأرشفة وإيقافها للمالك الأساسي فقط.", show_alert=True); return
        try: monitor_chat_id = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (monitor_chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        currently_on = archive_is_enabled(monitor_chat_id)
        if not currently_on:
            # Notify the group before enabling content archiving; do not silently enable it.
            try:
                await context.bot.send_message(chat_id=monitor_chat_id, text="📢 تنويه إداري: سيتم تسجيل الرسائل الجديدة لأغراض إدارة وحماية القروب ومراجعة البلاغات. يمكن للإدارة إيقاف التسجيل من إعدادات البوت.")
            except Exception as exc:
                await safe_callback_answer(q, "تعذر إرسال التنويه إلى القروب؛ لم يتم تفعيل الأرشفة. تحقق من صلاحية إرسال الرسائل.", show_alert=True)
                log.warning("تعذر تنويه أرشفة الرسائل للقروب %s: %s", monitor_chat_id, exc)
                return
            db.execute("INSERT INTO archive_settings(chat_id,enabled) VALUES(?,1) ON CONFLICT(chat_id) DO UPDATE SET enabled=1", (monitor_chat_id,))
            event = "archive_enabled_from_owner_panel"
        else:
            db.execute("UPDATE archive_settings SET enabled=0 WHERE chat_id=?", (monitor_chat_id,))
            event = "archive_disabled_from_owner_panel"
            try: await context.bot.send_message(chat_id=monitor_chat_id, text="📢 تنويه إداري: تم إيقاف تسجيل الرسائل الجديدة في أرشيف البوت.")
            except Exception as exc: log.info("تعذر إرسال تنويه إيقاف الأرشفة للقروب %s: %s", monitor_chat_id, exc)
        db.commit(); log_action(monitor_chat_id, uid, event, "owner monitoring panel")
        await _show_monitor_group(q, context, monitor_chat_id); return

    if data.startswith("monitor_archive:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "سجل الرسائل للمالك الأساسي فقط.", show_alert=True); return
        try: monitor_chat_id = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        rows = db.execute("SELECT display_name,username,message_text,created_at FROM message_archive WHERE chat_id=? ORDER BY id DESC LIMIT 10", (monitor_chat_id,)).fetchall()
        if not rows:
            body = "لا توجد رسائل مسجلة لهذا القروب. الأرشيف يسجل الرسائل الجديدة فقط بعد تفعيله."
        else:
            body = "\n\n".join(f"👤 {name or 'عضو'}" + (f" (@{username})" if username else "") + f"\n🕒 {created}\n💬 {(message_text or '[رسالة غير نصية]')[:240]}" for name,username,message_text,created in rows)
        await q.edit_message_text((f"📨 آخر الرسائل المسجلة — القروب {monitor_chat_id}\n\n" + body)[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بطاقة القروب", callback_data=f"monitor_group:{monitor_chat_id}")],[InlineKeyboardButton("🏠 الرئيسية", callback_data="home")]])); return

    if data.startswith("monitor_logs:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "سجل العمليات للمالك الأساسي فقط.", show_alert=True); return
        try: monitor_chat_id = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        rows = db.execute("SELECT action,user_id,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 15", (monitor_chat_id,)).fetchall()
        body = "\n\n".join(f"⚙️ {action} | المنفّذ: {actor}\n🕒 {created}\n📝 {(details or '')[:180]}" for action,actor,details,created in rows) or "لا توجد عمليات مسجلة لهذا القروب."
        await q.edit_message_text((f"📋 سجل عمليات القروب {monitor_chat_id}\n\n" + body)[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بطاقة القروب", callback_data=f"monitor_group:{monitor_chat_id}")],[InlineKeyboardButton("🏠 الرئيسية", callback_data="home")]])); return

    if data == "staff_center":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "إدارة الطاقم حصرية للمالك الأساسي والمالك المفوّض.", show_alert=True); return
        await q.edit_message_text("🧭 مركز إدارة الطاقم\n\nاختر خدمة لإدارة المهام والمناوبات والصلاحيات والتقييمات والاعتراضات.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 إنشاء تكليف", callback_data="staff_task_new"), InlineKeyboardButton("🗓️ تعيين مناوبة", callback_data="staff_shift_new")],
            [InlineKeyboardButton("📊 تقييم الطاقم", callback_data="staff_rating"), InlineKeyboardButton("🏅 منح وسام", callback_data="staff_badge_new")],
            [InlineKeyboardButton("🔐 صلاحيات القروبات", callback_data="staff_permissions"), InlineKeyboardButton("⚖️ الاعتراضات", callback_data="staff_appeals")],
            [InlineKeyboardButton("📨 استفسارات الطاقم", callback_data="staff_inquiries_inbox"), InlineKeyboardButton("📋 المهام", callback_data="staff_tasks")],
            [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]
        ])); return
    if data in ("staff_task_new", "staff_shift_new", "staff_badge_new"):
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذه الوظيفة للمالك الأساسي والمفوّض فقط.", show_alert=True); return
        if data == "staff_task_new":
            context.user_data["staff_task_state"] = True
            await q.message.reply_text("📋 أرسل: رقم_المشرف | عنوان المهمة | التفاصيل (اختياري). أرسل /cancel للإلغاء.")
        elif data == "staff_shift_new":
            context.user_data["staff_shift_state"] = True
            await q.message.reply_text("🗓️ أرسل: رقم_المشرف | موعد البداية | موعد النهاية (اختياري). أرسل /cancel للإلغاء.")
        else:
            context.user_data["staff_badge_state"] = True
            await q.message.reply_text("🏅 أرسل: رقم_المشرف | اسم الوسام | سبب المنح. أرسل /cancel للإلغاء.")
        return
    if data == "staff_tasks":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            rows=db.execute("SELECT id,title,status,due_at FROM staff_tasks WHERE assigned_to=? ORDER BY id DESC LIMIT 15",(uid,)).fetchall()
        else:
            rows=db.execute("SELECT id,title,status,due_at FROM staff_tasks ORDER BY id DESC LIMIT 20").fetchall()
        text="📋 المهام والتكليفات\n\n"+("\n".join(f"#{i} · {t} · {st}"+(f" · {due}" if due else "") for i,t,st,due in rows) if rows else "لا توجد مهام مسجلة.")
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ تكليف",callback_data="staff_task_new")],[InlineKeyboardButton("⬅️ مركز الطاقم",callback_data="staff_center")]])); return
    if data == "staff_rating":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q,"عرض التقييم للمالك والمفوّض فقط.",show_alert=True); return
        rows=db.execute("SELECT user_id,COUNT(*),SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) FROM staff_tasks GROUP BY user_id").fetchall()
        body="\n".join(f"• {uid2}: {done or 0}/{total} مهمة مكتملة" for uid2,total,done in rows) or "لا توجد بيانات مهام كافية للتقييم بعد."
        await q.edit_message_text("📊 تقرير إنجاز المهام (مؤشر أولي وليس حكمًا شاملًا على الأداء)\n\n"+body,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الطاقم",callback_data="staff_center")]])); return
    if data == "staff_permissions":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q,"تعديل الصلاحيات حصرية للمالك والمفوّض.",show_alert=True); return
        await q.edit_message_text("🔐 إدارة صلاحيات القروبات\n\nلإضافة مشرف وصلاحيات مفصلة نحتاج تحديد القروب والعضو والصلاحيات من داخل القروب. لا تمنح هذه الصفحة أي صلاحية تلقائيًا حتى لا تتجاوز التفويض الحالي.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الطاقم",callback_data="staff_center")]])); return
    if data == "staff_appeals":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q,"مراجعة الاعتراضات للمالك والمفوّض فقط.",show_alert=True); return
        rows=db.execute("SELECT id,chat_id,user_id,appeal_text,status FROM staff_appeals WHERE status='new' ORDER BY id DESC LIMIT 15").fetchall()
        body="\n\n".join(f"#{i} · قروب {c} · العضو {u}\n{t}" for i,c,u,t,st in rows) or "لا توجد اعتراضات جديدة. استقبال الاعتراضات يحتاج تفعيل مسار تقديمها في القروب."
        await q.edit_message_text("⚖️ مراجعة الاعتراضات\n\n"+body,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الطاقم",callback_data="staff_center")]])); return
    if data == "staff_inquiries_inbox":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q,"صندوق الطاقم للمالك والمفوّض فقط.",show_alert=True); return
        rows=db.execute("SELECT id,user_id,body,status FROM staff_inquiries WHERE status!='closed' ORDER BY id DESC LIMIT 20").fetchall()
        body="\n\n".join(f"#{i} · {u} · {st}\n{b[:400]}" for i,u,b,st in rows) or "لا توجد استفسارات طاقم مفتوحة."
        await q.edit_message_text("📨 استفسارات الطاقم\n\n"+body,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الطاقم",callback_data="staff_center")],[InlineKeyboardButton("⬅️ الرئيسية",callback_data="home")]])); return
    if data == "assistant_tools_help":
        await q.edit_message_text("🧮 أدواتي الذكية\n\n• حاسبة ونسب مئوية\n• تحويل وحدات\n• حساب العمر والتواريخ\n• تقويم هجري وميلادي\n\nاختر التقويم من القائمة الرئيسية، وستضاف الأدوات الأخرى تدريجيًا دون التأثير على بياناتك.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 التقويم الهجري والميلادي", callback_data="calendar:home")],[InlineKeyboardButton("⬅️ القائمة الرئيسية",callback_data="home")]])); return
    if data == "inquiry_start":
        if q.message.chat.type != ChatType.PRIVATE:
            await safe_callback_answer(q, "افتح البوت في الخاص لإرسال استفسار يحفظ الخصوصية.", show_alert=True); return
        context.user_data["awaiting_general_inquiry"] = True
        await q.message.reply_text("💬 اكتب استفسارك هنا في الخاص. سيصل إلى المالك الأساسي فقط، ولن يظهر في القروب. أرسل /cancel للإلغاء.")
        return
    if data == "inquiries_inbox":
        if not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "inquiries"))):
            await safe_callback_answer(q, "صندوق الاستفسارات يتطلب صلاحية من المالك الأساسي.", show_alert=True); return
        rows = db.execute("SELECT id,display_name,inquiry_text,status FROM general_inquiries WHERE status!='closed' ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            await q.edit_message_text("📨 لا توجد استفسارات مفتوحة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return
        kb = [[InlineKeyboardButton(f"#{iid} · {name or 'عضو'} · {'جديد' if status=='new' else 'تم الرد' if status=='answered' else status}", callback_data=f"inquiry_view:{iid}")] for iid,name,body,status in rows]
        kb.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
        await q.edit_message_text("📨 صندوق الاستفسارات العامة — المالك الأساسي فقط", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("inquiry_view:"):
        if not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "inquiries"))): await safe_callback_answer(q, "يتطلب صندوق الاستفسارات صلاحية ممنوحة.", show_alert=True); return
        try: iid=int(data.split(":",1)[1])
        except ValueError: return
        row=db.execute("SELECT user_id,username,display_name,inquiry_text,status,reply_text FROM general_inquiries WHERE id=?",(iid,)).fetchone()
        if not row: await safe_callback_answer(q,"الاستفسار غير موجود.",show_alert=True); return
        await q.edit_message_text(f"📨 الاستفسار #{iid}\nالحالة: {row[4]}\nالاسم: {row[2]}\nالمستخدم: @{row[1] or 'غير متوفر'}\nالمعرّف: {row[0]}\n\n{row[3]}\n\nالرد السابق: {row[5] or 'لا يوجد'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ الرد", callback_data=f"inquiry_reply:{iid}"),InlineKeyboardButton("✅ إغلاق", callback_data=f"inquiry_close:{iid}")],[InlineKeyboardButton("⬅️ الصندوق",callback_data="inquiries_inbox")]])); return
    if data.startswith("inquiry_reply:"):
        if not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "inquiries"))): await safe_callback_answer(q,"الرد يتطلب صلاحية صندوق الاستفسارات.",show_alert=True); return
        try: iid=int(data.split(":",1)[1])
        except ValueError: return
        if not db.execute("SELECT 1 FROM general_inquiries WHERE id=?",(iid,)).fetchone(): await safe_callback_answer(q,"الاستفسار غير موجود.",show_alert=True); return
        context.user_data["inquiry_reply_id"]=iid
        await q.message.reply_text(f"✍️ أرسل الآن ردك على الاستفسار #{iid} في رسالة واحدة. /cancel للإلغاء.")
        return
    if data.startswith("inquiry_close:"):
        if not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "inquiries"))): await safe_callback_answer(q,"الإغلاق يتطلب صلاحية صندوق الاستفسارات.",show_alert=True); return
        try: iid=int(data.split(":",1)[1])
        except ValueError: return
        db.execute("UPDATE general_inquiries SET status='closed' WHERE id=?",(iid,)); db.commit()
        await safe_callback_answer(q,"تم إغلاق الاستفسار.")
        try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 تم الإغلاق",callback_data="noop")]]))
        except Exception: pass
        return

    if data == "channels_manage":
        if not is_owner(uid): await safe_callback_answer(q,"إدارة ربط القنوات والنشر للمالك الأساسي فقط.",show_alert=True); return
        rows=await get_live_linked_channels(context.bot)
        summary="\n".join(f"• {title} (@{username or 'بدون معرف'}) — {cid}" for cid,title,username in rows) or "لا توجد قنوات متصلة وجاهزة للنشر."
        await q.edit_message_text("📡 إدارة القنوات والنشر\n\n"+summary+"\n\nلربط قناة: أضف البوت مشرفًا مع صلاحية النشر، ثم أرسل /addchannel @username في الخاص.\nلفك الربط: /delchannel @username\nلعرض القائمة: /channels\n\nالنشر: اختر القنوات ثم أرسل الرسالة واعتمد المعاينة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 نشر إلى القنوات",callback_data="channel_pub_start")],[InlineKeyboardButton("🔄 تحديث القائمة",callback_data="channels_manage")],[InlineKeyboardButton("⬅️ الرئيسية",callback_data="home")]])); return
    if data == "channel_pub_start":
        if not is_owner(uid): await safe_callback_answer(q,"النشر للقنوات للمالك الأساسي فقط.",show_alert=True); return
        rows=await get_live_linked_channels(context.bot)
        if not rows: await q.edit_message_text("لا توجد قنوات متصلة وجاهزة للنشر. تحقق من صلاحية البوت في القناة.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ إدارة القنوات",callback_data="channels_manage")]])); return
        context.user_data["channel_broadcast_selected"]=[]
        kb=[[InlineKeyboardButton("⬜ "+title,callback_data=f"channel_pub_toggle:{cid}")] for cid,title,username in rows[:50]]
        kb += [[InlineKeyboardButton("🌐 تحديد جميع القنوات",callback_data="channel_pub_all")],[InlineKeyboardButton("➡️ متابعة",callback_data="channel_pub_prepare")],[InlineKeyboardButton("❌ إلغاء",callback_data="channel_pub_cancel")]]
        await q.edit_message_text("📡 حدد القنوات التي تريد النشر فيها:",reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("channel_pub_toggle:"):
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        try: cid=int(data.split(":",1)[1])
        except ValueError: return
        selected=set(context.user_data.get("channel_broadcast_selected",[]))
        if cid in selected: selected.remove(cid)
        else: selected.add(cid)
        context.user_data["channel_broadcast_selected"]=sorted(selected)
        rows=await get_live_linked_channels(context.bot)
        live_ids={x for x,_,_ in rows}
        selected.intersection_update(live_ids)
        context.user_data["channel_broadcast_selected"]=sorted(selected)
        kb=[[InlineKeyboardButton(("✅ " if x in selected else "⬜ ")+title,callback_data=f"channel_pub_toggle:{x}")] for x,title,username in rows[:50]]
        kb += [[InlineKeyboardButton("🌐 تحديد جميع القنوات",callback_data="channel_pub_all")],[InlineKeyboardButton("➡️ متابعة",callback_data="channel_pub_prepare")],[InlineKeyboardButton("❌ إلغاء",callback_data="channel_pub_cancel")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return
    if data == "channel_pub_all":
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        rows=await get_live_linked_channels(context.bot)
        context.user_data["channel_broadcast_selected"]=[cid for cid,title,username in rows]
        kb=[[InlineKeyboardButton("✅ "+title,callback_data=f"channel_pub_toggle:{cid}")] for cid,title,username in rows[:50]]
        kb += [[InlineKeyboardButton("➡️ متابعة",callback_data="channel_pub_prepare")],[InlineKeyboardButton("❌ إلغاء",callback_data="channel_pub_cancel")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return
    if data == "channel_pub_prepare":
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        live_rows=await get_live_linked_channels(context.bot)
        live_ids={cid for cid,title,username in live_rows}
        selected=[cid for cid in context.user_data.get("channel_broadcast_selected",[]) if cid in live_ids]
        context.user_data["channel_broadcast_selected"]=selected
        if not selected: await safe_callback_answer(q,"لا توجد قناة متصلة وجاهزة للنشر ضمن اختيارك.",show_alert=True); return
        context.user_data["awaiting_channel_broadcast"]=True
        await q.edit_message_text(f"أرسل الآن في الخاص الرسالة أو الصورة أو الفيديو الذي تريد نشره في {len(selected)} قناة متصلة. ستظهر معاينة قبل النشر.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء",callback_data="channel_pub_cancel")]])); return
    if data == "channel_pub_confirm":
        if not is_owner(uid): await safe_callback_answer(q,"للمالك الأساسي فقط.",show_alert=True); return
        draft=context.user_data.get("channel_broadcast_draft")
        selected=context.user_data.get("channel_broadcast_selected",[])
        if not draft or not selected: await q.edit_message_text("انتهت صلاحية مسودة النشر. ابدأ من جديد."); return
        sent=failed=0
        live_rows=await get_live_linked_channels(context.bot)
        live_ids={cid for cid,title,username in live_rows}
        selected=[cid for cid in selected if cid in live_ids]
        for cid in selected:
            try: await context.bot.copy_message(chat_id=cid,from_chat_id=draft["chat_id"],message_id=draft["message_id"]); sent+=1
            except Exception as exc: failed+=1; log.warning("Channel publish failed %s: %s",cid,exc)
        for key in ("channel_broadcast_draft","channel_broadcast_selected","awaiting_channel_broadcast"): context.user_data.pop(key,None)
        await q.edit_message_text(f"✅ انتهى النشر للقنوات.\nنجح: {sent}\nتعذر: {failed}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📡 إدارة القنوات",callback_data="channels_manage")],[InlineKeyboardButton("⬅️ الرئيسية",callback_data="home")]])); return
    if data == "channel_pub_cancel":
        for key in ("channel_broadcast_draft","channel_broadcast_selected","awaiting_channel_broadcast"): context.user_data.pop(key,None)
        await q.edit_message_text("تم إلغاء النشر للقنوات.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📡 إدارة القنوات",callback_data="channels_manage")],[InlineKeyboardButton("⬅️ الرئيسية",callback_data="home")]])); return

    # نشر آخر نسخة من المستودع على Render متاح للمالك الأساسي فقط.
    if data in ("update_manager", "update_deploy_latest"):
        if not is_owner(uid):
            await q.edit_message_text("⛔ إدارة تحديثات البوت متاحة للمالك الأساسي فقط.")
            return
        if data == "update_manager":
            configured = bool(RENDER_API_KEY and RENDER_SERVICE_ID)
            status_text = (
                "🟢 الربط مع Render جاهز.\nيمكنك نشر آخر commit موجود في GitHub؛ هذا لا يرفع تعديلات محلية غير منشورة."
                if configured else
                "🟠 الربط غير مكتمل. أضف RENDER_API_KEY وRENDER_SERVICE_ID إلى متغيرات البيئة في Render، ثم أعد تشغيل الخدمة.\n"
                "احفظ المفتاح كمتغير سري ولا ترسله في المحادثة."
            )
            await q.edit_message_text(
                "🔄 إدارة تحديثات البوت\n\n"
                + status_text
                + "\n\nقبل النشر: ارفع الكود المعدل إلى GitHub. زر النشر سيطلب من Render تشغيل آخر نسخة من المستودع."
                + "\nملاحظة: قد يعاد تشغيل البوت أثناء النشر.",
                reply_markup=update_manager_markup(),
            )
            return
        try:
            await q.edit_message_text("⏳ جارٍ طلب نشر آخر نسخة من Render…")
            status_code, deploy_id = await _render_trigger_latest_deploy()
            suffix = f"\nمعرّف النشر: {html.escape(str(deploy_id))}" if deploy_id else ""
            await q.edit_message_text(
                "✅ استقبل Render طلب النشر بنجاح.\n"
                "انتظر اكتمال النشر، ثم تحقق من سجل الخدمة وحالة البوت."
                + suffix,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 إدارة التحديثات", callback_data="update_manager")],
                                                   [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]),
            )
        except Exception as exc:
            log.warning("Render deploy request failed: %s", exc)
            await q.edit_message_text(
                "❌ لم يتم طلب النشر. تحقق من RENDER_API_KEY وRENDER_SERVICE_ID وصلاحيات المفتاح في Render.\n"
                + html.escape(str(exc))[:800],
                reply_markup=update_manager_markup(),
                parse_mode="HTML",
            )
        return

    # استدعاء التاق: المالك الأساسي والمفوّض لهما الصلاحية نفسها على القروبات المسجلة.
    if data.startswith(("owner_tag_groups", "owner_tag_toggle:", "owner_tag_all", "owner_tag_send")):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "tag")):
            await safe_callback_answer(q, "لا تملك صلاحية استدعاء التاق؛ اطلب منحها من المالك الأساسي.", show_alert=True)
            return
        allowed_tag_ids = capability_group_ids(uid, "tag")
        groups = [(cid, title) for cid, title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if cid in allowed_tag_ids]
        if data == "owner_tag_groups":
            context.user_data["owner_tag_selected"] = []
            rows = [[InlineKeyboardButton(f"⬜ {title or cid}", callback_data=f"owner_tag_toggle:{cid}")] for cid, title in groups[:50]]
            rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات", callback_data="owner_tag_all")])
            rows.append([InlineKeyboardButton("📣 منشن الأعضاء في القروبات المحددة", callback_data="owner_tag_send")])
            rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
            await q.edit_message_text("📣 المنشن الجماعي للأعضاء\nاختر قروبًا أو عدة قروبات، أو حدّد جميع القروبات المسجلة. سيُمنشن الأعضاء الذين حفظ البوت معرّفاتهم في قاعدة البيانات فقط؛ تيليجرام لا يتيح للبوت جلب قائمة كاملة بكل أعضاء القروب مباشرةً.", reply_markup=InlineKeyboardMarkup(rows))
            return
        if data.startswith("owner_tag_toggle:"):
            try:
                cid = int(data.split(":",1)[1])
            except ValueError:
                await safe_callback_answer(q, "معرّف قروب غير صحيح.", show_alert=True); return
            if not any(row[0] == cid for row in groups) or not can_use_capability(uid, "tag", cid):
                await safe_callback_answer(q, "القروب غير مسجل أو لا تملك صلاحية التاق فيه.", show_alert=True); return
            selected = context.user_data.setdefault("owner_tag_selected", [])
            if cid in selected: selected.remove(cid)
            else: selected.append(cid)
            rows = [[InlineKeyboardButton(("✅ " if gid in selected else "⬜ ") + (title or str(gid)), callback_data=f"owner_tag_toggle:{gid}")] for gid, title in groups[:50]]
            rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات", callback_data="owner_tag_all")])
            rows.append([InlineKeyboardButton(f"📣 إرسال التاق ({len(selected)})", callback_data="owner_tag_send")])
            rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
            return
        if data == "owner_tag_all":
            context.user_data["owner_tag_selected"] = [cid for cid, _ in groups]
            rows = [[InlineKeyboardButton("✅ " + (title or str(cid)), callback_data=f"owner_tag_toggle:{cid}")] for cid, title in groups[:50]]
            rows.append([InlineKeyboardButton("📣 إرسال التاق للجميع", callback_data="owner_tag_send")])
            rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
            return
        if data == "owner_tag_send":
            selected = context.user_data.get("owner_tag_selected", [])
            selected = [cid for cid in selected if any(row[0] == cid for row in groups) and can_use_capability(uid, "tag", cid)]
            if not selected:
                await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل.", show_alert=True); return
            sent_groups = failed_groups = mentioned_total = 0
            empty_groups = 0
            for cid in selected:
                members = db.execute(
                    "SELECT user_id,display_name FROM member_activity WHERE chat_id=? ORDER BY display_name COLLATE NOCASE",
                    (cid,)
                ).fetchall()
                # Telegram limits a message to 4096 characters; split mentions into safe chunks.
                mentions = []
                seen_ids = set()
                for member_id, display_name in members:
                    if not member_id or member_id in seen_ids:
                        continue
                    seen_ids.add(member_id)
                    label = html.escape((display_name or "عضو").replace("\n", " ")[:80])
                    mentions.append(f'<a href="tg://user?id={member_id}">{label}</a>')
                if not mentions:
                    empty_groups += 1
                    continue
                # تقسيم تلقائي حسب حد طول رسالة تيليجرام، دون قطع روابط المنشن.
                chunks = split_mentions_for_telegram(mentions)
                try:
                    for chunk in chunks:
                        await context.bot.send_message(chat_id=cid, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
                        await asyncio.sleep(0.35)
                    sent_groups += 1
                    mentioned_total += len(mentions)
                except Exception as exc:
                    failed_groups += 1
                    log.warning("Mass mention failed for group %s: %s", cid, exc)
            context.user_data.pop("owner_tag_selected", None)
            await q.edit_message_text(
                "📣 انتهت محاولة المنشن الجماعي.\n"
                f"✅ القروبات التي وصلها المنشن: {sent_groups}\n"
                f"👤 إجمالي المنشنات المرسلة: {mentioned_total}\n"
                f"⚠️ قروبات تعذر الإرسال فيها: {failed_groups}\n"
                f"ℹ️ قروبات بلا أعضاء مسجلين في قاعدة البيانات: {empty_groups}\n\n"
                "ملاحظة: المنشن يشمل الأعضاء الذين يعرفهم البوت من قاعدة بياناته، وليس بالضرورة جميع أعضاء القروب الحاليين.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 استدعاء آخر", callback_data="owner_tag_groups")],[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])
            )
            return

    if data == "panel_tag_locked":
        await safe_callback_answer(q, "🔐 اطلب من المالك الأساسي منحك صلاحية tag لهذا القروب عبر /grantcap USER_ID tag CHAT_ID.", show_alert=True)
        return
    if data == "panel_links_locked":
        await safe_callback_answer(q, "🔐 فتح الروابط للأعضاء متاح للمالك الأساسي أو لمن منحه صلاحية الحماية.", show_alert=True)
        return

    # لوحة القروب المحلية: كل callback يعيد التحقق من كون المستخدم مشرف Telegram في القروب نفسه.
    if data.startswith(("group_panel_home:", "group_panel_settings:", "group_panel_admin:", "group_panel_roles:", "group_panel_logs:", "group_panel_stats:", "group_panel_toggle_links:", "group_panel_tag:")):
        try:
            action, chat_raw = data.split(":", 1)
            group_chat_id = int(chat_raw)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "بيانات القروب غير صحيحة.", show_alert=True)
            return
        if not (is_owner(uid) or is_delegated_owner(uid) or await is_telegram_group_admin(context.bot, uid, group_chat_id)):
            await safe_callback_answer(q, "هذه اللوحة متاحة للمالك والمفوّض ومشرفي هذا القروب فقط.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (group_chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل بعد. أرسل /start داخله أولًا.", show_alert=True)
            return
        ext_info = external_group_info(group_chat_id)
        if ext_info and ext_info[0] and not is_owner(uid) and not ext_info[1]:
            await safe_callback_answer(q, "أوقف المالك الأساسي خدمات البوت عن هذا القروب.", show_alert=True); return
        if ext_info and ext_info[0] and not is_owner(uid):
            required_cap = {
                "group_panel_toggle_links": "protection", "group_panel_settings": "settings",
                "group_panel_admin": "members", "group_panel_roles": "members",
                "group_panel_stats": "stats", "group_panel_logs": "stats", "group_panel_tag": "tag"
            }.get(action)
            if required_cap and not external_group_cap_enabled(group_chat_id, required_cap):
                await safe_callback_answer(q, "هذه الخدمة غير مفعّلة لهذا القروب من المالك الأساسي.", show_alert=True); return
        if action == "group_panel_toggle_links":
            current = get_settings(group_chat_id).get("links", 0)
            if current and not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "protection", group_chat_id))):
                await safe_callback_answer(q, "فتح الروابط للأعضاء يتطلب صلاحية المالك أو تفويض الحماية.", show_alert=True); return
            set_setting(group_chat_id, "links", 0 if current else 1)
            log_action(group_chat_id, uid, "setting", f"links={0 if current else 1}")
            title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (group_chat_id,)).fetchone()
            await q.edit_message_text(
                f"🛡️ لوحة إدارة القروب: {html.escape((title_row[0] if title_row else str(group_chat_id)) or str(group_chat_id))}\n\n"
                f"تم {'فتح' if current else 'تفعيل منع'} الروابط.\n"
                "ملاحظة: يجب أن يكون البوت مشرفًا مع صلاحية حذف الرسائل ليحذف الروابط المخالفة.",
                parse_mode="HTML", reply_markup=group_owner_panel_markup(group_chat_id, uid))
            return
        if action == "group_panel_tag":
            ext_row = external_group_info(group_chat_id)
            external_tag_allowed = bool(ext_row and ext_row[0] and external_group_cap_enabled(group_chat_id, "tag")
                                        and await is_telegram_group_admin(context.bot, uid, group_chat_id))
            if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "tag", group_chat_id) or external_tag_allowed):
                await safe_callback_answer(q, "⛔ صلاحية التاق غير ممنوحة لك من المالك الأساسي.", show_alert=True)
                return
            members = db.execute(
                "SELECT user_id,display_name FROM member_activity WHERE chat_id=? ORDER BY display_name COLLATE NOCASE",
                (group_chat_id,)
            ).fetchall()
            mentions, seen_ids = [], set()
            for member_id, display_name in members:
                if not member_id or member_id in seen_ids:
                    continue
                seen_ids.add(member_id)
                label = html.escape((display_name or "عضو").replace("\n", " ")[:80])
                mentions.append(f'<a href="tg://user?id={member_id}">{label}</a>')
            if not mentions:
                await safe_callback_answer(q, "لا توجد معرّفات أعضاء مسجلة لهذا القروب بعد.", show_alert=True)
                return
            # تقسيم تلقائي حسب حد طول رسالة تيليجرام، دون قطع روابط المنشن.
            chunks = split_mentions_for_telegram(mentions)
            try:
                for chunk in chunks:
                    await context.bot.send_message(chat_id=group_chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
                    await asyncio.sleep(0.35)
            except Exception as exc:
                log.warning("Local group mass mention failed for %s: %s", group_chat_id, exc)
                await safe_callback_answer(q, "تعذر إكمال الإرسال. تحقق من صلاحيات البوت وحدود تيليجرام.", show_alert=True)
                return
            await safe_callback_answer(q, f"تم إرسال {len(mentions)} منشن مسجل على {len(chunks)} رسالة تلقائيًا.", show_alert=True)
            return
        if action == "group_panel_home":
            title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (group_chat_id,)).fetchone()
            await q.edit_message_text(
                f"🛡️ لوحة القروب: {html.escape((title_row[0] if title_row else str(group_chat_id)) or str(group_chat_id))}\n\n"
                "هذه لوحة محلية بصلاحيات القروب فقط؛ لا تتيح إدارة مالك البوت أو القروبات الأخرى.",
                parse_mode="HTML", reply_markup=group_owner_panel_markup(group_chat_id, uid))
            return
        if action == "group_panel_settings":
            await q.edit_message_text(settings_text(0), reply_markup=settings_page_markup(group_chat_id, 0, f"group_panel_home:{group_chat_id}"))
            return
        if action == "group_panel_roles":
            await q.edit_message_text(
                "🪪 إدارة الرتب والصلاحيات لهذا القروب فقط\n\n"
                "لترقية عضو: أرسل أمرًا بالرد على رسالته داخل القروب، مثل:\n"
                "• رفع مشرف\n• رفع مشرف عام\n• رفع مدير\n• رفع مدير عام\n\n"
                "لإزالة رتبته: رد على رسالة العضو واكتب: تنزيل رتبة\n\n"
                "الرتب محلية داخل هذا البوت ولا تغيّر صلاحيات تيليجرام الأصلية. "
                "لا يمكن منح رتبة مالك البوت أو المالك المفوّض من خلال هذه الأوامر.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لوحة القروب", callback_data=f"group_panel_home:{group_chat_id}")]])
            )
            return
        if action == "group_panel_admin":
            await q.edit_message_text(
                "👥 إدارة أعضاء هذا القروب فقط\nاختر إجراءً. سيطلب البوت رقم المستخدم أو رقم الرسالة في الخاص، ولا ينفّذ الإجراء إلا إذا كانت صلاحياته في Telegram تسمح بذلك.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 حظر عضو", callback_data=f"admin_action:ban:{group_chat_id}"), InlineKeyboardButton("🔓 فك الحظر", callback_data=f"admin_action:unban:{group_chat_id}")],
                    [InlineKeyboardButton("👢 طرد عضو", callback_data=f"admin_action:kick:{group_chat_id}"), InlineKeyboardButton("🔇 كتم عضو", callback_data=f"admin_action:mute:{group_chat_id}")],
                    [InlineKeyboardButton("🔊 إلغاء الكتم", callback_data=f"admin_action:unmute:{group_chat_id}")],
                    [InlineKeyboardButton("🗑️ حذف رسالة", callback_data=f"admin_action:delete:{group_chat_id}"), InlineKeyboardButton("📌 تثبيت رسالة", callback_data=f"admin_action:pin:{group_chat_id}")],
                    [InlineKeyboardButton("⬅️ لوحة القروب", callback_data=f"group_panel_home:{group_chat_id}")],
                ]))
            return
        if action == "group_panel_logs":
            rows = db.execute("SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 20", (group_chat_id,)).fetchall()
            title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (group_chat_id,)).fetchone()
            body = "\n".join(f"• {created} — {action}: {details or ''}" for action, details, created in rows) or "لا توجد عمليات مسجلة بعد."
            await q.edit_message_text(f"📋 سجلات القروب: {title_row[0] if title_row else group_chat_id}\n\n{body}"[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لوحة القروب", callback_data=f"group_panel_home:{group_chat_id}")]]))
            return
        if action == "group_panel_stats":
            total = db.execute("SELECT COUNT(*) FROM logs WHERE chat_id=?", (group_chat_id,)).fetchone()[0]
            members = db.execute("SELECT COUNT(*) FROM member_activity WHERE chat_id=?", (group_chat_id,)).fetchone()[0]
            try: member_count = await context.bot.get_chat_member_count(group_chat_id)
            except Exception: member_count = "غير متاح"
            title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (group_chat_id,)).fetchone()
            await q.edit_message_text(
                f"📊 إحصائيات القروب: {title_row[0] if title_row else group_chat_id}\n\n👥 عدد الأعضاء: {member_count}\n👤 أعضاء تم رصد نشاطهم: {members}\n📋 العمليات المسجلة: {total}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لوحة القروب", callback_data=f"group_panel_home:{group_chat_id}")]]))
            return

    if data == "assistant_ai_help":
        await q.message.reply_text("🤖 المساعد الذكي\n\nاستخدم /ai متبوعًا بسؤالك في الخاص، أو داخل القروب وفق صلاحياتك وإعدادات البوت. يتطلب الاتصال ضبط OPENAI_API_KEY في بيئة التشغيل؛ لا ترسل المفتاح في المحادثات. للتحقق من حالة الإعداد استخدم /aistatus.")
        return
    if data == "assistant_notes_help":
        await q.message.reply_text("📝 إدارة الملاحظات من البوت: استخدم /notes لعرض الملاحظات و/note لإضافة ملاحظة أو تعديلها وفق التعليمات.")
        return
    if data == "assistant_reminder_help":
        await q.message.reply_text("⏰ إدارة التذكيرات من البوت: /remind لإضافة تذكير، /reminders لعرضها، و/delreminder لحذف تذكير.")
        return
    if data == "assistant_tasks_help":
        await q.message.reply_text("✅ إدارة المهام من البوت: /task لإضافة مهمة، /tasks لعرضها، /donetask لإتمامها، و/deltask لحذفها.")
        return
    if data == "assistant_expenses_help":
        await q.message.reply_text("💰 المصاريف:\n/expense المبلغ | الوصف — تسجيل مصروف\n/expenses — عرض آخر المصاريف وإجمالي الشهر")
        return
    if data == "assistant_summary_help":
        await q.message.reply_text("📄 للتلخيص المحلي اكتب /summarize ثم النص، أو أرسل ملف PDF أو DOCX أو TXT في الخاص. يجري استخراج بعض الجمل من النص دون ذكاء اصطناعي أو إرسالها إلى خدمة خارجية.")
        return
    if data == "assistant_quiz_help":
        await q.message.reply_text("🎓 اكتب /quiz ثم اسم الموضوع للحصول على قالب أسئلة مراجعة محلي. القالب عام وليس أسئلة مولّدة أو رسمية.")
        return
    if data == "assistant_privacy":
        await q.message.reply_text("🔒 تُحفظ ملاحظاتك ومهامك ومواعيدك ومصاريفك في قاعدة بيانات البوت مرتبطة بمعرّف حسابك. لا توجد خدمة ذكاء اصطناعي أو إرسال للنصوص إلى مزود خارجي في هذا الإصدار. استخدم أوامر الحذف المتاحة لإزالة سجلاتك.")
        return
    if data == "student_schedule_help":
        await q.message.reply_text("🎓 الجدول الدراسي من البوت: /studyadd لإضافة جلسة مذاكرة، /studyschedule لعرض الجدول، و/delstudy لحذف جلسة.")
        return
    if data == "student_events_help":
        await q.message.reply_text("🔔 إدارة الاختبارات والمواعيد من البوت: /exam لإضافة موعد، /exams لعرض المواعيد، و/delexam للحذف.")
        return
    if data == "assistant_group_panel":
        if can_use_panel(uid):
            await panel(update, context)
        else:
            await q.message.reply_text("🛡️ إدارة القروبات متاحة للمصرح لهم فقط. أضف البوت إلى قروبك وامنحه صلاحيات المشرف المطلوبة، ثم اتبع تعليمات التسجيل.")
        return

    if data.startswith("reentry_warn:"):
        try: _chat_scope = int(data.split(":", 2)[1])
        except Exception: _chat_scope = 0
        if not (is_owner(uid) or can_use_capability(uid, "reentry", _chat_scope)):
            await safe_callback_answer(q, "تحتاج إلى صلاحية مراجعة المطرودين من المالك الأساسي.", show_alert=True)
            return
        try:
            _, chat_raw, user_raw = data.split(":", 2)
            target_chat, target_user = int(chat_raw), int(user_raw)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "بيانات الإنذار غير صحيحة.", show_alert=True)
            return
        restriction = db.execute("SELECT display_name, username, status FROM reentry_restrictions WHERE chat_id=? AND user_id=?", (target_chat, target_user)).fetchone()
        if not restriction:
            await safe_callback_answer(q, "لا يوجد سجل عودة لهذا العضو.", show_alert=True)
            return
        row = db.execute("SELECT warning_count FROM member_security_warnings WHERE chat_id=? AND user_id=?", (target_chat, target_user)).fetchone()
        count = row[0] if row else 0
        if count >= 3:
            await safe_callback_answer(q, "وصل الحد الأعلى: 3 إنذارات.", show_alert=True)
            return
        count += 1
        db.execute("""INSERT INTO member_security_warnings(chat_id,user_id,warning_count,last_reason,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
            warning_count=excluded.warning_count,last_reason=excluded.last_reason,updated_at=excluded.updated_at""",
            (target_chat, target_user, count, restriction[2] or "مغادرة/طرد", now()))
        db.commit()
        log_action(target_chat, target_user, "member_security_warning", f"count={count}; by={uid}")
        await safe_callback_answer(q, f"تم تسجيل الإنذار {count}/3")
        try:
            await q.edit_message_text((q.message.text or "") + f"\n\n⚠️ الإنذارات المسجلة: {count}/3\nسجّل الإنذار: {uid}")
        except Exception as exc:
            log.info("تعذر تحديث رسالة الإنذار: %s", exc)
        if count == 3:
            alert = (f"🚨 بلغ العضو الحد الأعلى من الإنذارات (3/3).\nالقروب: {target_chat}\n"
                     f"الاسم: {restriction[0] or target_user}\nاسم المستخدم: @{restriction[1] or 'غير متوفر'}\n"
                     f"المعرّف الثابت: {target_user}\nيرجى مراجعة طلب العودة واتخاذ القرار.")
            recipients = {OWNER_ID} if OWNER_ID else set()
            recipients.update(r[0] for r in db.execute("SELECT user_id FROM delegated_owners").fetchall())
            for recipient in recipients:
                try:
                    await context.bot.send_message(chat_id=recipient, text=alert)
                except Exception as exc:
                    log.info("تعذر إرسال تنبيه الإنذارات إلى %s: %s", recipient, exc)
        return

    if data.startswith(("reentry_approve:", "reentry_keep:")):
        try:
            action, chat_raw, user_raw = data.split(":", 2)
            target_chat, target_user = int(chat_raw), int(user_raw)
        except (ValueError, TypeError):
            return
        if not (is_owner(uid) or can_use_capability(uid, "reentry", target_chat)):
            await safe_callback_answer(q, "تحتاج إلى صلاحية مراجعة المطرودين لهذا القروب.", show_alert=True)
            return
        row = db.execute("SELECT display_name, username, status FROM reentry_restrictions WHERE chat_id=? AND user_id=?", (target_chat, target_user)).fetchone()
        if not row:
            return
        if action == "reentry_approve":
            try:
                await context.bot.unban_chat_member(chat_id=target_chat, user_id=target_user, only_if_banned=True)
                db.execute("UPDATE reentry_restrictions SET status='approved' WHERE chat_id=? AND user_id=?", (target_chat, target_user))
                db.commit()
                log_action(target_chat, target_user, "reentry_approved", f"approved_by={uid}")
                await q.edit_message_text((q.message.text or "") + f"\n\n✅ تمت الموافقة على عودة العضو بواسطة: {uid}\nيمكنه العودة عبر رابط الدعوة أو رابط القروب.")
            except Exception as exc:
                log.warning("تعذرت الموافقة على عودة %s إلى %s: %s", target_user, target_chat, exc)
                await q.edit_message_text((q.message.text or "") + "\n\n⚠️ تعذر رفع الحظر. تأكد أن البوت مشرف ولديه صلاحية الحظر.")
        else:
            db.execute("UPDATE reentry_restrictions SET status='blocked' WHERE chat_id=? AND user_id=?", (target_chat, target_user))
            db.commit()
            log_action(target_chat, target_user, "reentry_denied", f"kept_by={uid}")
            await q.edit_message_text((q.message.text or "") + f"\n\n⛔ تقرر إبقاء المنع بواسطة: {uid}")
        return

    if data == "owner_autoreplies":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا القسم للمالك أو المالك المفوّض فقط.", show_alert=True); return
        groups = await accessible_groups(context.bot, uid)
        await q.edit_message_text("💬 إدارة الردود الآلية\n\nاختر القروب الذي تريد إضافة ردوده أو تعديلها. التغييرات تحفظ في قاعدة البيانات وتعمل تلقائيًا داخل القروب.", reply_markup=owner_autoreply_groups_markup(groups)); return

    if data == "owner_ar_all":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا القسم للمالك أو المالك المفوّض فقط.", show_alert=True); return
        groups = await accessible_groups(context.bot, uid)
        enabled_count = db.execute("SELECT COUNT(*) FROM auto_reply_settings WHERE enabled=1").fetchone()[0]
        rows = [
            [InlineKeyboardButton("➕ إضافة/تعميم رد على كل القروبات", callback_data="owner_ar_add_all")],
            [InlineKeyboardButton("✏️ تعديل عبارة ورد في كل القروبات", callback_data="owner_ar_edit_all")],
            [InlineKeyboardButton("🗑️ حذف عبارة من كل القروبات", callback_data="owner_ar_delete_all")],
            [InlineKeyboardButton("🔄 تشغيل الردود في كل القروبات", callback_data="owner_ar_on_all"),
             InlineKeyboardButton("⏸️ إيقاف الردود في كل القروبات", callback_data="owner_ar_off_all")],
            [InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="owner_autoreplies")],
        ]
        await q.edit_message_text(
            f"🌐 الإدارة الموحّدة للردود الآلية\n\n"
            f"القروبات المسجلة المتاحة: {len(groups)}\n"
            f"القروبات التي تظهر فيها الردود مفعّلة حاليًا (وفق قاعدة البيانات): {enabled_count}\n\n"
            "أي إضافة أو تعديل هنا يُطبّق على جميع القروبات المسجلة دفعة واحدة، "
            "ويُمنح المالك المفوّض الصلاحية نفسها لهذا القسم.",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data in ("owner_ar_add_all", "owner_ar_edit_all"):
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا القسم للمالك أو المالك المفوّض فقط.", show_alert=True); return
        context.user_data["owner_autoreply_state"] = {
            "action": "add_all" if data == "owner_ar_add_all" else "edit_all",
            "chat_id": None
        }
        await q.edit_message_text(
            "أرسل العبارة والرد بهذا الشكل ليتم تعميمهما على جميع القروبات المسجلة:\n"
            "العبارة | الرد الذي سيظهر في القروب\n\n"
            "مثال: وين البوت؟ | أنا بوت حماية القروبات 🛡️\n"
            "في وضع التعديل يجب أن تكون العبارة موجودة مسبقًا في قروب واحد على الأقل.\n"
            "أرسل /cancel للإلغاء.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ إلغاء", callback_data="owner_ar_all")]]))
        return

    if data == "owner_ar_delete_all":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا القسم للمالك أو المالك المفوّض فقط.", show_alert=True); return
        context.user_data["owner_autoreply_state"] = {"action": "delete_all", "chat_id": None}
        await q.edit_message_text(
            "أرسل العبارة المطابقة تمامًا لحذفها من جميع القروبات المسجلة.\n"
            "أرسل /cancel للإلغاء.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ إلغاء", callback_data="owner_ar_all")]]))
        return

    if data in ("owner_ar_on_all", "owner_ar_off_all"):
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "هذا القسم للمالك أو المالك المفوّض فقط.", show_alert=True); return
        groups = db.execute("SELECT chat_id FROM watched_groups").fetchall()
        enabled = 1 if data == "owner_ar_on_all" else 0
        for (group_id,) in groups:
            ensure_auto_replies(group_id)
            db.execute("UPDATE auto_reply_settings SET enabled=? WHERE chat_id=?", (enabled, group_id))
        db.commit()
        await safe_callback_answer(q, f"تم {'تشغيل' if enabled else 'إيقاف'} الردود في {len(groups)} قروب.")
        await q.edit_message_text(
            f"✅ تم {'تشغيل' if enabled else 'إيقاف'} الردود الآلية في جميع القروبات المسجلة ({len(groups)}).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ إدارة الردود الموحدة", callback_data="owner_ar_all")],
                                               [InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="owner_autoreplies")]]))
        return

    if data.startswith("owner_ar_group:"):
        if not (is_owner(uid) or is_delegated_owner(uid)): await safe_callback_answer(q, "للمالك أو المالك المفوّض فقط.", show_alert=True); return
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        ensure_auto_replies(chat_id)
        state = db.execute("SELECT enabled FROM auto_reply_settings WHERE chat_id=?", (chat_id,)).fetchone()
        rules = db.execute("SELECT trigger_text,reply_text,enabled FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = title_row[0] if title_row else str(chat_id)
        lines = [f"💬 الردود الآلية — {title}", f"الحالة العامة: {'🟢 مفعّلة' if state and state[0] else '⏸️ متوقفة'}", ""]
        lines += [f"{'✅' if enabled else '⏸️'} {trigger} ← {reply}" for trigger,reply,enabled in rules[:35]]
        rows, buttons = owner_autoreply_group_markup(chat_id)
        await q.edit_message_text("\n".join(lines)[:3500], reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith(("owner_ar_add:", "owner_ar_edit:")):
        if not (is_owner(uid) or is_delegated_owner(uid)): await safe_callback_answer(q, "للمالك أو المالك المفوّض فقط.", show_alert=True); return
        action, raw_id = data.split(":",1)
        try: chat_id = int(raw_id)
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        context.user_data["owner_autoreply_state"] = {"action": "add" if action.endswith("add") else "edit", "chat_id": chat_id}
        prompt = "أرسل الآن العبارة والرد بهذا الشكل:\nالعبارة | الرد الذي سيظهر في القروب\n\nمثال: وين البوت؟ | أنا بوت حماية القروبات 🛡️\nاكتب /cancel للإلغاء."
        await q.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data=f"owner_ar_group:{chat_id}")]])); return

    if data.startswith("owner_ar_delete:"):
        if not (is_owner(uid) or is_delegated_owner(uid)): await safe_callback_answer(q, "للمالك أو المالك المفوّض فقط.", show_alert=True); return
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        rules = db.execute("SELECT trigger_text FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
        rows = [[InlineKeyboardButton(f"🗑️ {trigger[:50]}", callback_data=f"owner_ar_delone:{chat_id}:{i}")] for i,(trigger,) in enumerate(rules[:40])]
        context.user_data["owner_ar_delete_choices"] = {"chat_id": chat_id, "triggers": [r[0] for r in rules[:40]]}
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"owner_ar_group:{chat_id}")])
        await q.edit_message_text("اختر الرد الذي تريد حذفه:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("owner_ar_delone:"):
        if not (is_owner(uid) or is_delegated_owner(uid)): await safe_callback_answer(q, "للمالك أو المالك المفوّض فقط.", show_alert=True); return
        try:
            _, chat_raw, index_raw = data.split(":",2); chat_id=int(chat_raw); idx=int(index_raw)
            choices=context.user_data.get("owner_ar_delete_choices", {})
            trigger=choices["triggers"][idx]
            if choices.get("chat_id") != chat_id: raise ValueError()
        except (ValueError, KeyError, IndexError): await safe_callback_answer(q, "انتهت صلاحية القائمة. افتحها مجددًا.", show_alert=True); return
        db.execute("DELETE FROM auto_replies WHERE chat_id=? AND trigger_text=?", (chat_id, trigger)); db.commit()
        log_action(chat_id, uid, "auto_reply_deleted", trigger)
        await safe_callback_answer(q, "تم حذف الرد.")
        context.user_data.pop("owner_ar_delete_choices", None)
        state = db.execute("SELECT enabled FROM auto_reply_settings WHERE chat_id=?", (chat_id,)).fetchone()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title=title_row[0] if title_row else str(chat_id)
        rules = db.execute("SELECT trigger_text,reply_text,enabled FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
        lines=[f"💬 الردود الآلية — {title}", f"الحالة العامة: {'🟢 مفعّلة' if state and state[0] else '⏸️ متوقفة'}", ""] + [f"{'✅' if en else '⏸️'} {tr} ← {rep}" for tr,rep,en in rules[:35]]
        _, buttons=owner_autoreply_group_markup(chat_id)
        await q.edit_message_text("\n".join(lines)[:3500], reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith(("owner_ar_on:", "owner_ar_off:")):
        if not (is_owner(uid) or is_delegated_owner(uid)): await safe_callback_answer(q, "للمالك أو المالك المفوّض فقط.", show_alert=True); return
        action, raw_id=data.split(":",1)
        try: chat_id=int(raw_id)
        except ValueError: await safe_callback_answer(q, "رقم القروب غير صحيح.", show_alert=True); return
        ensure_auto_replies(chat_id)
        enabled = 1 if action.endswith("on") else 0
        db.execute("UPDATE auto_reply_settings SET enabled=? WHERE chat_id=?", (enabled,chat_id)); db.commit()
        await safe_callback_answer(q, "تم تشغيل الردود." if enabled else "تم إيقاف الردود.")
        title_row=db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title=title_row[0] if title_row else str(chat_id)
        rules=db.execute("SELECT trigger_text,reply_text,enabled FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
        lines=[f"💬 الردود الآلية — {title}", f"الحالة العامة: {'🟢 مفعّلة' if enabled else '⏸️ متوقفة'}", ""]+[f"{'✅' if en else '⏸️'} {tr} ← {rep}" for tr,rep,en in rules[:35]]
        _, buttons=owner_autoreply_group_markup(chat_id)
        await q.edit_message_text("\n".join(lines)[:3500], reply_markup=InlineKeyboardMarkup(buttons)); return

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
            await safe_callback_answer(q, "بيانات التحقق غير صحيحة.", show_alert=True)
            return
        if uid != target_uid:
            await safe_callback_answer(q, "زر التحقق مخصص للعضو الجديد فقط.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM pending_verifications WHERE chat_id=? AND user_id=?", (chat_id, target_uid)).fetchone():
            await safe_callback_answer(q, "تم التحقق مسبقًا أو انتهت المهلة.", show_alert=True)
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
            await safe_callback_answer(q, "تعذر إكمال التحقق. أبلغ مشرف القروب ليتأكد من صلاحيات البوت.", show_alert=True)
        return

    if data == "noop" or data.startswith("noop:"):
        await safe_callback_answer(q, "هذا الخيار للتوضيح فقط.", show_alert=False)
        return

    if data == "media_admin":
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await q.edit_message_text("⛔ التحكم بالوسائط متاح للإشراف والمدراء والمالك فقط.")
            return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🎵 {title or cid}", callback_data=f"media_group:{cid}")] for cid,title in groups[:30]]
        rows.append([InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")])
        await q.edit_message_text("🎵 إدارة الوسائط والتنزيلات\n\nاختر القروب لتفعيل المركز أو إيقافه. الخدمة مفعّلة افتراضيًا للأعضاء.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("media_group:"):
        try: chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await safe_callback_answer(q, "التعطيل متاح للإشراف والمدراء والمالك فقط.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية الوصول لهذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO media_config(chat_id,enabled) VALUES(?,1)", (chat_id,)); db.commit()
        enabled = db.execute("SELECT enabled FROM media_config WHERE chat_id=?", (chat_id,)).fetchone()[0]
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = title_row[0] if title_row else str(chat_id)
        label = "🔴 إيقاف الوسائط للأعضاء" if enabled else "🟢 تشغيل الوسائط للأعضاء"
        await q.edit_message_text(f"🎵 إعدادات الوسائط\nالقروب: {title}\nالحالة: {'مفعّلة' if enabled else 'متوقفة'}\n\nالأعضاء يستطيعون استخدام المركز عند تفعيله.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"media_toggle:{chat_id}")],[InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="media_admin")]]))
        return

    if data.startswith("media_toggle:"):
        try: chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await safe_callback_answer(q, "لا تملك صلاحية تعطيل الوسائط.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية الوصول لهذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO media_config(chat_id,enabled) VALUES(?,1)", (chat_id,))
        current = db.execute("SELECT enabled FROM media_config WHERE chat_id=?", (chat_id,)).fetchone()[0]
        db.execute("UPDATE media_config SET enabled=? WHERE chat_id=?", (0 if current else 1, chat_id)); db.commit()
        await safe_callback_answer(q, "تم إيقاف مركز الوسائط." if current else "تم تشغيل مركز الوسائط للأعضاء.")
        # Return to the group control page with current state.
        enabled = 0 if current else 1
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title = title_row[0] if title_row else str(chat_id)
        label = "🔴 إيقاف الوسائط للأعضاء" if enabled else "🟢 تشغيل الوسائط للأعضاء"
        await q.edit_message_text(f"🎵 إعدادات الوسائط\nالقروب: {title}\nالحالة: {'مفعّلة' if enabled else 'متوقفة'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"media_toggle:{chat_id}")],[InlineKeyboardButton("⬅️ قائمة القروبات", callback_data="media_admin")]]))
        return

    if data.startswith("member_media:"):
        try: chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "هذا القروب غير مسجل.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO media_config(chat_id,enabled) VALUES(?,1)", (chat_id,)); db.commit()
        enabled = db.execute("SELECT enabled FROM media_config WHERE chat_id=?", (chat_id,)).fetchone()[0]
        if not enabled:
            await q.edit_message_text("🎵 مركز الوسائط متوقف حاليًا بقرار من الإشراف. يمكن للأعضاء استخدامه عند إعادة تفعيله.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ مركز الأعضاء", callback_data="public:back")]])); return
        rows = [
            [InlineKeyboardButton("🎵 البحث عن الأغاني", url="https://open.spotify.com/search")],
            [InlineKeyboardButton("🎬 البحث عن الفيديوهات", url="https://www.youtube.com/results?search_query=")],
            [InlineKeyboardButton("📱 البحث عن مقاطع TikTok", url="https://www.tiktok.com/search")],
            [InlineKeyboardButton("🖼️ البحث عن الصور", url="https://images.google.com/")],
            [InlineKeyboardButton("⬇️ مصادر تنزيل الملفات المسموح بها", url="https://commons.wikimedia.org/wiki/Main_Page")],
            [InlineKeyboardButton("⬅️ مركز الأعضاء", callback_data="public:back")]
        ]
        await q.edit_message_text("🎵 مركز الوسائط والصور والتنزيلات\n\nمتاح لجميع الأعضاء افتراضيًا ما لم يوقفه الإشراف. اختر نوع المحتوى ثم افتح مصدر البحث. يمكنك حفظ أو تنزيل المحتوى الذي يتيح مصدره ذلك قانونيًا؛ لا يتجاوز البوت قيود الحماية أو حقوق النشر.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("member_games:"):
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True)
            return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "هذا القروب غير مسجل.", show_alert=True)
            return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,))
        db.commit()
        cfg = db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if not cfg or not cfg[0]:
            await q.edit_message_text("🎮 الألعاب متوقفة حاليًا في هذا القروب.")
            return
        rows = []
        base_buttons = [InlineKeyboardButton(label, callback_data=f"{callback}:{chat_id}") for label, callback in MEMBER_GAMES]
        rows.extend([base_buttons[i:i+2] for i in range(0, len(base_buttons), 2)])
        persistent_buttons = [InlineKeyboardButton(meta["title"], callback_data=f"persistent_game:{key}:{chat_id}") for key, meta in PERSISTENT_GAMES.items()]
        rows.extend([persistent_buttons[i:i+2] for i in range(0, len(persistent_buttons), 2)])
        rows.append([InlineKeyboardButton("⬅️ مركز الأعضاء", callback_data="public:back")])
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
            await safe_callback_answer(q, "⛔ النسخ الاحتياطي للمالك الأساسي فقط.", show_alert=True)
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
            await safe_callback_answer(q, "⛔ إعدادات النظام العليا للمالك الأساسي فقط.", show_alert=True)
            return
        groups_count = db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0]
        delegated_count = db.execute("SELECT COUNT(*) FROM delegated_owners").fetchone()[0]
        await q.edit_message_text(
            "⚙️ إعدادات النظام العليا — المالك الأساسي فقط\n\n"
            f"👥 القروبات المسجلة: {groups_count}\n"
            f"🤝 المالكون المفوّضون: {delegated_count}\n\n"
            "🔐 هذا القسم خاص بالمالك الأساسي. المالك المفوّض يستطيع استخدام لوحة التشغيل للقروبات التي هو عضو فيها، بما يشمل النشر والترحيب والسجلات والحماية وأوامر الإشراف، لكنه لا يستطيع إدارة المالكين أو النسخ الاحتياطي أو إعدادات النظام العليا.\n\n"
            "ملاحظة: إعدادات الحماية داخل القروب تُدار وفق صلاحياته التشغيلية، أما الصلاحيات العليا وإدارة المفوّضين فتبقى للمالك الأساسي.\n\n"
            "منح/سحب صلاحيات المشرفين: /grantcap USER_ID publish|tag|protection all|CHAT_ID ثم /revokecap بنفس الصيغة.\n"
            "حالة الذكاء الاصطناعي: /aistatus.\n"
            "الحماية الافتراضية: منع الروابط والصور والفيديو في القروبات المسجلة؛ المشرفون الفعليون في Telegram مستثنون.",
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
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
        ext = external_group_info(chat_id)
        if ext and ext[0] and not is_owner(uid) and not external_group_cap_enabled(chat_id, "settings"):
            await safe_callback_answer(q, "إعدادات القروب غير مفعّلة من المالك الأساسي.", show_alert=True); return
        await q.edit_message_text(settings_text(0), reply_markup=settings_page_markup(chat_id, 0))
        return

    if data.startswith("settings_page:"):
        parts = data.split(":")
        try:
            if len(parts) == 3: chat_id, page = int(parts[1]), int(parts[2])
            else: chat_id, page = q.message.chat.id, int(parts[1])
        except ValueError: await safe_callback_answer(q, "بيانات الإعدادات غير صحيحة", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية إعدادات هذا القروب.", show_alert=True); return
        ext = external_group_info(chat_id)
        if ext and ext[0] and not is_owner(uid) and not external_group_cap_enabled(chat_id, "settings"):
            await safe_callback_answer(q, "إعدادات القروب غير مفعّلة من المالك الأساسي.", show_alert=True); return
        back = f"group_panel_home:{chat_id}" if not can_use_panel(uid) else "home"
        await q.edit_message_text(settings_text(page), reply_markup=settings_page_markup(chat_id, page, back))
        return

    if data.startswith("toggle:"):
        parts = data.split(":")
        try:
            if len(parts) == 4: chat_id, field, page = int(parts[1]), parts[2], int(parts[3])
            else: field, page, chat_id = parts[1], int(parts[2]), q.message.chat.id
        except (ValueError, IndexError): await safe_callback_answer(q, "بيانات الإعداد غير صحيحة", show_alert=True); return
        if field not in SETTING_FIELDS or not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "ليس لديك صلاحية تغيير هذا الإعداد.", show_alert=True); return
        ext = external_group_info(chat_id)
        if ext and ext[0] and not is_owner(uid):
            if not external_group_cap_enabled(chat_id, "settings"):
                await safe_callback_answer(q, "إعدادات القروب غير مفعّلة من المالك الأساسي.", show_alert=True); return
            if field in {"links","photos","videos","audio","files","stickers","gif","username","tag","bots","keyboard","games","repeat","join_lock","entry","add_lock","edit","flood","verify_new_members"} and not external_group_cap_enabled(chat_id, "protection"):
                await safe_callback_answer(q, "الحماية غير مفعّلة لهذا القروب.", show_alert=True); return
        s = get_settings(chat_id)
        new_value = 0 if s[field] else 1
        if field == "links" and new_value == 0 and not (is_owner(uid) or (is_delegated_owner(uid) and can_use_capability(uid, "protection", chat_id))):
            await safe_callback_answer(q, "فتح الروابط للأعضاء يتطلب صلاحية المالك أو تفويض الحماية من المالك الأساسي.", show_alert=True); return
        set_setting(chat_id, field, new_value)
        log_action(chat_id, uid, "setting", f"{field}={new_value}")
        back = f"group_panel_home:{chat_id}" if not can_use_panel(uid) else "home"
        await q.edit_message_text(settings_text(page), reply_markup=settings_page_markup(chat_id, page, back))
        return

    if data == "bulk_security":
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "لا تملك صلاحية أوامر الحماية الجماعية.", show_alert=True); return
        allowed_ids = capability_group_ids(uid, "protection")
        groups = [(cid,title) for cid,title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if cid in allowed_ids]
        selected = set(context.user_data.get("bulk_security_selected", []))
        selected.intersection_update({cid for cid,_ in groups})
        context.user_data["bulk_security_selected"] = list(selected)
        await q.edit_message_text(
            "⚡ أوامر الحماية الجماعية\n\nاختر القروبات التي تريد تنفيذ الأمر عليها، أو حدّد الكل. بعد اختيار نوع الأمر سيطبّقه البوت ويحاول نشر إشعار عام داخل كل قروب.\n\n"
            f"القروبات المحددة: {len(selected)} من {len(groups)}",
            reply_markup=bulk_security_groups_markup(groups, selected))
        return

    if data.startswith("bulksec_toggle:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: cid = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (cid,)).fetchone() or not can_use_capability(uid, "protection", cid):
            await safe_callback_answer(q, "القروب غير مسجل أو لا تملك صلاحية الحماية فيه.", show_alert=True); return
        selected=set(context.user_data.get("bulk_security_selected", []))
        if cid in selected: selected.remove(cid)
        else: selected.add(cid)
        context.user_data["bulk_security_selected"]=list(selected)
        allowed_ids = capability_group_ids(uid, "protection")
        groups=[(cid,title) for cid,title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if cid in allowed_ids]
        await q.edit_message_text(f"⚡ أوامر الحماية الجماعية\n\nالقروبات المحددة: {len(selected)} من {len(groups)}", reply_markup=bulk_security_groups_markup(groups,selected)); return

    if data in ("bulksec_all", "bulksec_none"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        allowed_ids = capability_group_ids(uid, "protection")
        selected=[cid for cid,_ in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if cid in allowed_ids] if data=="bulksec_all" else []
        context.user_data["bulk_security_selected"]=selected
        allowed_ids = capability_group_ids(uid, "protection")
        groups=[(cid,title) for cid,title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if cid in allowed_ids]
        await q.edit_message_text(f"⚡ أوامر الحماية الجماعية\n\nالقروبات المحددة: {len(selected)} من {len(groups)}", reply_markup=bulk_security_groups_markup(groups,set(selected))); return

    if data == "bulksec_actions":
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        selected=[cid for cid in context.user_data.get("bulk_security_selected", []) if can_use_capability(uid, "protection", cid)]
        context.user_data["bulk_security_selected"] = selected
        if not selected:
            await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل تملك صلاحية الحماية فيه.", show_alert=True); return
        await q.edit_message_text(f"اختر الأمر المطلوب تنفيذه على {len(selected)} قروب.\nسيُحدّث البوت الإعدادات ويحاول نشر إشعار عام بالقروب.", reply_markup=bulk_security_actions_markup()); return

    if data == "bulksec_combo":
        if not (is_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "أوامر الحماية الجماعية تتطلب صلاحية يمنحها المالك الأساسي.", show_alert=True); return
        context.user_data["bulk_security_combo"] = {}
        await q.edit_message_text(
            "⚙️ إعداد مجموعة أوامر حماية مختلطة\n\nاضغط كل نوع للتبديل بين: غير محدد ← قفل ← فتح ← غير محدد. "
            "ثم نفّذ جميع الأوامر المحددة على القروبات التي اخترتها.",
            reply_markup=bulk_security_combo_markup({})); return
    if data.startswith("bulksec_combo_toggle:"):
        if not (is_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        field = data.split(":",1)[1]
        if field not in dict((f,l) for l,f in BULK_SECURITY_OPTIONS):
            await safe_callback_answer(q, "نوع الحماية غير متاح.", show_alert=True); return
        selection = dict(context.user_data.get("bulk_security_combo", {}))
        if field not in selection: selection[field] = 1
        elif selection[field] == 1: selection[field] = 0
        else: selection.pop(field, None)
        context.user_data["bulk_security_combo"] = selection
        await q.edit_message_text("⚙️ إعداد مجموعة أوامر حماية مختلطة\n\n🔒 = قفل | 🔓 = فتح | ⬜ = غير محدد",
                                  reply_markup=bulk_security_combo_markup(selection)); return
    if data == "bulksec_combo_clear":
        if not (is_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        context.user_data["bulk_security_combo"] = {}
        await q.edit_message_text("تم مسح التحديد. اختر الأوامر المطلوبة:", reply_markup=bulk_security_combo_markup({})); return
    if data == "bulksec_combo_apply":
        if not (is_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        selection = {f:int(v) for f,v in dict(context.user_data.get("bulk_security_combo", {})).items()
                     if f in SETTING_FIELDS and f in dict((field,label) for label,field in BULK_SECURITY_OPTIONS) and int(v) in (0,1)}
        selected_groups = [cid for cid in dict.fromkeys(context.user_data.get("bulk_security_selected", []))
                           if can_use_capability(uid, "protection", cid)]
        if not selection or not selected_groups:
            await safe_callback_answer(q, "حدد نوع حماية واحدًا على الأقل وقروبًا واحدًا على الأقل.", show_alert=True); return
        if selection.get("links") == 0 and not (is_owner(uid) or (is_delegated_owner(uid) and all(can_use_capability(uid, "protection", cid) for cid in selected_groups))):
            await safe_callback_answer(q, "فتح الروابط للأعضاء يتطلب المالك الأساسي أو المالك المفوض المخوّل بالحماية.", show_alert=True); return
        labels = dict((field,label) for label,field in BULK_SECURITY_OPTIONS)
        success = failed = announced = 0
        for cid in selected_groups:
            try:
                if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (cid,)).fetchone():
                    failed += 1; continue
                for field, value in selection.items():
                    set_setting(cid, field, value)
                    log_action(cid, uid, "bulk_combo_setting", f"{field}={value}")
                success += 1
                commands = [("قفل " if value else "فتح ") + labels[field] for field,value in selection.items()]
                try:
                    await context.bot.send_message(chat_id=cid, text="🛡️ تنفيذ أوامر حماية إدارية: " + "؛ ".join(commands))
                    announced += 1
                except Exception as exc:
                    log.info("Bulk mixed-setting notice failed chat=%s: %s", cid, exc)
            except Exception as exc:
                failed += 1; log.warning("Bulk mixed-setting failed chat=%s: %s", cid, exc)
        await q.edit_message_text(
            "✅ اكتمل تنفيذ مجموعة الأوامر.\n\n" + "\n".join(("🔒 قفل " if value else "🔓 فتح ") + labels[field] for field,value in selection.items()) +
            f"\n\nالقروبات التي حُدثت إعداداتها: {success}\nالإشعارات المنشورة: {announced}\nتعذر التنفيذ: {failed}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ أوامر جماعية أخرى", callback_data="bulk_security")],
                                               [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data.startswith("bulksec_multi:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: value = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "اختيار غير صحيح.", show_alert=True); return
        if value not in (0,1): await safe_callback_answer(q, "اختيار غير صحيح.", show_alert=True); return
        context.user_data["bulk_security_multi_value"] = value
        context.user_data["bulk_security_multi_fields"] = []
        await q.edit_message_text(f"🧩 اختر عدة أنواع ليتم {'قفلها' if value else 'فتحها'} دفعة واحدة على القروبات المحددة.", reply_markup=bulk_security_multi_markup([], value)); return
    if data.startswith("bulksec_multi_toggle:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: _, field, raw_value = data.split(":",2); value=int(raw_value)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if field not in dict((f,l) for l,f in BULK_SECURITY_OPTIONS) or value not in (0,1):
            await safe_callback_answer(q, "نوع الحماية غير متاح.", show_alert=True); return
        selected = set(context.user_data.get("bulk_security_multi_fields", []))
        if field in selected: selected.remove(field)
        else: selected.add(field)
        context.user_data["bulk_security_multi_fields"] = list(selected)
        await q.edit_message_text(f"🧩 اختر عدة أنواع ليتم {'قفلها' if value else 'فتحها'} دفعة واحدة.", reply_markup=bulk_security_multi_markup(selected, value)); return
    if data.startswith(("bulksec_multi_all:", "bulksec_multi_none:")):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: value = int(data.rsplit(":",1)[1])
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        fields = [field for _,field in BULK_SECURITY_OPTIONS] if data.startswith("bulksec_multi_all:") else []
        context.user_data["bulk_security_multi_fields"] = fields
        await q.edit_message_text(f"🧩 الأنواع المحددة: {len(fields)}. اختر الأنواع ثم نفّذ دفعة واحدة.", reply_markup=bulk_security_multi_markup(fields, value)); return
    if data.startswith("bulksec_multi_apply:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try: value = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        fields = [f for f in dict.fromkeys(context.user_data.get("bulk_security_multi_fields", []))
                  if f in SETTING_FIELDS and f in dict((field,label) for label,field in BULK_SECURITY_OPTIONS)]
        selected_groups = [cid for cid in dict.fromkeys(context.user_data.get("bulk_security_selected", []))
                           if can_use_capability(uid, "protection", cid)]
        if value not in (0,1) or not fields or not selected_groups:
            await safe_callback_answer(q, "حدد نوع حماية واحدًا على الأقل وقروبًا واحدًا على الأقل.", show_alert=True); return
        if "links" in fields and value == 0 and not (is_owner(uid) or (is_delegated_owner(uid) and all(can_use_capability(uid, "protection", cid) for cid in selected_groups))):
            await safe_callback_answer(q, "فتح الروابط للأعضاء على مستوى القروبات يتطلب المالك الأساسي أو المالك المفوض المخوّل بالحماية.", show_alert=True); return
        success = failed = announced = 0
        verb = "قفل" if value else "فتح"
        labels = dict((field,label) for label,field in BULK_SECURITY_OPTIONS)
        for cid in selected_groups:
            try:
                if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (cid,)).fetchone():
                    failed += 1; continue
                for field in fields:
                    set_setting(cid, field, value)
                    log_action(cid, uid, "bulk_multi_setting", f"{field}={value}")
                success += 1
                try:
                    await context.bot.send_message(chat_id=cid, text=f"🛡️ أمر إداري: تم {verb} " + "، ".join(labels[f] for f in fields) + " بواسطة إدارة البوت.")
                    announced += 1
                except Exception as exc:
                    log.info("Bulk multi-setting notice failed chat=%s: %s", cid, exc)
            except Exception as exc:
                failed += 1; log.warning("Bulk multi-setting failed chat=%s: %s", cid, exc)
        await q.edit_message_text(
            f"✅ نتيجة التنفيذ الجماعي\nالإجراء: {verb}\nالأنواع: " + "، ".join(labels[f] for f in fields) +
            f"\nالقروبات التي حُدثت إعداداتها: {success}\nالإشعارات المنشورة: {announced}\nتعذر التنفيذ: {failed}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ أمر جماعي آخر", callback_data="bulk_security")],
                                               [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data.startswith("bulksec_do:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "protection")):
            await safe_callback_answer(q, "غير مصرح لك.", show_alert=True); return
        try:
            _, field, raw_value = data.split(":",2); value=int(raw_value)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "الأمر غير صحيح.", show_alert=True); return
        if field not in SETTING_FIELDS or value not in (0,1) or field not in dict((f,l) for l,f in BULK_SECURITY_OPTIONS):
            await safe_callback_answer(q, "هذا الإعداد غير متاح للأوامر الجماعية.", show_alert=True); return
        selected=[cid for cid in dict.fromkeys(context.user_data.get("bulk_security_selected", [])) if can_use_capability(uid, "protection", cid)]
        if not selected:
            await safe_callback_answer(q, "انتهى التحديد؛ اختر القروبات من جديد.", show_alert=True); return
        if field == "links" and value == 0 and not (is_owner(uid) or (is_delegated_owner(uid) and all(can_use_capability(uid, "protection", cid) for cid in selected))):
            await safe_callback_answer(q, "فتح الروابط للأعضاء على مستوى القروبات يتطلب المالك الأساسي أو المالك المفوض المخوّل بالحماية.", show_alert=True); return
        option_label=dict((f,l) for l,f in BULK_SECURITY_OPTIONS)[field]
        verb="قفل" if value else "فتح"
        announcement=f"{'🔒' if value else '🔓'} أمر إداري: تم {verb} {option_label} بواسطة إدارة البوت."
        success=0; failed=0; announced=0
        for cid in selected:
            if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (cid,)).fetchone() or not can_use_capability(uid, "protection", cid):
                failed += 1; continue
            try:
                set_setting(cid, field, value)
                log_action(cid, uid, "bulk_setting", f"{field}={value}")
                success += 1
                try:
                    await context.bot.send_message(chat_id=cid, text=announcement)
                    announced += 1
                except Exception as send_exc:
                    log.info("Setting updated but public notice failed for %s: %s", cid, send_exc)
            except Exception as exc:
                failed += 1; log.exception("Bulk security setting failed for %s: %s", cid, exc)
        await safe_callback_answer(q, "اكتمل تنفيذ الأمر.")
        await q.edit_message_text(
            f"✅ نتيجة الأمر الجماعي\n\nالإجراء: {verb} {option_label}\n"
            f"تم تطبيق الإعدادات: {success} قروب\nتم نشر الإشعار العام: {announced} قروب\nتعذر التنفيذ: {failed} قروب\n\n"
            "إذا تعذر نشر الإشعار، فقد يكون البوت لا يملك صلاحية إرسال الرسائل في ذلك القروب؛ راجع صلاحياته.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ أمر جماعي آخر", callback_data="bulk_security")], [InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]))
        return

    if data in ("security", "security_groups"):
        if not (is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الحماية."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🛡️ {title or cid}", callback_data=f"settings_group:{cid}")] for cid,title in groups[:30]]
        if is_owner(uid) or can_use_capability(uid, "mass_ban"):
            rows.insert(0, [InlineKeyboardButton("🚫 الحظر الجماعي الشامل/الجزئي", callback_data="massban_menu")])
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
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب", show_alert=True); return
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
                [InlineKeyboardButton("⬅️ لوحة القروب" if not can_use_panel(uid) else "⬅️ القروبات", callback_data=f"group_panel_home:{chat_id}" if not can_use_panel(uid) else "administration_groups")]
            ])
        )
        return

    if data.startswith("admin_action:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"ban", "unban", "kick", "mute", "unmute", "delete", "pin"}:
            await safe_callback_answer(q, "الإجراء غير صحيح.", show_alert=True); return
        action = parts[1]
        try: chat_id = int(parts[2])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية إدارة هذا القروب.", show_alert=True); return
        permission_for_action = {"ban":"ban", "unban":"unban", "kick":"kick", "mute":"mute", "unmute":"unmute", "delete":"delete", "pin":"pin"}[action]
        if not has_group_permission(chat_id, uid, permission_for_action):
            await safe_callback_answer(q, "رتبتك لا تسمح بهذا الإجراء في هذا القروب.", show_alert=True); return
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
        pending = context.user_data.pop("awaiting_admin_action", None) or {}
        pending_chat = pending.get("chat_id")
        if pending_chat and not can_use_panel(uid) and await is_telegram_group_admin(context.bot, uid, pending_chat):
            await q.edit_message_text("تم إلغاء الإجراء.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لوحة القروب", callback_data=f"group_panel_admin:{pending_chat}")]])); return
        await q.edit_message_text("تم إلغاء الإجراء.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data == "welcome":
        if not (is_owner(uid) or is_general_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الترحيب."); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"👋 {title or cid}", callback_data=f"welcome_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("👋 إعداد الترحيب لكل قروب\nاختر قروبًا أنت عضو فيه لتعيين الرسالة أو إيقافها.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data == "games":
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"🎮 {title or cid}", callback_data=f"games_group:{cid}")] for cid,title in groups[:40]]
        if not rows:
            rows = [[InlineKeyboardButton("لا توجد قروبات متاحة", callback_data="noop")]]
        rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
        await q.edit_message_text("🎮 مركز الألعاب التفاعلية\nاختر القروب الذي تريد إدارته أو فتح ألعابه.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("games_group:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية الوصول لهذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,)); db.commit()
        cfg = db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        title = title_row[0] if title_row else str(chat_id)
        rows = []
        admin_buttons = [InlineKeyboardButton(label, callback_data=f"{callback}:{chat_id}") for label, callback in ADMIN_GAMES]
        rows.extend([admin_buttons[i:i+2] for i in range(0, len(admin_buttons), 2)])
        if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid):
            feature_names = [("kingdom","🌍 المملكة"),("hero","⚔️ البطل"),("cards","🃏 المقتنيات"),("alliance","🏰 التحالفات"),("case","🕵️ القضية"),("season","🏆 الموسم")]
            feature_buttons = []
            for feature, label in feature_names:
                flag = db.execute("SELECT enabled FROM persistent_game_flags WHERE chat_id=? AND game_key=?", (chat_id, feature)).fetchone()
                active = flag[0] if flag else 1
                feature_buttons.append(InlineKeyboardButton(("🟢 " if active else "🔴 ")+label, callback_data=f"game_feature_toggle:{chat_id}:{feature}"))
            rows += [feature_buttons[i:i+2] for i in range(0, len(feature_buttons), 2)]
        if is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid):
            rows.append([InlineKeyboardButton(("🟢 إيقاف الألعاب" if cfg[0] else "🔴 تشغيل الألعاب"), callback_data=f"game_toggle:{chat_id}"), InlineKeyboardButton(("🟢 إيقاف تحدي الخاسر" if cfg[1] else "🔴 تشغيل تحدي الخاسر"), callback_data=f"game_challenge_toggle:{chat_id}")])
        rows.append([InlineKeyboardButton("⬅️ القروبات", callback_data="games")])
        return

    if data.startswith("game_feature_toggle:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await safe_callback_answer(q, "إيقاف الألعاب وتشغيلها متاح للإشراف والمدراء والمالك فقط.", show_alert=True); return
        try:
            _, chat_raw, feature = data.split(":", 2)
            chat_id = int(chat_raw)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "تعذر قراءة إعداد اللعبة.", show_alert=True); return
        allowed_features = {"kingdom","hero","cards","alliance","case","season"}
        if feature not in allowed_features or not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)) or not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية تعديل هذه اللعبة.", show_alert=True); return
        row = db.execute("SELECT enabled FROM persistent_game_flags WHERE chat_id=? AND game_key=?", (chat_id, feature)).fetchone()
        new_value = 0 if (row and row[0]) else 1
        db.execute("INSERT INTO persistent_game_flags(chat_id,game_key,enabled) VALUES(?,?,?) ON CONFLICT(chat_id,game_key) DO UPDATE SET enabled=excluded.enabled", (chat_id, feature, new_value)); db.commit()
        await safe_callback_answer(q, "تم إيقاف اللعبة." if not new_value else "تم تشغيل اللعبة.")
        # Refresh the management page to show the new state.
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title = title_row[0] if title_row else str(chat_id)
        cfg = db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        menu_buttons = [InlineKeyboardButton(label, callback_data=f"{callback}:{chat_id}") for label, callback in ADMIN_GAMES]
        rows = [menu_buttons[i:i+2] for i in range(0, len(menu_buttons), 2)]
        feature_names = [("kingdom","🌍 المملكة"),("hero","⚔️ البطل"),("cards","🃏 المقتنيات"),("alliance","🏰 التحالفات"),("case","🕵️ القضية"),("season","🏆 الموسم")]
        feature_buttons=[]
        for f, label in feature_names:
            flag=db.execute("SELECT enabled FROM persistent_game_flags WHERE chat_id=? AND game_key=?",(chat_id,f)).fetchone(); active=flag[0] if flag else 1
            feature_buttons.append(InlineKeyboardButton(("🟢 " if active else "🔴 ")+label,callback_data=f"game_feature_toggle:{chat_id}:{f}"))
        rows += [feature_buttons[i:i+2] for i in range(0,len(feature_buttons),2)]
        rows.append([InlineKeyboardButton(("🟢 إيقاف الألعاب" if cfg[0] else "🔴 تشغيل الألعاب"),callback_data=f"game_toggle:{chat_id}"),InlineKeyboardButton(("🟢 إيقاف تحدي الخاسر" if cfg[1] else "🔴 تشغيل تحدي الخاسر"),callback_data=f"game_challenge_toggle:{chat_id}")])
        rows.append([InlineKeyboardButton("⬅️ القروبات",callback_data="games")])

    if data.startswith("persistent_game:"):
        try:
            _, game_key, chat_raw = data.split(":", 2); chat_id = int(chat_raw)
        except (ValueError, TypeError):
            await safe_callback_answer(q, "بيانات اللعبة غير صحيحة.", show_alert=True); return
        game_names = {
            "kingdom": ("🌍 مملكة القروب", "طوّر مملكتك واجمع الموارد وافتح مناطق جديدة."),
            "hero": ("⚔️ مغامرة البطل", "درّب بطلك وارفع مستواه وواصل التقدم."),
            "cards": ("🃏 ألبوم المقتنيات", "اجمع مقتنيات يومية وأكمل مجموعاتك."),
            "alliance": ("🏰 تحالفات القروب", "ساهم في تقدم قروبك واجمع نقاط التعاون."),
            "case": ("🕵️ ملف القضية", "حل لغز اليوم واختر الإجابة الصحيحة."),
            "season": ("🏆 موسم التحديات", "أنجز مهمة اليوم وواصل سلسلة إنجازاتك.")
        }
        if game_key not in game_names or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone():
            await safe_callback_answer(q, "اللعبة أو القروب غير متاح.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        flag=db.execute("SELECT enabled FROM persistent_game_flags WHERE chat_id=? AND game_key=?",(chat_id,game_key)).fetchone()
        if (cfg and not cfg[0]) or (flag and not flag[0]):
            await safe_callback_answer(q, "هذه الألعاب متوقفة حاليًا من لوحة الإدارة.",show_alert=True); return
        uid_player=q.from_user.id; display=q.from_user.full_name or "عضو"; epoch=int(time.time())
        row=db.execute("SELECT progress,coins,last_claim_epoch FROM persistent_game_state WHERE chat_id=? AND user_id=? AND game_key=?",(chat_id,uid_player,game_key)).fetchone()
        progress,coins,last_claim=row if row else (0,0,0)
        title,description=game_names[game_key]
        if epoch-last_claim < 86400:
            remaining=86400-(epoch-last_claim); hours=remaining//3600; minutes=(remaining%3600)//60
            await safe_callback_answer(q, f"أنجزت مكافأة اليوم. عد بعد {hours} س و{minutes} د.",show_alert=True); return
        # تقدم يومي محفوظ مع فصول متدرجة ومهام مختلفة لكل نمط لعب.
        progress += 1
        reward = 5 if progress % 7 == 0 else (3 if game_key in ("kingdom","hero","alliance") else 2)
        coins += reward
        story_steps = {
            "kingdom": ["تأسيس القرية وجمع الخشب", "بناء السوق وفتح المزارع", "ترميم الأسوار وحماية المملكة", "استكشاف الوادي وجمع الأحجار", "فتح بوابة المدينة الجديدة"],
            "hero": ["التدريب في ساحة المبتدئين", "الحصول على أول أداة", "اجتياز اختبار المهارة", "مواجهة حارس الكهف", "فتح مهارة بطولية جديدة"],
            "cards": ["اكتشاف بطاقة جديدة", "إكمال أول مجموعة مصغرة", "فتح صندوق مقتنيات", "العثور على بطاقة نادرة", "ترقية ألبومك وفتح صفحة جديدة"],
            "alliance": ["المساهمة في مهمة الفريق", "جمع موارد للتحالف", "اجتياز تحدي التعاون", "دعم حصن التحالف", "فتح إنجاز جماعي جديد"],
            "case": ["العثور على الدليل الأول", "مقارنة أقوال الشهود", "كشف تناقض في الأدلة", "تحديد المشتبه به الأقرب", "حل فصل من القضية"],
            "season": ["إنجاز مهمة الموسم", "اجتياز تحدي المعرفة", "جمع شارة الموسم", "فتح مرحلة أصعب", "تحقيق إنجاز موسمي"]
        }
        story = story_steps.get(game_key, [description])[min((progress - 1) % 5, len(story_steps.get(game_key, [description])) - 1)]
        chapter = (progress - 1) // 5 + 1
        db.execute("INSERT INTO persistent_game_state(chat_id,user_id,game_key,progress,coins,last_claim_epoch,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id,user_id,game_key) DO UPDATE SET progress=excluded.progress,coins=excluded.coins,last_claim_epoch=excluded.last_claim_epoch,updated_at=excluded.updated_at",(chat_id,uid_player,game_key,progress,coins,epoch,now()))
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,uid_player,display))
        db.execute("UPDATE game_scores SET points=points+?,display_name=? WHERE chat_id=? AND user_id=?",(reward,display,chat_id,uid_player)); record_game_points(chat_id,uid_player,reward,game_key); db.commit()
        milestone="\n🎉 إنجاز جديد! وصلت إلى 7 أيام." if progress==7 else ("\n🏅 إنجاز جديد! وصلت إلى 30 يومًا." if progress==30 else ("\n🌟 مكافأة أسبوعية إضافية!" if progress % 7 == 0 else ""))
        await q.edit_message_text(f"{title}\n\n{description}\n\n📖 الفصل: {chapter}\n🧭 مهمة اليوم: {story}\n📈 تقدمك: {progress} يوم\n🪙 رصيد اللعبة: {coins}\n⭐ نقاطك اليوم: {reward}{milestone}\n\nتتغير المهمة مع تقدمك، وتُحفظ بياناتك بعد إعادة تشغيل البوت. عد غدًا لمتابعة الفصل التالي.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 العودة للألعاب",callback_data=f"member_games:{chat_id}")],[InlineKeyboardButton("🏆 لوحة المتصدرين",callback_data=f"game_scores:{chat_id}")]])); return

    if data.startswith("game_toggle:") or data.startswith("game_challenge_toggle:"):
        try: chat_id = int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not (is_owner(uid) or is_delegated_owner(uid) or is_general_manager(uid) or is_protection_manager(uid)):
            await safe_callback_answer(q, "إيقاف الألعاب وتشغيلها متاح للإشراف والمدراء والمالك فقط.", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_config(chat_id,enabled,challenge_enabled) VALUES(?,1,1)", (chat_id,))
        field = "enabled" if data.startswith("game_toggle:") else "challenge_enabled"
        current = db.execute(f"SELECT {field} FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()[0]
        db.execute(f"UPDATE game_config SET {field}=? WHERE chat_id=?", (0 if current else 1, chat_id)); db.commit()
        await safe_callback_answer(q, "تم تحديث إعداد الألعاب.")
        # Return to the group games page.
        cfg = db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(); title = title_row[0] if title_row else str(chat_id)
        rows = [[InlineKeyboardButton("🧠 أسئلة ثقافية", callback_data=f"game_quiz:{chat_id}"), InlineKeyboardButton("🔤 تحدي الكلمات", callback_data=f"game_word:{chat_id}")], [InlineKeyboardButton("🧩 فك الكلمات", callback_data=f"game_scramble:{chat_id}"), InlineKeyboardButton("✊ حجر ورقة مقص", callback_data=f"game_rps:{chat_id}")], [InlineKeyboardButton("⭕ إكس أو لاعبين", callback_data=f"game_xo_start:{chat_id}"), InlineKeyboardButton("⚡ تحدي السرعة", callback_data=f"game_speed:{chat_id}")], [InlineKeyboardButton("🎯 تحدي اليوم", callback_data=f"game_daily:{chat_id}"), InlineKeyboardButton("🎯 تحدي الخاسر", callback_data=f"game_challenge:{chat_id}")], [InlineKeyboardButton("🏆 المتصدرون", callback_data=f"game_scores:{chat_id}"), InlineKeyboardButton("📅 أسبوعي/شهري", callback_data=f"game_periods:{chat_id}")]]
        if is_owner(uid) or is_delegated_owner(uid):
            feature_names = [("kingdom","🌍 المملكة"),("hero","⚔️ البطل"),("cards","🃏 المقتنيات"),("alliance","🏰 التحالفات"),("case","🕵️ القضية"),("season","🏆 الموسم")]
            feature_buttons=[]
            for f,label in feature_names:
                flag=db.execute("SELECT enabled FROM persistent_game_flags WHERE chat_id=? AND game_key=?",(chat_id,f)).fetchone(); active=flag[0] if flag else 1
                feature_buttons.append(InlineKeyboardButton(("🟢 " if active else "🔴 ")+label,callback_data=f"game_feature_toggle:{chat_id}:{f}"))
            rows += [feature_buttons[i:i+2] for i in range(0,len(feature_buttons),2)]
        rows += [[InlineKeyboardButton(("🟢 إيقاف الألعاب" if cfg[0] else "🔴 تشغيل الألعاب"), callback_data=f"game_toggle:{chat_id}"), InlineKeyboardButton(("🟢 إيقاف تحدي الخاسر" if cfg[1] else "🔴 تشغيل تحدي الخاسر"), callback_data=f"game_challenge_toggle:{chat_id}")], [InlineKeyboardButton("⬅️ القروبات", callback_data="games")]]

    if data.startswith("game_quiz:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        rows = [[InlineKeyboardButton(title, callback_data=f"game_quiz_cat:{chat_id}:{key}")]
                for key,(title,_questions) in QUIZ_CATEGORIES.items()]
        rows.append([InlineKeyboardButton("🎯 مسابقة متنوعة من 5 مراحل", callback_data=f"game_quiz_cat:{chat_id}:mixed")])
        rows.append([InlineKeyboardButton("⬅️ الألعاب", callback_data=f"member_games:{chat_id}")])
        await q.edit_message_text("🧠 المسابقات والمعلومات المتنوعة\n\nاختر المجال، أو ابدأ مسابقة متنوعة من خمس مراحل. بنك المعلومات داخلي ويعمل دون API؛ كل إجابة صحيحة تمنح نقاطًا وتعرض معلومة مختصرة.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_quiz_cat:"):
        try: _,chat_raw,category=data.split(":",2); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات المسابقة غير صحيحة.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        if category == "mixed": category=random.choice(list(QUIZ_CATEGORIES.keys()))
        if category not in QUIZ_CATEGORIES: await safe_callback_answer(q, "هذا المجال غير متاح.", show_alert=True); return
        await safe_callback_answer(q, "بدأت المسابقة!")
        await send_quiz_stage(context, chat_id, category, 1)
        await q.edit_message_text("🚀 بدأت المسابقة في القروب! انتقل إلى القروب للإجابة، ثم أكمل المراحل الخمس.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 مركز الألعاب", callback_data=f"member_games:{chat_id}")]])); return

    if data.startswith("game_stage_answer:"):
        try: _,token,choice_raw=data.split(":",2); choice=int(choice_raw)
        except ValueError: await safe_callback_answer(q, "الإجابة غير صالحة.", show_alert=True); return
        row=db.execute("SELECT chat_id,answer,completed FROM game_rounds WHERE token=? AND kind='quiz_stage'", (token,)).fetchone()
        if not row: await safe_callback_answer(q, "انتهت هذه الجولة؛ ابدأ مسابقة جديدة.", show_alert=True); return
        chat_id,answer_data,completed=row
        if q.message.chat.id != chat_id: await safe_callback_answer(q, "هذه الجولة تخص قروبًا آخر.", show_alert=True); return
        if completed: await safe_callback_answer(q, "تم حسم هذه المرحلة بالفعل.", show_alert=True); return
        try: category,stage_raw,question_raw,correct_raw=answer_data.split("|"); stage=int(stage_raw); question_index=int(question_raw); correct=int(correct_raw)
        except ValueError: await safe_callback_answer(q, "تعذر قراءة بيانات المرحلة.", show_alert=True); return
        title,questions=QUIZ_CATEGORIES[category]; question,options,correct_index,explanation=questions[question_index]
        if choice != correct:
            await safe_callback_answer(q, "❌ ليست الإجابة الصحيحة؛ جرّب مرة أخرى.", show_alert=True); return
        changed=db.execute("UPDATE game_rounds SET completed=1 WHERE token=? AND completed=0", (token,)).rowcount
        if not changed: await safe_callback_answer(q, "تم حسم هذه المرحلة بالفعل.", show_alert=True); return
        name=q.from_user.full_name or "عضو"
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+5,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?", (name,chat_id,uid))
        record_game_points(chat_id,uid,5,"quiz_stage")
        db.commit()
        rows=[]
        if stage < 5:
            rows.append([InlineKeyboardButton(f"➡️ المرحلة التالية ({stage+1}/5)", callback_data=f"game_stage_next:{chat_id}:{category}:{stage+1}")])
        else:
            rows.append([InlineKeyboardButton("🏆 عرض المتصدرين", callback_data=f"game_scores:{chat_id}")])
        rows.append([InlineKeyboardButton("🎮 الألعاب", callback_data=f"member_games:{chat_id}")])
        await q.edit_message_text(f"✅ إجابة صحيحة يا {name}!\n⭐ ربحت 5 نقاط.\n\n💡 معلومة: {explanation}\n\n{'🎉 أكملت المراحل الخمس! يمكنك بدء مسابقة جديدة.' if stage==5 else 'أصبحت جاهزًا للمرحلة التالية.'}", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_stage_next:"):
        try: _,chat_raw,category,stage_raw=data.split(":",3); chat_id=int(chat_raw); stage=int(stage_raw)
        except ValueError: await safe_callback_answer(q, "بيانات المرحلة غير صحيحة.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "أوقف الإشراف الألعاب في هذا القروب.", show_alert=True); return
        if category not in QUIZ_CATEGORIES or stage < 2 or stage > 5:
            await safe_callback_answer(q, "المرحلة غير متاحة.", show_alert=True); return
        await safe_callback_answer(q, f"المرحلة {stage} بدأت")
        await send_quiz_stage(context,chat_id,category,stage)
        try: await q.edit_message_text(f"🚀 نُشرت المرحلة {stage} من 5 في القروب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 مركز الألعاب", callback_data=f"member_games:{chat_id}")]]))
        except Exception: pass
        return

    if data.startswith("game_answer:"):
        try: _,chat_raw,correct_raw=data.split(":",2); chat_id=int(chat_raw); correct=int(correct_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
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
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        target, hint = random.choice(WORD_BANK)
        distractors = [word for word, _hint in WORD_BANK if word != target]
        options = random.sample(distractors, min(3, len(distractors))) + [target]
        random.shuffle(options)
        rows = [[InlineKeyboardButton(option, callback_data=f"game_word_answer:{chat_id}:{1 if option == target else 0}")] for option in options]
        rows.append([InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")])
        await context.bot.send_message(chat_id, f"🔤 تحدي الكلمات\n\n💡 تلميح: {hint}\nاختر الكلمة المناسبة:", reply_markup=InlineKeyboardMarkup(rows))
        await q.edit_message_text("تم نشر تحدي الكلمات في القروب 🔤", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_word_answer:"):
        try: _,chat_raw,correct_raw=data.split(":",2); chat_id=int(chat_raw); correct=int(correct_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
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
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        import random
        bot_choice=random.choice(["حجر","ورقة","مقص"])
        context.user_data[f"rps:{chat_id}"]=bot_choice
        rows=[[InlineKeyboardButton("✊ حجر", callback_data=f"game_rps_pick:{chat_id}:حجر"), InlineKeyboardButton("✋ ورقة", callback_data=f"game_rps_pick:{chat_id}:ورقة"), InlineKeyboardButton("✌️ مقص", callback_data=f"game_rps_pick:{chat_id}:مقص")], [InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]]
        await q.edit_message_text("✊ حجر ورقة مقص\nاختر حركتك، وسأقارنها باختيار البوت:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_rps_pick:"):
        try: _,chat_raw,choice=data.split(":",2); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        bot_choice=context.user_data.pop(f"rps:{chat_id}", None)
        if not bot_choice: await safe_callback_answer(q, "انتهت الجولة؛ ابدأ لعبة جديدة.", show_alert=True); return
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
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled,challenge_enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and (not cfg[0] or not cfg[1]): await safe_callback_answer(q, "تحدي الخاسر غير مفعّل.", show_alert=True); return
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
            await safe_callback_answer(q, "انتهى هذا التحدي أو أنه غير صالح.", show_alert=True); return
        chat_id, challenge_user_id, completed=challenge
        if challenge_user_id != uid:
            await safe_callback_answer(q, "هذا التحدي مخصص للعضو الذي استلمه.", show_alert=True); return
        if completed:
            await safe_callback_answer(q, "تم احتساب هذا التحدي مسبقًا.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("UPDATE game_challenges SET completed=1 WHERE token=? AND completed=0", (challenge_token,))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.commit(); await safe_callback_answer(q, "تم احتساب هذا التحدي مسبقًا.", show_alert=True); return
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)", (chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id, uid, 1, "challenge"); db.commit()
        await q.edit_message_text("👏 أحسنت! تم تسجيل نقطة مشاركة ⭐", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 العودة للألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_scores:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        rows=db.execute("SELECT display_name,points,wins,losses FROM game_scores WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 10",(chat_id,)).fetchall()
        body="\n".join(f"{i}. {name or 'عضو'} — ⭐ {points} | 🏆 {wins} فوز | ❌ {losses} خسارة" for i,(name,points,wins,losses) in enumerate(rows,1)) or "لا توجد نتائج بعد. ابدأ اللعب لتظهر هنا!"
        await q.edit_message_text("🏆 المتصدرون في هذا القروب\n\n"+body, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_periods:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        rows=[[InlineKeyboardButton("📅 المتصدرون هذا الأسبوع", callback_data=f"game_period_scores:{chat_id}:week")],
              [InlineKeyboardButton("🗓️ المتصدرون هذا الشهر", callback_data=f"game_period_scores:{chat_id}:month")],
              [InlineKeyboardButton("🏆 الترتيب العام", callback_data=f"game_scores:{chat_id}")],
              [InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]]
        await q.edit_message_text("🏆 اختر مدة ترتيب النقاط:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("game_period_scores:"):
        try: _, chat_raw, period = data.split(":",2); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if period not in ("week","month") or not await can_access_group(context.bot, uid, chat_id): await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(days=7 if period=="week" else 30)).isoformat()
        rows=db.execute("SELECT e.user_id, COALESCE(s.display_name,'عضو'), SUM(e.points) pts FROM game_score_events e LEFT JOIN game_scores s ON s.chat_id=e.chat_id AND s.user_id=e.user_id WHERE e.chat_id=? AND e.created_at>=? GROUP BY e.user_id ORDER BY pts DESC LIMIT 10", (chat_id,cutoff)).fetchall()
        title="الأسبوع" if period=="week" else "آخر 30 يومًا"
        body="\n".join(f"{i}. {name} — ⭐ {points}" for i,(_,name,points) in enumerate(rows,1)) or "لا توجد نقاط مسجلة لهذه المدة بعد."
        await q.edit_message_text(f"🏆 المتصدرون — {title}\n\n{body}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المدد", callback_data=f"game_periods:{chat_id}")],[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_scramble:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        token=uuid.uuid4().hex[:12]
        answer, _hint = random.choice(WORD_BANK)
        scrambled = "".join(random.sample(list(answer), len(answer)))
        alternatives = [word for word, _hint in WORD_BANK if word != answer]
        options = random.sample(alternatives, min(3, len(alternatives))) + [answer]
        random.shuffle(options)
        db.execute("INSERT INTO game_rounds(token,chat_id,kind,answer,created_at) VALUES(?,?,?,?,?)",(token,chat_id,"scramble",answer,now())); db.commit()
        buttons=[[InlineKeyboardButton(x, callback_data=f"game_round_answer:{token}:{x}")] for x in options]
        await context.bot.send_message(chat_id, f"🔤 فكّ الكلمة المبعثرة!\n\nالحروف: {scrambled}\nأول إجابة صحيحة تحصل على نقطتين ⭐", reply_markup=InlineKeyboardMarkup(buttons))
        await q.edit_message_text("تم نشر تحدي الكلمات في القروب المحدد ✅", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب", callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_speed:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.", show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?", (chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.", show_alert=True); return
        token=uuid.uuid4().hex[:12]
        db.execute("INSERT INTO game_rounds(token,chat_id,kind,answer,created_at) VALUES(?,?,?,?,?)",(token,chat_id,"speed","الرياض",now())); db.commit()
        buttons=[[InlineKeyboardButton("جدة", callback_data=f"game_round_answer:{token}:جدة"), InlineKeyboardButton("الرياض", callback_data=f"game_round_answer:{token}:الرياض")], [InlineKeyboardButton("أبها", callback_data=f"game_round_answer:{token}:أبها"), InlineKeyboardButton("تبوك", callback_data=f"game_round_answer:{token}:تبوك")]]
        await context.bot.send_message(chat_id,"⚡ تحدي السرعة!\nما عاصمة المملكة العربية السعودية؟\nأول عضو يضغط الإجابة الصحيحة يحصل على 3 نقاط ⭐",reply_markup=InlineKeyboardMarkup(buttons))
        await q.edit_message_text("نُشر تحدي السرعة في القروب ⚡",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_round_answer:"):
        try: _,token,answer=data.split(":",2)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        row=db.execute("SELECT chat_id,kind,answer,completed,created_at FROM game_rounds WHERE token=?",(token,)).fetchone()
        if not row: await safe_callback_answer(q, "انتهت الجولة أو غير موجودة.",show_alert=True); return
        chat_id,kind,correct,completed,created_at=row
        if completed: await safe_callback_answer(q, "سبق أن فاز عضو بهذه الجولة.",show_alert=True); return
        if answer != correct: await safe_callback_answer(q, "ليست الإجابة الصحيحة، حاول مجددًا.",show_alert=True); return
        if (datetime.now(timezone.utc)-datetime.fromisoformat(created_at)).total_seconds()>180: await safe_callback_answer(q, "انتهى وقت الجولة.",show_alert=True); return
        db.execute("UPDATE game_rounds SET completed=1 WHERE token=? AND completed=0",(token,))
        if db.execute("SELECT changes()").fetchone()[0]!=1: db.commit(); await safe_callback_answer(q, "سبق أن فاز عضو بهذه الجولة.",show_alert=True); return
        name=q.from_user.full_name or str(uid); points=3 if kind=="speed" else 2
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+?,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(points,name,chat_id,uid)); record_game_points(chat_id,uid,points,kind); db.commit()
        await q.edit_message_text(f"🏆 فاز {name} في {'تحدي السرعة' if kind=='speed' else 'فك الكلمة'} وربح {points} نقاط ⭐")
        return

    if data.startswith("game_daily:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.",show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "هذا القروب غير مسجل.", show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.",show_alert=True); return
        day=datetime.now(ZoneInfo("Asia/Riyadh")).date().isoformat()
        existing=db.execute("SELECT completed FROM daily_game_challenges WHERE chat_id=? AND challenge_date=?",(chat_id,day)).fetchone()
        if existing:
            await safe_callback_answer(q, "تم نشر تحدي اليوم بالفعل في هذا القروب.",show_alert=True); return
        # Draw today's challenge from the embedded offline question bank.
        bank_category = random.choice(list(QUIZ_CATEGORIES.keys()))
        question, options, correct_index, explanation = random.choice(QUIZ_CATEGORIES[bank_category][1])
        answer = options[correct_index]
        # Store index + answer + explanation; callback data stays short and within Telegram's 64-byte limit.
        db_answer = f"{correct_index}|{answer}|{explanation}"
        db.execute("INSERT INTO daily_game_challenges(chat_id,challenge_date,answer) VALUES(?,?,?)",(chat_id,day,db_answer)); db.commit()
        buttons=[[InlineKeyboardButton(x,callback_data=f"game_daily_answer:{chat_id}:{day}:{i}")] for i,x in enumerate(options)]
        try:
            sent=await context.bot.send_message(chat_id,f"🎯 تحدي اليوم — {QUIZ_CATEGORIES[bank_category][0]}\n\n{question}\nأول إجابة صحيحة تحصل على 5 نقاط ⭐\n\n💡 معلومة: تظهر بعد حسم الجولة.",reply_markup=InlineKeyboardMarkup(buttons))
            db.execute("UPDATE daily_game_challenges SET message_id=? WHERE chat_id=? AND challenge_date=?",(sent.message_id,chat_id,day)); db.commit()
        except Exception:
            db.execute("DELETE FROM daily_game_challenges WHERE chat_id=? AND challenge_date=? AND completed=0",(chat_id,day)); db.commit()
            await safe_callback_answer(q, "تعذر نشر التحدي. تأكد أن البوت يستطيع إرسال الرسائل.",show_alert=True); return
        await q.edit_message_text("تم نشر تحدي اليوم في القروب 🎯",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("game_daily_answer:"):
        try: _,chat_raw,day,answer_raw=data.split(":",3); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.",show_alert=True); return
        row=db.execute("SELECT answer,completed FROM daily_game_challenges WHERE chat_id=? AND challenge_date=?",(chat_id,day)).fetchone()
        if not row or row[1]: await safe_callback_answer(q, "انتهى تحدي اليوم أو فاز به عضو آخر.",show_alert=True); return
        stored_answer = str(row[0])
        if "|" in stored_answer:
            try: correct_index_raw, correct_answer, explanation = stored_answer.split("|", 2)
            except ValueError: await safe_callback_answer(q, "تعذر قراءة بيانات التحدي.", show_alert=True); return
            if answer_raw != correct_index_raw:
                await safe_callback_answer(q, "إجابة غير صحيحة، حاول مرة أخرى.",show_alert=True); return
        else:
            # Backward compatibility for daily challenges created by older bot versions.
            correct_answer, explanation = stored_answer, "أحسنت! إجابة صحيحة من بنك المعلومات الداخلي."
            if answer_raw != correct_answer:
                await safe_callback_answer(q, "إجابة غير صحيحة، حاول مرة أخرى.",show_alert=True); return
        db.execute("UPDATE daily_game_challenges SET completed=1,winner_id=? WHERE chat_id=? AND challenge_date=? AND completed=0",(uid,chat_id,day))
        if db.execute("SELECT changes()").fetchone()[0]!=1: db.commit(); await safe_callback_answer(q, "سبق أن فاز عضو آخر.",show_alert=True); return
        name=q.from_user.full_name or str(uid)
        db.execute("INSERT OR IGNORE INTO game_scores(chat_id,user_id,display_name) VALUES(?,?,?)",(chat_id,uid,name))
        db.execute("UPDATE game_scores SET points=points+5,wins=wins+1,display_name=? WHERE chat_id=? AND user_id=?",(name,chat_id,uid)); record_game_points(chat_id,uid,5,"daily"); db.commit()
        try: await context.bot.send_message(chat_id,f"🎉 مبروك {name}! فزت بتحدي اليوم وربحت 5 نقاط ⭐")
        except Exception: pass
        await safe_callback_answer(q, f"إجابة صحيحة! {correct_answer}. {explanation} تم تسجيل 5 نقاط.",show_alert=True); return

    if data.startswith("game_xo_start:"):
        try: chat_id=int(data.split(":",1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح.",show_alert=True); return
        if not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone(): await safe_callback_answer(q, "القروب غير مسجل.",show_alert=True); return
        cfg=db.execute("SELECT enabled FROM game_config WHERE chat_id=?",(chat_id,)).fetchone()
        if cfg and not cfg[0]: await safe_callback_answer(q, "الألعاب متوقفة في هذا القروب.",show_alert=True); return
        token=uuid.uuid4().hex[:12]
        db.execute("INSERT INTO game_xo(token,chat_id,player_x,board,turn,status,created_at) VALUES(?,?,?,'         ','X','waiting',?)",(token,chat_id,uid,now())); db.commit()
        try:
            sent=await context.bot.send_message(chat_id,f"⭕ تحدي إكس أو\nاللاعب الأول: {q.from_user.full_name} (❌)\nمن يرغب بالمنافسة يضغط الانضمام:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🙋 انضم كلاعب ⭕",callback_data=f"xo_join:{token}")]]))
            db.execute("UPDATE game_xo SET message_id=? WHERE token=?",(sent.message_id,token)); db.commit()
        except Exception:
            db.execute("DELETE FROM game_xo WHERE token=?",(token,)); db.commit(); await safe_callback_answer(q, "تعذر إرسال اللعبة إلى القروب.",show_alert=True); return
        await q.edit_message_text("تم نشر تحدي إكس أو في القروب. ينتظر لاعبًا ثانيًا.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الألعاب",callback_data=f"games_group:{chat_id}")]])); return

    if data.startswith("xo_join:"):
        token=data.split(":",1)[1]; row=db.execute("SELECT chat_id,player_x,player_o,board,turn,status,message_id FROM game_xo WHERE token=?",(token,)).fetchone()
        if not row: await safe_callback_answer(q, "اللعبة غير موجودة.",show_alert=True); return
        chat_id,px,po,board,turn,status,message_id=row
        if status!="waiting": await safe_callback_answer(q, "اللعبة بدأت أو انتهت.",show_alert=True); return
        if uid==px: await safe_callback_answer(q, "تحتاج لاعبًا ثانيًا.",show_alert=True); return
        db.execute("UPDATE game_xo SET player_o=?,status='playing' WHERE token=? AND status='waiting'",(uid,token)); db.commit()
        text=f"⭕ إكس أو — ❌ {px} ضد ⭕ {uid}\nالدور: ❌ اللاعب الأول"
        try: await q.edit_message_text(text,reply_markup=game_board_markup(token,board))
        except Exception: await safe_callback_answer(q, "تعذر تحديث لوحة اللعبة.",show_alert=True); return
        return

    if data.startswith("xo_move:"):
        try: _,token,pos_raw=data.split(":",2); pos=int(pos_raw)
        except ValueError: await safe_callback_answer(q, "حركة غير صحيحة.",show_alert=True); return
        row=db.execute("SELECT chat_id,player_x,player_o,board,turn,status,message_id FROM game_xo WHERE token=?",(token,)).fetchone()
        if not row: await safe_callback_answer(q, "اللعبة غير موجودة.",show_alert=True); return
        chat_id,px,po,board,turn,status,message_id=row
        if status!="playing" or uid not in (px,po): await safe_callback_answer(q, "لست لاعبًا في هذه الجولة.",show_alert=True); return
        if (turn=="X" and uid!=px) or (turn=="O" and uid!=po): await safe_callback_answer(q, "ليس دورك الآن.",show_alert=True); return
        if pos<0 or pos>8 or board[pos]!=" ": await safe_callback_answer(q, "هذا المربع غير متاح.",show_alert=True); return
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
        except Exception: await safe_callback_answer(q, "تم تسجيل الحركة، لكن تعذر تحديث اللوحة.",show_alert=True)
        return

    if data == "statistics" or data.startswith("group:") or data == "owner_ai_report":
        await safe_callback_answer(q, "تمت إزالة الإحصائيات وتحليل القروبات من لوحة البوت.", show_alert=True)
        return

    if data == "group_controls" or data.startswith(("gctrl_group:", "gctrl_toggle:", "gctrl_link:")):
        await safe_callback_answer(q, "تمت إزالة لوحة الإحصائيات والتحليل وقفل الدخول.", show_alert=True)
        return

    if data.startswith("welcome_group:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True); return
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
            await safe_callback_answer(q, "ليس لديك صلاحية هذا القروب", show_alert=True); return
        context.user_data["awaiting_welcome_chat"] = chat_id
        await q.edit_message_text("أرسل رسالة الترحيب الجديدة في الخاص مع البوت. استخدم {name} لاسم العضو و {group} لاسم القروب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data=f"welcome_group:{chat_id}")]]))
        return

    if data.startswith("welcome_off:"):
        chat_id = int(data.split(":", 1)[1])
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "ليس لديك صلاحية هذا القروب", show_alert=True); return
        db.execute("DELETE FROM welcome_messages WHERE chat_id=?", (chat_id,))
        db.commit()
        log_action(chat_id, uid, "welcome_off", "تم إيقاف الترحيب")
        await q.edit_message_text("✅ تم إيقاف رسالة الترحيب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ القروبات", callback_data="welcome")]]))
        return

    if data == "roles_groups":
        if not (is_owner(uid) or is_delegated_owner(uid)):
            await safe_callback_answer(q, "⛔ لا تملك صلاحية توزيع الرتب.", show_alert=True)
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
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True)
            return
        if not await can_access_group(context.bot, uid, chat_id):
            await safe_callback_answer(q, "لا تملك صلاحية هذا القروب.", show_alert=True)
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
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish")):
            await safe_callback_answer(q, "لا تملك صلاحية النشر؛ اطلب منحها من المالك الأساسي.", show_alert=True)
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
        if is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish"):
            rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات المتاحة لي", callback_data="broadcast_select_all")])
        rows.append([InlineKeyboardButton("➡️ متابعة وإرسال الإعلان", callback_data="broadcast_prepare")])
        rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")])
        await q.edit_message_text("📣 نشر إعلان بالقروبات\n\nحدد القروبات التي تريد النشر فيها. المالك المالك المفوّض يمكنه النشر إلى القروبات المسجلة التي يوجد فيها البوت.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("broadcast_toggle:"):
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish")):
            await safe_callback_answer(q, "ليس لديك صلاحية.", show_alert=True)
            return
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True)
            return
        if not await can_publish_to_live(context.bot, uid, chat_id):
            await safe_callback_answer(q, "النشر متاح فقط في القروبات المسجلة التي أنت عضو فيها.", show_alert=True)
            return
        selected = set(context.user_data.get("broadcast_selected_groups", []))
        if chat_id in selected: selected.remove(chat_id)
        else: selected.add(chat_id)
        context.user_data["broadcast_selected_groups"] = sorted(selected)
        # Refresh only authorized group options.
        groups = [(cid, title) for cid, title in db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall() if await can_publish_to_live(context.bot, uid, cid)]
        rows = [[InlineKeyboardButton(("✅ " if cid in selected else "⬜️ ") + (title or str(cid)), callback_data=f"broadcast_toggle:{cid}")] for cid, title in groups[:30]]
        if is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish"): rows.append([InlineKeyboardButton("🌐 تحديد جميع القروبات المتاحة لي", callback_data="broadcast_select_all")])
        rows += [[InlineKeyboardButton("➡️ متابعة وإرسال الإعلان", callback_data="broadcast_prepare")], [InlineKeyboardButton("❌ إلغاء", callback_data="broadcast_cancel")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "broadcast_select_all":
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish")):
            await safe_callback_answer(q, "ليس لديك صلاحية النشر.", show_alert=True)
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
            await safe_callback_answer(q, "حدد قروبًا واحدًا على الأقل تملك صلاحية النشر فيه.", show_alert=True)
            return
        context.user_data.pop("awaiting_admin_action", None)
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
        if not (is_owner(uid) or is_delegated_owner(uid) or can_use_capability(uid, "publish")) or not draft or not selected:
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
        allowed_archive_groups = capability_group_ids(uid, "archive")
        if not (is_owner(uid) or allowed_archive_groups):
            await q.edit_message_text("⛔ لا تملك صلاحية سجل التحويلات والملفات.")
            return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        if not is_owner(uid): groups = [(cid,title) for cid,title in groups if cid in allowed_archive_groups and can_use_capability(uid,"archive",cid)]
        rows = [[InlineKeyboardButton(f"📨 {title or chat_id}", callback_data=f"archive_group:{chat_id}")] for chat_id, title in groups[:30]]
        if not rows:
            rows = [[InlineKeyboardButton("لا توجد مجموعات مسجلة", callback_data="noop")]]
        rows.append([InlineKeyboardButton("🔎 البحث عن عضو أو ملف", callback_data="archive_search")])
        rows.append([InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")])
        await q.edit_message_text("📨 سجل التحويلات والملفات\nاختر مجموعة لعرض الرسائل المسجلة، أو ابحث برقم العضو/@username. يظهر فقط ما استقبله البوت وسُجل بعد تفعيل الأرشفة في القروب؛ لا يستطيع البوت قراءة محادثات أخرى.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("archive_group:"):
        try: chat_id = int(data.split(":", 1)[1])
        except ValueError: await safe_callback_answer(q, "معرّف القروب غير صحيح.", show_alert=True); return
        if not (is_owner(uid) or can_use_capability(uid, "archive", chat_id)):
            await safe_callback_answer(q, "لا تملك صلاحية سجل هذا القروب.", show_alert=True)
            return
        rows = db.execute("SELECT chat_title,display_name,username,user_id,message_text,created_at,content_type,forward_origin,message_link,media_file_id FROM message_archive WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            text = "لا توجد رسائل مسجلة لهذه المجموعة. تأكد من تفعيل الأرشفة داخلها بالأمر /archive_on بعد إعلام الأعضاء."
        else:
            lines = ["📨 أحدث الرسائل والتحويلات والملفات — حسب الصلاحيات الممنوحة"]
            for title, name, username, user_id, body, created, content_type, forward_origin, message_link, media_file_id in rows:
                media_note = "\n📎 يوجد مرفق محفوظ بالمعرّف" if media_file_id else ""
                lines.append(f"\n📍 {title or chat_id}\n👤 {name or 'عضو'} — ID: {user_id}" + (f" (@{username})" if username else "") + f"\n🕒 {created} | النوع: {content_type or 'غير محدد'}{media_note}\n↪️ المصدر: {forward_origin or 'غير متاح/ليست رسالة محوّلة'}\n💬 {(body or '[رسالة غير نصية]')[:160]}" + (f"\n🔗 {message_link}" if message_link else ""))
            text = "\n".join(lines)[:3900]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 البحث عن عضو أو ملف", callback_data="archive_search")],[InlineKeyboardButton("⬅️ سجل التحويلات والملفات", callback_data="archive")],[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]), disable_web_page_preview=True)
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
            await safe_callback_answer(q, "إضافة المالك المفوّض للمالك الأساسي فقط.", show_alert=True)
            return
        if q.message.chat.type != ChatType.PRIVATE:
            await safe_callback_answer(q, "افتح اللوحة في الخاص مع البوت.", show_alert=True)
            return
        context.user_data["awaiting_delegated_owner_id"] = True
        await q.edit_message_text(
            "➕ إضافة مالك مفوّض\n\nأرسل رقم المستخدم (User ID) في هذه المحادثة الخاصة.\nلن تُمنح الصلاحية إلا بعد إدخال الرقم هنا، ولا ترسل رمز البوت أو التوكن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء والعودة", callback_data="delegated_owner")]])
        )
        return

    if data.startswith("delegated_manage:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "إدارة صلاحيات المالك المفوض للمالك الأساسي فقط.", show_alert=True)
            return
        try: target_uid = int(data.split(":", 1)[1])
        except ValueError:
            await safe_callback_answer(q, "رقم المستخدم غير صحيح.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await q.edit_message_text("هذا المالك المفوض لم يعد مسجلًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ المالكون المفوضون", callback_data="delegated_owner")]])); return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
        buttons = [[InlineKeyboardButton("🌐 صلاحيات جميع القروبات / سحبها", callback_data=f"delegated_scope:{target_uid}:all")]]
        for cap, label in STAFF_CAPABILITY_LABELS.items():
            granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone())
            buttons.append([InlineKeyboardButton(("✅ " if granted else "⬜️ ") + label + (" — جميع القروبات" if granted else " — غير ممنوحة"), callback_data=f"delegcap:{target_uid}:{cap}")])
        buttons.append([InlineKeyboardButton("📍 تحديد صلاحيات لقروبات محددة", callback_data=f"delegated_scope:{target_uid}:groups")])
        # Keep the legacy publish-permission editor available for older deployments/data.
        buttons.append([InlineKeyboardButton("📢 إدارة نطاق النشر بالتفصيل", callback_data=f"delegated_publish_legacy:{target_uid}")])
        buttons += [[InlineKeyboardButton("🛑 عزل هذا المالك المفوض", callback_data=f"delegated_remove_confirm:{target_uid}")], [InlineKeyboardButton("⬅️ قائمة المالكين", callback_data="delegated_owner")]]
        await q.edit_message_text(f"👑 إدارة صلاحيات المالك المفوض\nالاسم: {user_display_name(target_uid)}\n\nاختر كل صلاحية على حدة لمنحها أو سحبها. الصلاحية العامة تشمل القروبات الحالية والمستقبلية، ويمكن تخصيص قروبات من زر النطاق.\n✅ ممنوحة  |  ⬜️ غير ممنوحة", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("delegated_scope:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "المالك الأساسي فقط يستطيع تحديد نطاق الصلاحيات.", show_alert=True); return
        try: _, target_raw, scope = data.split(":", 2); target_uid = int(target_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await safe_callback_answer(q, "المالك المفوض غير مسجل.", show_alert=True); return
        if scope == "all":
            buttons = []
            for cap, label in STAFF_CAPABILITY_LABELS.items():
                granted = bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid, cap)).fetchone())
                buttons.append([InlineKeyboardButton(("🟢 سحب" if granted else "🟢 منح") + " جميع القروبات: " + label, callback_data=f"delegcap:{target_uid}:{cap}")])
            buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"delegated_manage:{target_uid}")])
            await q.edit_message_text("🌐 نطاق جميع القروبات\nاضغط على الصلاحية لمنحها لجميع القروبات أو سحبها. يشمل ذلك القروبات المستقبلية.", reply_markup=InlineKeyboardMarkup(buttons)); return
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
        buttons = [[InlineKeyboardButton("🏘️ " + ((title or str(cid))[:45]), callback_data=f"delegated_group:{target_uid}:{cid}")] for cid, title in groups[:60]]
        if not buttons: buttons = [[InlineKeyboardButton("لا توجد قروبات مسجلة", callback_data="noop")]]
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"delegated_manage:{target_uid}")])
        await q.edit_message_text("📍 اختر القروب الذي تريد تحديد صلاحياته لهذا المالك المفوض:", reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith("delegated_group:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "المالك الأساسي فقط يستطيع تحديد الصلاحيات.", show_alert=True); return
        try: _, target_raw, chat_raw = data.split(":", 2); target_uid=int(target_raw); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid) or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone():
            await safe_callback_answer(q, "المستخدم أو القروب غير صحيح.", show_alert=True); return
        title_row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        rows=[]
        for cap,label in STAFF_CAPABILITY_LABELS.items():
            effective = can_use_capability(target_uid, cap, chat_id)
            rows.append([InlineKeyboardButton(("✅ " if effective else "⬜️ ")+label, callback_data=f"delegated_gcap:{target_uid}:{chat_id}:{cap}")])
        rows.append([InlineKeyboardButton("⬅️ اختيار قروب آخر", callback_data=f"delegated_scope:{target_uid}:groups")])
        rows.append([InlineKeyboardButton("⬅️ إدارة المالك المفوض", callback_data=f"delegated_manage:{target_uid}")])
        await q.edit_message_text(f"📍 صلاحيات القروب: {(title_row[0] or chat_id)}\nاختر الصلاحيات التي تريد منحها أو سحبها. عند وجود صلاحية عامة، يتيح هذا الخيار استثناء هذا القروب.", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("delegcap:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "منح وسحب صلاحيات المالك المفوض للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, cap = data.split(":", 2); target_uid=int(target_raw)
        except ValueError: await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid) or cap not in STAFF_CAPABILITY_LABELS:
            await safe_callback_answer(q, "المستخدم أو الصلاحية غير صحيحة.", show_alert=True); return
        exists = db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0", (target_uid,cap)).fetchone()
        if exists:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=?", (target_uid,cap))
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target_uid,cap))
            action="تم سحب الصلاحية من جميع القروبات"
        else:
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=?", (target_uid,cap))
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)", (target_uid,cap,0,uid,now()))
            action="تم منح الصلاحية لجميع القروبات الحالية والمستقبلية"
        db.commit(); log_action(0,uid,"delegated_capability_changed",f"target={target_uid}; capability={cap}; action={action}")
        await safe_callback_answer(q, action)
        # Re-open the same delegate's permissions page with fresh state.
        groups = db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title COLLATE NOCASE").fetchall()
        buttons = [[InlineKeyboardButton("🌐 صلاحيات جميع القروبات / سحبها", callback_data=f"delegated_scope:{target_uid}:all")]]
        for key,label in STAFF_CAPABILITY_LABELS.items():
            granted=bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0",(target_uid,key)).fetchone())
            buttons.append([InlineKeyboardButton(("✅ " if granted else "⬜️ ")+label+(" — جميع القروبات" if granted else " — غير ممنوحة"),callback_data=f"delegcap:{target_uid}:{key}")])
        buttons.append([InlineKeyboardButton("📍 تحديد صلاحيات لقروبات محددة",callback_data=f"delegated_scope:{target_uid}:groups")])
        buttons += [[InlineKeyboardButton("🛑 عزل هذا المالك المفوض",callback_data=f"delegated_remove_confirm:{target_uid}")],[InlineKeyboardButton("⬅️ قائمة المالكين",callback_data="delegated_owner")]]
        await q.edit_message_text(f"👑 إدارة صلاحيات المالك المفوض\n{user_display_name(target_uid)}\n\n{action}.",reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith("delegated_gcap:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "منح وسحب صلاحيات القروبات للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, chat_raw, cap = data.split(":",3); target_uid=int(target_raw); chat_id=int(chat_raw)
        except ValueError: await safe_callback_answer(q,"بيانات غير صحيحة.",show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid) or cap not in STAFF_CAPABILITY_LABELS or not db.execute("SELECT 1 FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone():
            await safe_callback_answer(q,"المستخدم أو القروب أو الصلاحية غير صحيحة.",show_alert=True); return
        global_grant=bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=0",(target_uid,cap)).fetchone())
        local_grant=bool(db.execute("SELECT 1 FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)).fetchone())
        denied=bool(db.execute("SELECT 1 FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)).fetchone())
        if global_grant and not denied:
            db.execute("INSERT OR REPLACE INTO staff_capability_denials(user_id,capability,chat_id,revoked_by,revoked_at) VALUES(?,?,?,?,?)",(target_uid,cap,chat_id,uid,now()))
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id))
            action="تم استثناء القروب وسحب الصلاحية منه"
        elif denied:
            db.execute("DELETE FROM staff_capability_denials WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)); action="تم إلغاء الاستثناء وإعادة الصلاحية العامة لهذا القروب"
        elif local_grant:
            db.execute("DELETE FROM staff_capabilities WHERE user_id=? AND capability=? AND chat_id=?",(target_uid,cap,chat_id)); action="تم سحب الصلاحية من هذا القروب"
        else:
            db.execute("INSERT OR REPLACE INTO staff_capabilities(user_id,capability,chat_id,granted_by,granted_at) VALUES(?,?,?,?,?)",(target_uid,cap,chat_id,uid,now())); action="تم منح الصلاحية لهذا القروب"
        db.commit(); log_action(chat_id,uid,"delegated_group_capability_changed",f"target={target_uid}; capability={cap}; action={action}")
        await safe_callback_answer(q,action)
        title_row=db.execute("SELECT title FROM watched_groups WHERE chat_id=?",(chat_id,)).fetchone()
        rows=[]
        for key,label in STAFF_CAPABILITY_LABELS.items():
            effective=can_use_capability(target_uid,key,chat_id)
            rows.append([InlineKeyboardButton(("✅ " if effective else "⬜️ ")+label,callback_data=f"delegated_gcap:{target_uid}:{chat_id}:{key}")])
        rows += [[InlineKeyboardButton("⬅️ اختيار قروب آخر",callback_data=f"delegated_scope:{target_uid}:groups")],[InlineKeyboardButton("⬅️ إدارة المالك المفوض",callback_data=f"delegated_manage:{target_uid}")]]
        await q.edit_message_text(f"📍 صلاحيات القروب: {(title_row[0] or chat_id)}\n{action}.",reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("delegated_publish_legacy:"):
        if not is_owner(uid):
            await safe_callback_answer(q,"هذه اللوحة للمالك الأساسي فقط.",show_alert=True); return
        target_uid=int(data.split(":",1)[1])
        groups=db.execute("SELECT chat_id,title FROM watched_groups ORDER BY title").fetchall()
        all_granted=bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=0",(target_uid,)).fetchone())
        buttons=[[InlineKeyboardButton(("🌐 سحب صلاحية النشر العامة" if all_granted else "🌐 منح صلاحية النشر العامة"),callback_data=f"delegated_all:{target_uid}:{0 if all_granted else 1}")]]
        for cid,title in groups[:30]:
            granted=all_granted or bool(db.execute("SELECT 1 FROM publish_permissions WHERE user_id=? AND chat_id=?",(target_uid,cid)).fetchone())
            buttons.append([InlineKeyboardButton(("✅ " if granted else "⬜️ ")+(title or str(cid)),callback_data=f"pubperm_toggle:{target_uid}:{cid}")])
        buttons.append([InlineKeyboardButton("⬅️ رجوع",callback_data=f"delegated_manage:{target_uid}")])
        await q.edit_message_text("📢 نطاق النشر والإعلانات لهذا المالك المفوض:",reply_markup=InlineKeyboardMarkup(buttons)); return

    if data.startswith("pubperm_toggle:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "هذا الإجراء للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, chat_raw = data.split(":", 2); target_uid=int(target_raw); chat_id=int(chat_raw)
        except ValueError:
            await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await safe_callback_answer(q, "المالك المفوض غير مسجل.", show_alert=True); return
        group = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if not group:
            await safe_callback_answer(q, "القروب غير مسجل.", show_alert=True); return
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
        await safe_callback_answer(q, action)
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
            await safe_callback_answer(q, "هذا الإجراء للمالك الأساسي فقط.", show_alert=True); return
        try: _, target_raw, enable_raw = data.split(":", 2); target_uid=int(target_raw); enable=int(enable_raw)
        except ValueError:
            await safe_callback_answer(q, "بيانات غير صحيحة.", show_alert=True); return
        if not is_delegated_owner(target_uid) or is_owner(target_uid):
            await safe_callback_answer(q, "المالك المفوض غير مسجل.", show_alert=True); return
        if enable:
            db.execute("INSERT OR REPLACE INTO publish_permissions(user_id,chat_id,granted_by,granted_at) VALUES(?,0,?,?)", (target_uid,uid,now()))
        else:
            db.execute("DELETE FROM publish_permissions WHERE user_id=?", (target_uid,))
        db.commit()
        await safe_callback_answer(q, "تم تحديث الصلاحية.")
        # Reuse the management view by re-entering it.
        await q.edit_message_text("تم تحديث الصلاحيات. افتح إدارة هذا المالك المفوض مجددًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↻ تحديث الصلاحيات", callback_data=f"delegated_manage:{target_uid}")], [InlineKeyboardButton("⬅️ المالكون المفوضون", callback_data="delegated_owner")]]))
        return

    if data.startswith("delegated_remove_confirm:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "عزل المالك المفوض للمالك الأساسي فقط.", show_alert=True); return
        try: target_uid=int(data.split(":",1)[1])
        except ValueError:
            await safe_callback_answer(q, "رقم المستخدم غير صحيح.", show_alert=True); return
        await q.edit_message_text(f"⚠️ هل تريد عزل {user_display_name(target_uid)} وإلغاء كل صلاحياته؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نعم، عزله", callback_data=f"delegated_remove:{target_uid}"), InlineKeyboardButton("لا، رجوع", callback_data=f"delegated_manage:{target_uid}")]]))
        return

    if data.startswith("delegated_remove:"):
        if not is_owner(uid):
            await safe_callback_answer(q, "عزل المالك المفوض للمالك الأساسي فقط.", show_alert=True); return
        try: target_uid=int(data.split(":",1)[1])
        except ValueError:
            await safe_callback_answer(q, "رقم المستخدم غير صحيح.", show_alert=True); return
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
        if is_owner(uid):
            await q.edit_message_text("🔔 إعدادات إشعارات دخول وخروج الأعضاء. اختر القروب ثم شغّل أو أوقف الإشعارات.", reply_markup=group_controls_list_markup())
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
        except ValueError: await safe_callback_answer(q, "معرّف غير صحيح", show_alert=True); return
        if not await can_access_group(context.bot, uid, chat_id): await safe_callback_answer(q, "لا تملك صلاحية هذا القروب", show_alert=True); return
        rows = db.execute("SELECT action,details,created_at FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 25", (chat_id,)).fetchall()
        title = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
        body = "\n".join(f"• {created} — {action}: {details or ''}" for action,details,created in rows) or "لا توجد عمليات مسجلة بعد."
        await q.edit_message_text(f"📋 سجل القروب: {title[0] if title else chat_id}\n\n{body}"[:3900], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ السجلات", callback_data="logs")],[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]])); return

    if data == "settings":
        if not (is_owner(uid) or is_general_manager(uid) or is_protection_manager(uid) or is_delegated_owner(uid)):
            await q.edit_message_text("⛔ ليس لديك صلاحية الإعدادات."); return
        if q.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            chat_id = q.message.chat.id
            if not await can_access_group(context.bot, uid, chat_id): await safe_callback_answer(q, "لا تملك صلاحية هذا القروب", show_alert=True); return
            await q.edit_message_text(settings_text(0), reply_markup=settings_page_markup(chat_id, 0)); return
        groups = await accessible_groups(context.bot, uid)
        rows = [[InlineKeyboardButton(f"⚙️ {title or cid}", callback_data=f"settings_group:{cid}")] for cid,title in groups[:30]]
        rows += [[InlineKeyboardButton("⬅️ الرئيسية", callback_data="home")]]
        await q.edit_message_text("⚙️ اختر القروب الذي تريد إدارة إعداداته:", reply_markup=InlineKeyboardMarkup(rows)); return



async def callback(update, context):
    """Dispatch a button action and acknowledge it exactly once.

    Branch-specific alerts are allowed to answer first. Successful branches that
    do not explicitly answer receive a neutral acknowledgement on return.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return await _callback_impl(update, context)
    query_id = str(getattr(query, "id", ""))
    if query_id:
        _ACTIVE_CALLBACK_IDS.add(query_id)
        _ANSWERED_CALLBACK_IDS.discard(query_id)
    try:
        return await _callback_impl(update, context)
    finally:
        try:
            if query_id and query_id not in _ANSWERED_CALLBACK_IDS:
                await safe_callback_answer(query)
            elif not query_id:
                await safe_callback_answer(query)
        finally:
            if query_id:
                _ACTIVE_CALLBACK_IDS.discard(query_id)
                _ANSWERED_CALLBACK_IDS.discard(query_id)


def owner_autoreply_groups_markup(groups):
    rows = [[InlineKeyboardButton(f"💬 {title or chat_id}", callback_data=f"owner_ar_group:{chat_id}")] for chat_id, title in groups[:50]]
    if not rows:
        rows = [[InlineKeyboardButton("لا توجد قروبات مسجلة بعد", callback_data="noop")]]
    rows.insert(0, [InlineKeyboardButton("🌐 إدارة موحّدة — جميع القروبات دفعة واحدة", callback_data="owner_ar_all")])
    rows.append([InlineKeyboardButton("⬅️ لوحة التحكم", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def owner_autoreply_group_markup(chat_id):
    rows = db.execute("SELECT trigger_text,reply_text,enabled FROM auto_replies WHERE chat_id=? ORDER BY trigger_text", (chat_id,)).fetchall()
    buttons = [
        [InlineKeyboardButton("➕ إضافة رد جديد", callback_data=f"owner_ar_add:{chat_id}")],
        [InlineKeyboardButton("✏️ تعديل رد موجود", callback_data=f"owner_ar_edit:{chat_id}")],
        [InlineKeyboardButton("🗑️ حذف رد", callback_data=f"owner_ar_delete:{chat_id}")],
        [InlineKeyboardButton("🔄 تشغيل الردود", callback_data=f"owner_ar_on:{chat_id}"), InlineKeyboardButton("⏸️ إيقاف الردود", callback_data=f"owner_ar_off:{chat_id}")],
        [InlineKeyboardButton("⬅️ القروبات", callback_data="owner_autoreplies")]
    ]
    return rows, buttons

async def owner_autoreply_text_handler(update, context):
    """Receives auto-reply setup text, while reserving AI status aliases."""
    if not update.effective_user or not update.effective_message or update.effective_chat.type != ChatType.PRIVATE:
        return
    raw = (update.effective_message.text or "").strip()
    # Let the owner check AI status even while an auto-reply wizard is waiting for input.
    if _is_aistatus_alias(raw):
        await aistatus_cmd(update, context)
        return
    state = context.user_data.get("owner_autoreply_state")
    if not state:
        return
    if not (is_owner(update.effective_user.id) or is_delegated_owner(update.effective_user.id)):
        context.user_data.pop("owner_autoreply_state", None)
        return
    raw = (update.effective_message.text or "").strip()
    if not raw or raw.startswith("/"):
        await update.effective_message.reply_text("أرسل النص المطلوب بدون أمر. أرسل /cancel لإلغاء العملية.")
        return
    if raw.lower() == "/cancel":
        context.user_data.pop("owner_autoreply_state", None)
        await update.effective_message.reply_text("تم إلغاء العملية.")
        return
    action = state["action"]
    if action in ("add_all", "edit_all", "delete_all"):
        groups = [r[0] for r in db.execute("SELECT chat_id FROM watched_groups ORDER BY title").fetchall()]
        if not groups:
            context.user_data.pop("owner_autoreply_state", None)
            await update.effective_message.reply_text("لا توجد قروبات مسجلة لتطبيق التغيير عليها.")
            return
        if action == "delete_all":
            trigger = raw.strip()
            if not trigger or len(trigger) > 100:
                await update.effective_message.reply_text("أرسل عبارة صحيحة لا تتجاوز 100 حرف.")
                return
            deleted = 0
            for group_id in groups:
                cur = db.execute("DELETE FROM auto_replies WHERE chat_id=? AND trigger_text=?", (group_id, trigger))
                deleted += cur.rowcount
                if cur.rowcount:
                    log_action(group_id, update.effective_user.id, "auto_reply_deleted", trigger)
            db.commit()
            context.user_data.pop("owner_autoreply_state", None)
            await update.effective_message.reply_text(f"🗑️ اكتمل الحذف من جميع القروبات المسجلة. عدد السجلات المحذوفة: {deleted}.")
            return
        if "|" not in raw:
            await update.effective_message.reply_text("أرسل العبارة والرد بهذا الشكل:\nالعبارة | الرد")
            return
        trigger, reply = (x.strip() for x in raw.split("|", 1))
        if not trigger or not reply or len(trigger) > 100 or len(reply) > 1000:
            await update.effective_message.reply_text("تأكد من كتابة العبارة والرد، بحد أقصى 100 حرف للعبارة و1000 للرد.")
            return
        if action == "edit_all":
            found = any(db.execute("SELECT 1 FROM auto_replies WHERE chat_id=? AND trigger_text=?", (group_id, trigger)).fetchone() for group_id in groups)
            if not found:
                context.user_data.pop("owner_autoreply_state", None)
                await update.effective_message.reply_text("لم أجد العبارة في أي قروب. استخدم الإضافة للتعميم أو أعد المحاولة بعبارة موجودة.")
                return
        for group_id in groups:
            ensure_auto_replies(group_id)
            db.execute(
                "INSERT INTO auto_replies(chat_id,trigger_text,reply_text,enabled) VALUES(?,?,?,1) "
                "ON CONFLICT(chat_id,trigger_text) DO UPDATE SET reply_text=excluded.reply_text,enabled=1",
                (group_id, trigger, reply))
            db.execute("UPDATE auto_reply_settings SET enabled=1 WHERE chat_id=?", (group_id,))
            log_action(group_id, update.effective_user.id, "auto_reply_added" if action == "add_all" else "auto_reply_edited", trigger)
        db.commit()
        context.user_data.pop("owner_autoreply_state", None)
        await update.effective_message.reply_text(
            f"✅ تم {'تعميم' if action == 'add_all' else 'تعديل وتعميم'} الرد على جميع القروبات المسجلة ({len(groups)}).\n\n"
            f"🗣️ العبارة: {trigger}\n💬 الرد: {reply}\n\n"
            "ملاحظة: تم حفظ الإعدادات في قاعدة البيانات؛ يجب أن تكون القروبات مسجلة في البوت.")
        return

    chat_id = int(state["chat_id"])
    if action in ("add", "edit"):
        if " | " not in raw and "|" not in raw:
            await update.effective_message.reply_text("أرسل العبارة والرد بهذا الشكل:\nالعبارة | الرد الذي سيظهر في القروب")
            return
        trigger, reply = (x.strip() for x in raw.split("|", 1))
        if not trigger or not reply or len(trigger) > 100 or len(reply) > 1000:
            await update.effective_message.reply_text("تأكد من كتابة العبارة والرد، بحد أقصى 100 حرف للعبارة و1000 للرد.")
            return
        ensure_auto_replies(chat_id)
        existing = db.execute("SELECT trigger_text FROM auto_replies WHERE chat_id=?", (chat_id,)).fetchall()
        matched = next((row[0] for row in existing if normalize_auto_reply(row[0]) == normalize_auto_reply(trigger)), None)
        if action == "edit" and not matched:
            await update.effective_message.reply_text("لم أجد عبارة مطابقة للتعديل. افتح إدارة الردود واختر عبارة موجودة.")
            context.user_data.pop("owner_autoreply_state", None)
            return
        if matched and action == "edit":
            db.execute("UPDATE auto_replies SET reply_text=?,enabled=1 WHERE chat_id=? AND trigger_text=?", (reply, chat_id, matched))
            trigger = matched
            event = "auto_reply_edited"
        else:
            db.execute("INSERT INTO auto_replies(chat_id,trigger_text,reply_text,enabled) VALUES(?,?,?,1) ON CONFLICT(chat_id,trigger_text) DO UPDATE SET reply_text=excluded.reply_text,enabled=1", (chat_id, trigger, reply))
            event = "auto_reply_added"
        db.execute("INSERT OR IGNORE INTO auto_reply_settings(chat_id,enabled) VALUES(?,1)", (chat_id,))
        db.execute("UPDATE auto_reply_settings SET enabled=1 WHERE chat_id=?", (chat_id,))
        db.commit()
        log_action(chat_id, update.effective_user.id, event, trigger)
        context.user_data.pop("owner_autoreply_state", None)
        await update.effective_message.reply_text(f"✅ تم {'تعديل' if action == 'edit' else 'إضافة'} الرد وتفعيله في القروب.\n\n🗣️ العبارة: {trigger}\n💬 الرد: {reply}")
        return
    context.user_data.pop("owner_autoreply_state", None)


async def cancel_owner_autoreply(update, context):
    keys = ("owner_autoreply_state", "awaiting_motivation_edit", "awaiting_general_inquiry", "inquiry_reply_id", "awaiting_channel_broadcast", "channel_broadcast_draft", "channel_broadcast_selected", "awaiting_staff_permissions_id", "awaiting_delegated_owner_id", "awaiting_unified_target_id", "awaiting_warning_grant", "awaiting_warning_log_search", "awaiting_archive_search", "awaiting_calendar_search", "awaiting_coursework_page", "awaiting_admin_action", "awaiting_broadcast", "broadcast_draft", "broadcast_selected_groups", "awaiting_welcome_chat", "external_support_state", "support_reply_ticket_id", "mass_ban_state", "massban_selected_groups", "awaiting_admin_action", "staff_task_state", "staff_shift_state", "staff_badge_state", "awaiting_channel_broadcast")
    cancelled = False
    for key in keys:
        if context.user_data.pop(key, None) is not None:
            cancelled = True
    await update.effective_message.reply_text("تم إلغاء العملية الحالية." if cancelled else "لا توجد عملية قيد التنفيذ.")


class HealthHandler(BaseHTTPRequestHandler):
    def _json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def _body(self):
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1_000_000)
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
    def _user_id(self):
        # Verify Telegram Mini App initData using Telegram's documented HMAC scheme.
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        if not TOKEN or not init_data: return None
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        supplied = pairs.pop("hash", None)
        if not supplied: return None
        import hashlib, hmac
        check = "\n".join(f"{k}={v}" for k,v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied): return None
        try:
            auth_date = int(pairs.get("auth_date", "0"))
            if abs(int(time.time()) - auth_date) > 86400: return None
            user = json.loads(pairs.get("user", "{}"))
            uid = int(user["id"])
            return uid if uid > 0 else None
        except Exception: return None
    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/health":
            raw = b"Telegram bot is running."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)
    def do_POST(self):
        # The interactive university portal and its private API have been retired.
        self.send_error(404)
    def log_message(self, format, *args): return

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

async def aistatus_cmd(update, context):
    uid = update.effective_user.id if update.effective_user else 0
    if not (is_owner(uid) or can_use_capability(uid, "ai") or capability_group_ids(uid, "ai")):
        await update.effective_message.reply_text("⛔ تحتاج إلى صلاحية الذكاء الاصطناعي من المالك."); return
    status = "مفتاح API مضبوط (لم يُختبر الاتصال بهذا الأمر)" if OPENAI_API_KEY else "غير مفعّل؛ التقارير تعمل بالتحليل المحلي"
    await update.effective_message.reply_text(
        f"🧠 حالة إعداد الذكاء الاصطناعي: {status}\nالنموذج المضبوط: {OPENAI_MODEL}\n\n"
        "تنبيه الخصوصية: عند استخدام /ai يُرسل نص السؤال الذي تكتبه إلى مزود الذكاء الاصطناعي لمعالجته. "
        "لا يعني ضبط المفتاح وحده أن الاتصال يعمل؛ راجع سجلات التشغيل عند حدوث خطأ."
    )


async def group_analysis_report_worker(app):
    """Daily owner/delegated-owner report at 20:30 Riyadh time."""
    while True:
        try:
            now_local = _riyadh_now()
            target = now_local.replace(hour=20, minute=30, second=0, microsecond=0)
            if target <= now_local:
                target += __import__("datetime").timedelta(days=1)
            await asyncio.sleep(max(30, (target - now_local).total_seconds()))
            recipients = {int(OWNER_ID)} if OWNER_ID else set()
            recipients.update(row[0] for row in db.execute("SELECT user_id FROM staff_capabilities WHERE capability='ai'").fetchall())
            for recipient in sorted(recipients):
                await send_group_analysis_report(app.bot, recipient)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("خطأ في عامل تقارير تحليل القروبات")
            await asyncio.sleep(60)




async def weekly_security_review_worker(app):
    """Weekly review of aggregate security/configuration signals; AI receives metrics only."""
    while True:
        try:
            now_local = _riyadh_now()
            target = now_local.replace(hour=9, minute=0, second=0, microsecond=0)
            days_ahead = (0 - now_local.weekday()) % 7
            if days_ahead == 0 and target <= now_local:
                days_ahead = 7
            target += __import__("datetime").timedelta(days=days_ahead)
            await asyncio.sleep(max(30, (target - now_local).total_seconds()))
            week_key = _week_start()
            if db.execute("SELECT 1 FROM security_review_runs WHERE week_start=?", (week_key,)).fetchone():
                await asyncio.sleep(3600)
                continue
            total_groups = int(db.execute("SELECT COUNT(*) FROM watched_groups").fetchone()[0] or 0)
            external_groups = int(db.execute("SELECT COUNT(*) FROM external_group_control WHERE is_external=1").fetchone()[0] or 0)
            disabled_groups = int(db.execute("SELECT COUNT(*) FROM external_group_control WHERE is_external=1 AND enabled=0").fetchone()[0] or 0)
            ai_enabled_groups = db.execute("SELECT chat_id,title FROM external_group_control WHERE is_external=1 AND ai_enabled=1 ORDER BY title").fetchall()
            links_open = db.execute("""SELECT w.chat_id,w.title FROM watched_groups w JOIN settings s ON s.chat_id=w.chat_id
                WHERE COALESCE(s.links,1)=0 ORDER BY w.title LIMIT 30""").fetchall()
            violation_count = int(db.execute("SELECT COUNT(*) FROM member_violations WHERE warning_count>0").fetchone()[0] or 0)
            active_bans = int(db.execute("SELECT COUNT(*) FROM global_bans WHERE active=1").fetchone()[0] or 0)
            new_tickets = int(db.execute("SELECT COUNT(*) FROM external_support_tickets WHERE status='new'").fetchone()[0] or 0)
            latest_actions = db.execute("SELECT action,COUNT(*) FROM logs WHERE created_at>=? GROUP BY action ORDER BY COUNT(*) DESC LIMIT 8",
                                        ((now_local - __import__("datetime").timedelta(days=7)).isoformat(),)).fetchall()
            source_syntax_ok = False
            try:
                with open(__file__, "r", encoding="utf-8") as _source_file:
                    compile(_source_file.read(), __file__, "exec")
                source_syntax_ok = True
            except Exception as source_exc:
                log.error("Weekly source syntax/integrity check failed: %s", source_exc)
            metrics = {
                "source_syntax_ok": source_syntax_ok,
                "bot_token_configured": bool(TOKEN),
                "owner_id_configured": bool(OWNER_ID),
                "groups_total": total_groups, "external_groups": external_groups, "disabled_external_groups": disabled_groups,
                "external_ai_enabled_groups": [{"chat_id":r[0],"title":r[1]} for r in ai_enabled_groups],
                "groups_with_links_open": [{"chat_id":r[0],"title":r[1]} for r in links_open],
                "members_with_violations": violation_count, "active_global_bans": active_bans,
                "unanswered_support_tickets": new_tickets, "top_actions_last_7_days": latest_actions
            }
            report = (
                "🛡️ المراجعة الأمنية الأسبوعية للبوت\n\n"
                f"فحص صياغة الكود: {'ناجح' if source_syntax_ok else 'فشل — يتطلب مراجعة عاجلة'}\n"
                f"إعداد رمز البوت: {'موجود' if TOKEN else 'غير موجود'} | إعداد المالك: {'موجود' if OWNER_ID else 'غير موجود'}\n"
                f"القروبات المسجلة: {total_groups}\nالقروبات الخارجية: {external_groups}\n"
                f"القروبات الخارجية المتوقفة: {disabled_groups}\nالقروبات الخارجية المفعّل لها الذكاء يدويًا: {len(ai_enabled_groups)}\n"
                f"القروبات المفتوحة فيها الروابط: {len(links_open)}\nالأعضاء ذوو المخالفات المسجلة: {violation_count}\n"
                f"حالات الحظر الجماعي النشطة: {active_bans}\nطلبات الدعم الجديدة: {new_tickets}\n"
                "مؤشرات آخر أسبوع: " + (", ".join(f"{a} ({n})" for a,n in latest_actions) or "لا توجد إجراءات مسجلة") + "\n\n"
                "التوصيات: راجع القروبات التي الروابط فيها مفتوحة، وطلبات الدعم غير المجابة، وأي صلاحيات إضافية مفعّلة. "
                "لا يجري تعديل الكود أو نشر تحديث برمجي تلقائيًا دون اختبارات وموافقة؛ هذا يمنع تحديثًا غير موثوق من تعطيل الحماية."
            )
            if OPENAI_API_KEY:
                try:
                    ai_summary = await openai_assistant_response(
                        "راجع مؤشرات الأمان المجمعة التالية فقط، ولا تفترض وجود اختراق دون دليل. "
                        "اكتب ملخصًا عربيًا من نقاط: المخاطر المحتملة، الأولويات، والفحوصات المقترحة. لا تقترح نشر كود غير مختبر.\n"
                        + json.dumps(metrics, ensure_ascii=False),
                        "مراجعة أمنية أسبوعية"
                    )
                    if ai_summary:
                        report += "\n\n🤖 ملخص المساعد الذكي:\n" + ai_summary[:2200]
                except Exception as exc:
                    log.warning("AI weekly security review failed: %s", exc)
            db.execute("INSERT OR REPLACE INTO security_review_runs(week_start,created_at,report) VALUES(?,?,?)", (week_key, now(), report[:9000]))
            db.commit()
            recipients = {int(OWNER_ID)} if OWNER_ID else set()
            recipients.update(int(r[0]) for r in db.execute(
                "SELECT user_id FROM delegated_owners WHERE user_id IN (SELECT user_id FROM staff_capabilities WHERE capability='security_review' OR capability='executive')"
            ).fetchall())
            for recipient in sorted(recipients):
                try:
                    await app.bot.send_message(chat_id=recipient, text=report[:3900])
                except Exception as exc:
                    log.warning("Weekly security review delivery failed user=%s: %s", recipient, exc)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Weekly security review worker failed")
            await asyncio.sleep(300)


async def bot_connectivity_monitor_worker(app):
    """Detect Telegram API connectivity loss/recovery and alert the owner once per incident."""
    consecutive_failures = 0
    incident_alerted = False
    while True:
        try:
            await app.bot.get_me()
            if consecutive_failures:
                log.info("Telegram API connectivity recovered after %s failed checks", consecutive_failures)
                if incident_alerted and OWNER_ID:
                    try:
                        await app.bot.send_message(
                            chat_id=OWNER_ID,
                            text="✅ عاد اتصال البوت بواجهة Telegram API بعد انقطاع مؤقت. راجع سجل Render لمعرفة مدة الانقطاع وسببه إن توفر."
                        )
                    except Exception as send_exc:
                        log.warning("Could not deliver recovery alert to owner: %s", send_exc)
            consecutive_failures = 0
            incident_alerted = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            log.warning("Telegram API health check failed (%s/2): %s", consecutive_failures, str(exc)[:400])
            if consecutive_failures >= 2 and not incident_alerted:
                incident_alerted = True
                if OWNER_ID:
                    try:
                        await app.bot.send_message(
                            chat_id=OWNER_ID,
                            text=("⚠️ تنبيه مراقبة البوت\n\nتعذر الاتصال بواجهة Telegram API في فحصين متتاليين. "
                                  "قد يكون هناك انقطاع اتصال أو مشكلة في Telegram. ستصلك رسالة عند عودة الاتصال "
                                  "إذا ظل البوت قيد التشغيل. افحص Render Logs للتأكد من السبب.")
                        )
                    except Exception as send_exc:
                        # Delivery may fail during a genuine outage; keep the event in Render logs.
                        log.error("Connectivity alert could not be delivered (owner=%s): %s", OWNER_ID, send_exc)
        await asyncio.sleep(60)


async def post_init(app):
    """Start long-running assistant workers only after PTB has initialized its event loop."""
    # Keep all existing background jobs and ensure scheduled reports actually start.
    app.create_task(assistant_reminder_worker(app), name="assistant-reminder-worker")
    app.create_task(bot_connectivity_monitor_worker(app), name="bot-connectivity-monitor-worker")
    app.create_task(automatic_unmute_worker(app), name="automatic-unmute-worker")
    app.create_task(owner_activity_audit_worker(app), name="owner-activity-audit-worker")
    app.create_task(weekly_security_review_worker(app), name="weekly-security-review-worker")
    app.create_task(motivation_daily_worker(app), name="motivation-daily-worker")
    app.create_task(bot_promo_worker(app), name="bot-promo-worker")
    app.create_task(evening_adhkar_worker(app), name="evening-adhkar-worker")
    app.create_task(weekly_stats_worker(app), name="weekly-stats-worker")
    app.create_task(daily_member_activity_worker(app), name="daily-member-activity-worker")
    app.create_task(group_analysis_report_worker(app), name="group-analysis-report-worker")



async def owner_activity_audit_worker(app):
    """Notify the primary owner about logged actions by delegated owners/managers/staff."""
    # Skip historic rows at startup; only new actions generate notifications.
    try:
        latest = db.execute("SELECT COALESCE(MAX(id),0) FROM logs").fetchone()[0]
        db.execute("UPDATE owner_activity_cursor SET last_log_id=? WHERE id=1", (latest,))
        db.commit()
    except Exception:
        log.exception("تعذر تهيئة مؤشر تنبيهات نشاط الإدارة")
    while True:
        try:
            if not OWNER_ID:
                await asyncio.sleep(10)
                continue
            cursor = db.execute("SELECT last_log_id FROM owner_activity_cursor WHERE id=1").fetchone()
            last_id = int(cursor[0]) if cursor else 0
            rows = db.execute(
                "SELECT id,chat_id,user_id,action,details,created_at FROM logs WHERE id>? ORDER BY id LIMIT 100",
                (last_id,)
            ).fetchall()
            for log_id, chat_id, actor_id, action, details, created_at in rows:
                try:
                    actor_id = int(actor_id or 0)
                    is_staff_actor = bool(
                        actor_id and ((is_delegated_owner(actor_id) and not is_owner(actor_id))
                        or db.execute("SELECT 1 FROM managers WHERE user_id=?", (actor_id,)).fetchone()
                        or db.execute("SELECT 1 FROM protection_managers WHERE user_id=?", (actor_id,)).fetchone()
                        or db.execute("SELECT 1 FROM group_roles WHERE user_id=? LIMIT 1", (actor_id,)).fetchone())
                    )
                    if is_staff_actor:
                        role = role_name(actor_id)
                        name = user_display_name(actor_id)
                        group_title = "خاص/النظام" if not chat_id else str(chat_id)
                        if chat_id:
                            row = db.execute("SELECT title FROM watched_groups WHERE chat_id=?", (chat_id,)).fetchone()
                            if row and row[0]: group_title = row[0]
                        message = (
                            "🔔 تنبيه نشاط إداري\n\n"
                            f"👤 المنفّذ: {name}\n🪪 المعرّف: {actor_id}\n🏷️ الصفة: {role}\n"
                            f"⚙️ نوع الإجراء: {action or 'غير محدد'}\n📍 القروب/النطاق: {group_title}\n"
                            f"📝 التفاصيل: {(details or 'لا توجد تفاصيل')[:900]}\n🕒 الوقت: {created_at or 'غير محدد'}"
                        )
                        try:
                            await app.bot.send_message(chat_id=OWNER_ID, text=message)
                        except Exception as exc:
                            log.info("تعذر إرسال تنبيه النشاط الإداري للمالك: %s", exc)
                    db.execute("UPDATE owner_activity_cursor SET last_log_id=? WHERE id=1", (log_id,))
                    db.commit()
                except Exception:
                    log.exception("تعذر معالجة سجل النشاط الإداري %s", log_id)
                    db.execute("UPDATE owner_activity_cursor SET last_log_id=? WHERE id=1", (log_id,))
                    db.commit()
            await asyncio.sleep(5 if rows else 10)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("خطأ في مراقبة النشاط الإداري")
            await asyncio.sleep(10)

def main():
    # Centralized startup validation; retain the existing Arabic BOT_TOKEN error.
    try:
        settings.validate_for_startup()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    command_handlers = {
        "joinalerts_on": owner_group_feature_command, "joinalerts_off": owner_group_feature_command, "leavealerts_on": owner_group_feature_command, "leavealerts_off": owner_group_feature_command, "lockall": lock_all_messages_command, "unlockall": lock_all_messages_command,
        "start": start, "help": help_cmd, "panel": panel, "motivation": motivation_cmd, "ai": ai_cmd, "aigroups": aigroups_cmd, "calendar": calendar_cmd, "adhkar": adhkar_cmd, "staffdone": staffdone_cmd, "report": report_cmd, 
        "note": assistant_note_cmd, "notes": assistant_notes_cmd, "usage": assistant_usage_cmd, "clear_ai": assistant_clear_ai_cmd,
        "remind": assistant_remind_cmd, "reminders": assistant_reminders_cmd, "delreminder": assistant_delreminder_cmd, "assistantstats": assistant_stats_cmd,
        "task": assistant_task_cmd, "tasks": assistant_tasks_cmd, "donetask": assistant_done_task_cmd, "deltask": assistant_delete_task_cmd,
        "expense": assistant_expense_cmd, "expenses": assistant_expenses_cmd, "summarize": assistant_summarize_cmd, "quiz": assistant_quiz_cmd,
        "studyadd": student_studyadd_cmd, "studyschedule": student_schedule_cmd, "delstudy": student_studydelete_cmd,
        "exam": student_exam_cmd, "exams": student_events_cmd, "delexam": student_event_delete_cmd,
        "addowner": addowner, "delowner": delowner, "owners": owners_cmd,
        "grantpublish": grantpublish, "revokepublish": revokepublish,
        "grantcap": grantcap, "revokecap": revokecap, "aistatus": aistatus_cmd,
        "addchannel": addchannel_cmd, "delchannel": delchannel_cmd, "channels": channels_cmd,
        "publishchannels": publish_channels_cmd, "ask": ask_cmd,
        "addprotect": addprotect, "delprotect": delprotect, "permissions": permissions_cmd,
        "addmanager": addmanager, "delmanager": delmanager, "managers": managers,
        "id": id_cmd, "idgroup": idgroup, "monitor": monitor_cmd, "badges": badges_cmd, "badge_remove": badge_remove_cmd,
        "memberpoints": member_points_cmd, "membertop": member_top_cmd, "givepoints": give_member_points_cmd, "setmembertitle": set_member_title_cmd,
        "dailyactivity": daily_activity_settings_cmd,
        "ban": ban, "unban": unban, "kick": kick, "mute": mute, "unmute": unmute, "warn": warn_cmd,
        "muted": muted_cmd, "unmuteall": unmuteall_cmd, "del": del_cmd, "pin": pin,
        "alerts": alerts, "logs": logs_cmd, "settings": settings_cmd, "locks": locks,
        "archive": archive_cmd, "archive_on": archive_on, "archive_off": archive_off,
        "autoreplies": auto_replies_list, "autoreplies_on": auto_replies_on,
        "autoreplies_off": auto_replies_off, "addreply": add_auto_reply, "delreply": delete_auto_reply
    }
    for name, fn in command_handlers.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CommandHandler("cancel", cancel_owner_autoreply), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, lock_all_groups_text), group=-6)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, process_private_workflows), group=-5)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, assistant_private_text), group=-4)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, assistant_private_document), group=-4)

    for cmd in LOCK_MAP:
        app.add_handler(CommandHandler(cmd, set_lock))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, owner_autoreply_text_handler), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, broadcast_draft_message), group=-1)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_listener), group=-1)
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER), group=0)
    app.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(ChatJoinRequestHandler(chat_join_request_handler), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members), group=1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, ArabicTextCommand), group=2)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_filter), group=3)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_filter), group=4)
    async def error_handler(update, context):
        # Avoid noisy failures for idempotent UI edits and explain polling conflicts clearly.
        error = context.error
        error_text = str(error or "")
        if isinstance(error, BadRequest) and "message is not modified" in error_text.lower():
            log.debug("Ignored Telegram no-op edit: %s", error_text)
            return
        if "terminated by other getUpdates request" in error_text or "Conflict: terminated by other getUpdates request" in error_text:
            log.error("Telegram polling conflict: another instance is polling this bot token. Stop duplicate Render/local instances; only one polling process may run.")
            return
        log.error("Update error: %s", error, exc_info=error)

    app.add_error_handler(error_handler)

    threading.Thread(target=start_health_server, daemon=True).start()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
