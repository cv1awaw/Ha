#!/usr/bin/env python3

import os
import sys
import sqlite3
import logging
import fcntl
from datetime import datetime, timedelta
import re
import asyncio
import tempfile

# -------------------------------------------------------------------------------------
# OPTIONAL / CONDITIONAL IMPORTS FOR PDF & IMAGE TEXT EXTRACTION
# If not installed, we skip that functionality to avoid crashes.
# -------------------------------------------------------------------------------------
pdf_available = True
try:
    import PyPDF2
except ImportError:
    pdf_available = False

pytesseract_available = True
pillow_available = True
try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract_available = False
    pillow_available = False

from telegram import (
    Update,
    ChatPermissions,
    ChatMemberStatus,
    ChatType,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ------------------- Configuration -------------------

DATABASE = 'warnings.db'
ALLOWED_USER_ID = 6177929931  # Change to your actual authorized user ID
LOCK_FILE = '/tmp/telegram_bot.lock'
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- Pending group name dict -------------------

pending_group_names = {}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    Acquire a lock so only one instance can run at a time.
    """
    try:
        lock_file = open(LOCK_FILE, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. This is the only running instance.")
        return lock_file
    except IOError:
        logger.error("Another instance of this bot is already running. Exiting.")
        sys.exit("Another instance of this bot is already running.")

def release_lock(lock_file):
    """
    Release the lock at exit.
    """
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

lock_file = acquire_lock()

import atexit
atexit.register(release_lock, lock_file)

# ------------------- Database Initialization -------------------

def init_permissions_db():
    """
    Initialize the 'permissions' and 'removed_users' tables.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # Create permissions table
        c.execute('''
            CREATE TABLE IF NOT EXISTS permissions (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL
            )
        ''')

        # Create removed_users table
        c.execute('''
            CREATE TABLE IF NOT EXISTS removed_users (
                group_id INTEGER,
                user_id INTEGER,
                removal_reason TEXT,
                removal_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id),
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("Permissions and Removed Users tables initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize permissions DB: {e}")
        raise

def init_db():
    """
    Initialize the main DB tables if they don't exist.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("PRAGMA foreign_keys = 1")
        c = conn.cursor()

        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS bypass_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS deletion_settings (
                group_id INTEGER PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY(group_id) REFERENCES groups(group_id)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("Main DB tables initialized.")
        
        init_permissions_db()
    except Exception as e:
        logger.error(f"Failed to initialize the database: {e}")
        raise

# ------------------- Database Helper Functions -------------------

def add_group(group_id):
    """
    Insert a group row if not present (initial group_name=None).
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("""
            INSERT OR IGNORE INTO groups (group_id, group_name)
            VALUES (?, ?)
        """, (group_id, None))
        conn.commit()
        conn.close()
        logger.info(f"Added group {group_id} to DB (no name yet).")
    except Exception as e:
        logger.error(f"Error adding group {group_id}: {e}")
        raise

def set_group_name(group_id, name):
    """
    Update the group_name for a group row.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name=? WHERE group_id=?', (name, group_id))
        conn.commit()
        conn.close()
        logger.info(f"Group {group_id} name set to '{name}'.")
    except Exception as e:
        logger.error(f"Error setting name for group {group_id}: {e}")
        raise

def group_exists(group_id):
    """
    Return True if the group_id is in 'groups'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM groups WHERE group_id=?', (group_id,))
        row = c.fetchone()
        conn.close()
        return bool(row)
    except Exception as e:
        logger.error(f"Error checking group {group_id}: {e}")
        return False

def is_bypass_user(user_id):
    """
    Return True if user_id is in 'bypass_users'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id=?', (user_id,))
        found = c.fetchone()
        conn.close()
        return bool(found)
    except Exception as e:
        logger.error(f"Error checking bypass for {user_id}: {e}")
        return False

def add_bypass_user(user_id):
    """
    Insert user_id into bypass_users if not present.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO bypass_users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"User {user_id} added to bypass list.")
    except Exception as e:
        logger.error(f"Error adding user {user_id} to bypass list: {e}")
        raise

def remove_bypass_user(user_id):
    """
    Remove user_id from 'bypass_users'. Return True if found/removed.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM bypass_users WHERE user_id=?', (user_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"Removed user {user_id} from bypass list.")
            return True
        else:
            logger.warning(f"User {user_id} not in bypass list.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from bypass list: {e}")
        return False

def enable_deletion(group_id):
    """
    Set 'enabled=1' in deletion_settings for group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 1)
            ON CONFLICT(group_id) DO UPDATE SET enabled=1
        """, (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"Enabled Arabic deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error enabling deletion for {group_id}: {e}")
        raise

def disable_deletion(group_id):
    """
    Set 'enabled=0' in deletion_settings for group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 0)
            ON CONFLICT(group_id) DO UPDATE SET enabled=0
        """, (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"Disabled Arabic deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error disabling deletion for {group_id}: {e}")
        raise

def is_deletion_enabled(group_id):
    """
    Return True if 'enabled=1' in deletion_settings for group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT enabled FROM deletion_settings WHERE group_id=?', (group_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"Error checking deletion for {group_id}: {e}")
        return False

def remove_user_from_removed_users(group_id, user_id):
    """
    Delete the (group_id, user_id) row from removed_users. Return True if found/removed.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM removed_users WHERE group_id=? AND user_id=?', (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"Removed user {user_id} from removed_users for group {group_id}.")
            return True
        else:
            logger.warning(f"User {user_id} not in removed_users for group {group_id}.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from removed_users: {e}")
        return False

def revoke_user_permissions(user_id):
    """
    Revoke all permissions by setting role='removed' in 'permissions'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE permissions SET role=? WHERE user_id=?', ('removed', user_id))
        conn.commit()
        conn.close()
        logger.info(f"Revoked permissions for user {user_id}.")
    except Exception as e:
        logger.error(f"Error revoking perms for {user_id}: {e}")
        raise

def list_removed_users(group_id=None):
    """
    Return rows from removed_users. If group_id is None, return all; else filter by group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        if group_id is not None:
            c.execute("""
                SELECT user_id, removal_reason, removal_time
                FROM removed_users
                WHERE group_id=?
            """, (group_id,))
            rows = c.fetchall()
        else:
            c.execute("""
                SELECT group_id, user_id, removal_reason, removal_time
                FROM removed_users
            """)
            rows = c.fetchall()
        conn.close()
        logger.info("Fetched removed_users entries.")
        return rows
    except Exception as e:
        logger.error(f"Error fetching removed_users: {e}")
        return []

# Short-term deletion after forcibly removing a user
delete_all_messages_after_removal = {}

# ------------------- Command Handler Functions -------------------

async def handle_set_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If ALLOWED_USER_ID has a pending group name, set it from their text.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if user.id not in pending_group_names:
        return

    text = (update.message.text or "").strip()
    if not text:
        return  # ignore empty

    group_id = pending_group_names.pop(user.id)
    try:
        set_group_name(group_id, text)
        reply = escape_markdown(
            f"✅ Group `{group_id}` name set to: *{text}*", version=2
        )
        await context.bot.send_message(chat_id=user.id, text=reply, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error setting group name for {group_id}: {e}")
        msg = escape_markdown("⚠️ Could not set group name. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown("✅ Bot is ready.", version=2),
        parse_mode='MarkdownV2'
    )

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_add <group_id>
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/group_add <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    if group_exists(g_id):
        msg = "⚠️ That group is already registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    add_group(g_id)
    pending_group_names[user.id] = g_id
    confirmation = f"✅ Group `{g_id}` added.\nPlease send the group name in a message."
    await context.bot.send_message(chat_id=user.id,
                                   text=escape_markdown(confirmation, version=2),
                                   parse_mode='MarkdownV2')

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_group <group_id>
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/rmove_group <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id=?', (g_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            cf = f"✅ Group `{g_id}` removed."
            await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
        else:
            wr = f"⚠️ Group `{g_id}` not found."
            await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error removing group {g_id}: {e}")
        msg = "⚠️ Failed to remove group. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')

# ---------- Removing /unremove_user entirely, as requested ----------

async def love_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /love <group_id> <user_id> – remove user from 'Removed Users' (like unremove).
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = "⚠️ Usage: `/love <group_id> <user_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except:
        msg = "⚠️ Both group_id and user_id must be integers."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        wr = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    removed = remove_user_from_removed_users(g_id, u_id)
    if not removed:
        wr = f"⚠️ User `{u_id}` is not in 'Removed Users' for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Error revoking perms for {u_id}: {e}")

    cf = f"✅ Loved user `{u_id}` (removed from 'Removed Users') in group `{g_id}`."
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_user <group_id> <user_id> – forcibly remove user from group & DB
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = "⚠️ Usage: `/rmove_user <group_id> <user_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except:
        msg = "⚠️ Both group_id and user_id must be integers."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    remove_bypass_user(u_id)
    remove_user_from_removed_users(g_id, u_id)
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Revoke perms failed for {u_id}: {e}")

    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=u_id)
    except Exception as e:
        err = f"⚠️ Could not ban `{u_id}` from group `{g_id}` (check bot perms)."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')
        logger.error(f"Ban error for user {u_id} in group {g_id}: {e}")
        return

    delete_all_messages_after_removal[g_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    asyncio.create_task(remove_deletion_flag_after_timeout(g_id))

    cf = f"✅ Removed `{u_id}` from group `{g_id}`.\nMessages for next {MESSAGE_DELETE_TIMEFRAME}s will be deleted."
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')

# ------------------- NEW Commands: /mute /limit /slow -------------------

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mute <group_id> <user_id> <minutes>
    Mutes the user (cannot send messages) for the specified number of minutes.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 3:
        msg = "⚠️ Usage: `/mute <group_id> <user_id> <minutes>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return
    
    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
        minutes = int(context.args[2])
    except:
        msg = "⚠️ group_id, user_id, and minutes must be integers."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return
    
    if not group_exists(g_id):
        wr = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    from telegram import ChatPermissions
    until_date = datetime.utcnow() + timedelta(minutes=minutes)
    perms = ChatPermissions(can_send_messages=False)

    try:
        await context.bot.restrict_chat_member(chat_id=g_id,
                                               user_id=u_id,
                                               permissions=perms,
                                               until_date=until_date)
        cf = f"✅ Muted user `{u_id}` in group `{g_id}` for {minutes} minute(s)."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error muting {u_id} in {g_id}: {e}")
        err = "⚠️ Could not mute. Check bot’s admin rights & logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /limit <group_id> <user_id> <permission_type> <on/off>
    
    Toggles a specific permission for that user. For example:
      /limit -10012345 999999 photos off
    means disallow user from sending photos in that group.

    **Possible <permission_type> values** might be:
    text, photos, videos, stickers, gifs, music, voice, video_messages,
    inlinebots, embed_links, polls, games, etc.

    This is a simplified approach that re-applies all permissions each time. 
    For real usage, you'd track partial perms in the DB.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 4:
        msg = ("⚠️ Usage: `/limit <group_id> <user_id> <permission_type> <on/off>`\n\n"
               "Examples of <permission_type>: text, photos, videos, stickers, polls, etc.")
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
        p_type = context.args[2].lower().strip()
        toggle = context.args[3].lower().strip()
    except:
        msg = "⚠️ group_id and user_id must be integers, then permission_type, then on/off."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        wr = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    # We'll do a simple approach: start with all True, then turn some off if toggle=off.
    can_send_messages = True
    can_send_media_messages = True
    can_send_polls = True
    can_send_other_messages = True
    can_add_web_page_previews = True

    # Based on p_type, we flip the relevant permission
    def off():
        return toggle == "off"

    if p_type in ["photos", "videos", "files", "music", "gifs", "voice", "video_messages", "inlinebots", "embed_links"]:
        if off():
            can_send_media_messages = False

    elif p_type in ["stickers", "games"]:
        if off():
            can_send_other_messages = False

    elif p_type == "polls":
        if off():
            can_send_polls = False

    elif p_type == "text":
        if off():
            can_send_messages = False

    else:
        wr = "⚠️ Unknown permission_type. Try 'stickers', 'photos', 'text', 'polls', etc."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    from telegram import ChatPermissions
    perms = ChatPermissions(
        can_send_messages=can_send_messages,
        can_send_media_messages=can_send_media_messages,
        can_send_polls=can_send_polls,
        can_send_other_messages=can_send_other_messages,
        can_add_web_page_previews=True
    )

    try:
        await context.bot.restrict_chat_member(chat_id=g_id, user_id=u_id, permissions=perms)
        msg = f"✅ Set permission '{p_type}' to '{toggle}' for `{u_id}` in group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error limiting perms for {u_id} in group {g_id}: {e}")
        err = "⚠️ Could not limit permission. Check bot’s admin rights & logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

async def slow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /slow <group_id> <delay_in_seconds>

    Telegram Bot API doesn't officially allow changing slow mode as of now.
    We'll log a warning or show a placeholder. 
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = "⚠️ Usage: `/slow <group_id> <delay_in_seconds>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        delay = int(context.args[1])
    except:
        wr = "⚠️ group_id and delay must be integers."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        msg = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    # Placeholder, since official Bot API doesn't support setting slow mode.
    logger.warning("Set slow mode is not officially supported by Bot API. Placeholder only.")
    ef = f"⚠️ There's no official method to set slow mode via Bot API. (Placeholder only.)"
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(ef, version=2), parse_mode='MarkdownV2')

# ------------------- Deletion / Filtering Handlers -------------------

def has_arabic(text):
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    chat_id = msg.chat.id

    if not is_deletion_enabled(chat_id):
        return
    if is_bypass_user(user.id):
        return

    text_or_caption = (msg.text or msg.caption or "")
    if text_or_caption and has_arabic(text_or_caption):
        try:
            await msg.delete()
            logger.info(f"Deleted Arabic text from user {user.id} in group {chat_id}.")
        except Exception as e:
            logger.error(f"Error deleting Arabic message: {e}")
        return

    # PDF check
    if msg.document and msg.document.file_name and msg.document.file_name.lower().endswith('.pdf'):
        if pdf_available:
            file_id = msg.document.file_id
            file_ref = await context.bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                await file_ref.download_to_drive(tmp_pdf.name)
                tmp_pdf.flush()
                try:
                    with open(tmp_pdf.name, 'rb') as pdf_file:
                        try:
                            import PyPDF2
                            reader = PyPDF2.PdfReader(pdf_file)
                            text_all = ""
                            for page in reader.pages:
                                text_all += page.extract_text() or ""
                            if has_arabic(text_all):
                                await msg.delete()
                                logger.info(f"Deleted PDF with Arabic from {user.id} in {chat_id}.")
                        except Exception as e:
                            logger.error(f"PyPDF2 read error: {e}")
                except Exception as e:
                    logger.error(f"PDF parse error: {e}")
                finally:
                    try:
                        os.remove(tmp_pdf.name)
                    except:
                        pass

    # Image check
    if msg.photo:
        if pytesseract_available and pillow_available:
            photo_obj = msg.photo[-1]
            file_id = photo_obj.file_id
            file_ref = await context.bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_img:
                await file_ref.download_to_drive(tmp_img.name)
                tmp_img.flush()
                try:
                    from PIL import Image
                    extracted = pytesseract.image_to_string(Image.open(tmp_img.name)) or ""
                    if has_arabic(extracted):
                        await msg.delete()
                        logger.info(f"Deleted image with Arabic from user {user.id} in {chat_id}.")
                except Exception as e:
                    logger.error(f"OCR error: {e}")
                finally:
                    try:
                        os.remove(tmp_img.name)
                    except:
                        pass

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat.id
    if chat_id in delete_all_messages_after_removal:
        expiry = delete_all_messages_after_removal[chat_id]
        if datetime.utcnow() > expiry:
            delete_all_messages_after_removal.pop(chat_id, None)
            logger.info(f"Short-term deletion window expired for group {chat_id}.")
            return
        try:
            await msg.delete()
            logger.info(f"Deleted message in group {chat_id} (short-term).")
        except Exception as e:
            logger.error(f"Failed to delete flagged message in group {chat_id}: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

async def remove_deletion_flag_after_timeout(group_id):
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    if group_id in delete_all_messages_after_removal:
        delete_all_messages_after_removal.pop(group_id, None)
        logger.info(f"Deletion flag removed for group {group_id}")

# ------------------- Commands to toggle Arabic deletion -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_sad <group_id> – enable Arabic deletion
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/be_sad <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        enable_deletion(g_id)
        cf = f"✅ Arabic deletion enabled for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error enabling deletion for {g_id}: {e}")
        err = "⚠️ Could not enable. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_happy <group_id> – disable Arabic deletion
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/be_happy <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        disable_deletion(g_id)
        cf = f"✅ Arabic deletion disabled for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error disabling deletion for {g_id}: {e}")
        err = "⚠️ Could not disable. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

# ------------------- /check Command -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check <group_id> – verify 'Removed Users' vs. actual group membership
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/check <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        ef = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(ef, version=2), parse_mode='MarkdownV2')
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id=?', (g_id,))
        removed_list = [row[0] for row in c.fetchall()]
        conn.close()
    except Exception as e:
        logger.error(f"Error listing removed users for {g_id}: {e}")
        er = "⚠️ DB error. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(er, version=2), parse_mode='MarkdownV2')
        return

    if not removed_list:
        msg = f"⚠️ No removed users found for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    still_in = []
    not_in = []
    for uid in removed_list:
        try:
            member = await context.bot.get_chat_member(chat_id=g_id, user_id=uid)
            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                still_in.append(uid)
            else:
                not_in.append(uid)
        except Exception as e:
            logger.error(f"Error get_chat_member for user {uid} in group {g_id}: {e}")
            not_in.append(uid)

    resp = f"*Check Results for Group `{g_id}`:*\n\n"
    if still_in:
        resp += "*These removed users are still in the group:*\n"
        for x in still_in:
            resp += f"• `{x}`\n"
    else:
        resp += "No removed users are still in the group.\n"
    resp += "\n"
    if not_in:
        resp += "*Users not in the group (OK):*\n"
        for x in not_in:
            resp += f"• `{x}`\n"

    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown(resp, version=2),
        parse_mode='MarkdownV2'
    )

    # Optionally auto-ban them
    for x in still_in:
        try:
            await context.bot.ban_chat_member(chat_id=g_id, user_id=x)
            logger.info(f"Auto-banned user {x} in group {g_id} after /check.")
        except Exception as e:
            logger.error(f"Failed to ban {x} in group {g_id}: {e}")

# ------------------- /link Command -------------------

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /link <group_id> – create a one-time-use invite link for group_id
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = "⚠️ Usage: `/link <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except:
        wr = "⚠️ group_id must be an integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        ef = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(ef, version=2), parse_mode='MarkdownV2')
        return

    try:
        invite_link_obj = await context.bot.create_chat_invite_link(
            chat_id=g_id,
            member_limit=1,
            name="One-Time Link"
        )
        cf = f"✅ One-time invite link for group `{g_id}`:\n\n{invite_link_obj.invite_link}"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
        logger.info(f"Created one-time link for group {g_id}: {invite_link_obj.invite_link}")
    except Exception as e:
        logger.error(f"Error creating invite link for {g_id}: {e}")
        err = "⚠️ Could not create invite link. Check bot’s admin rights & logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

# ------------------- main() -------------------

def main():
    """
    Initialize DB, build app, run the bot with new commands (/mute, /limit, /slow)
    and no /unremove_user.
    """
    try:
        init_db()
    except Exception as e:
        logger.critical(f"DB init failure: {e}")
        sys.exit("Cannot start due to DB init failure.")

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN not set.")
        sys.exit("BOT_TOKEN not set.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[len('bot='):].strip()

    try:
        app = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"Failed building Telegram app: {e}")
        sys.exit("Bot build error.")

    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("group_add", group_add_cmd))
    app.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    app.add_handler(CommandHandler("bypass", bypass_cmd))
    app.add_handler(CommandHandler("unbypass", unbypass_cmd))
    app.add_handler(CommandHandler("group_id", group_id_cmd))
    app.add_handler(CommandHandler("show", show_groups_cmd))
    app.add_handler(CommandHandler("list", show_groups_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("be_sad", be_sad_cmd))
    app.add_handler(CommandHandler("be_happy", be_happy_cmd))
    app.add_handler(CommandHandler("rmove_user", rmove_user_cmd))
    app.add_handler(CommandHandler("add_removed_user", add_removed_user_cmd))
    app.add_handler(CommandHandler("list_removed_users", list_removed_users_cmd))
    # *** Removed unremove_user command entirely! ***
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("link", link_cmd))
    # NEW commands
    app.add_handler(CommandHandler("love", love_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("slow", slow_cmd))

    # Message Handlers
    # 1) Check Arabic
    app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO,
        delete_arabic_messages
    ))
    # 2) Short-term deletion
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))
    # 3) If user is pending group name
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_set_group_name
    ))

    app.add_error_handler(error_handler)

    logger.info("Bot starting with new commands (no unremove_user).")
    app.run_polling()


if __name__ == "__main__":
    main()
