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

# ------------------------------------------------------------------------
# OPTIONAL / CONDITIONAL IMPORTS FOR PDF & IMAGE TEXT EXTRACTION
# If not installed, we skip that functionality to avoid crashes.
# ------------------------------------------------------------------------
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

from telegram import Update, ChatMember
from telegram.constants import ChatMemberStatus, ChatType
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
ALLOWED_USER_ID = 6177929931  # Change to your personal Telegram user ID
LOCK_FILE = '/tmp/telegram_bot.lock'
MESSAGE_DELETE_TIMEFRAME = 15  # for short-term deletion after forcibly removing a user

# ------------------- Logging Setup -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- Pending group name dict -------------------

# If you run `/group_add <group_id>`, we store user_id -> group_id here
# so we know you owe us a group name in your next non-command text.
pending_group_names = {}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    Acquire an exclusive lock so only one instance of this bot is running.
    """
    try:
        lock_file = open(LOCK_FILE, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. This is the only running instance.")
        return lock_file
    except IOError:
        logger.error("Another instance of this bot is already running. Exiting now.")
        sys.exit("Another instance of this bot is already running.")

def release_lock(lock_file):
    """
    Release the lock at exit.
    """
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot is stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

lock_file = acquire_lock()

import atexit
atexit.register(release_lock, lock_file)

# ------------------- Database Initialization -------------------

def init_permissions_db():
    """
    Initialize 'permissions' and 'removed_users' tables.
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
        logger.error(f"Failed to initialize permissions database: {e}")
        raise

def init_db():
    """
    Initialize the SQLite DB and create any missing tables.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("PRAGMA foreign_keys = 1")  # enable foreign keys
        c = conn.cursor()

        # Create groups table
        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT
            )
        ''')

        # Create bypass_users table
        c.execute('''
            CREATE TABLE IF NOT EXISTS bypass_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')

        # Create deletion_settings table
        c.execute('''
            CREATE TABLE IF NOT EXISTS deletion_settings (
                group_id INTEGER PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY(group_id) REFERENCES groups(group_id)
            )
        ''')

        # Create users table
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
        logger.info("Database initialized successfully.")
        
        init_permissions_db()
    except Exception as e:
        logger.error(f"Failed to initialize DB: {e}")
        raise

# ------------------- Database Helpers -------------------

def add_group(group_id):
    """
    Insert a group row if it doesn't exist (initial name=None).
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO groups (group_id, group_name)
            VALUES (?, ?)
        ''', (group_id, None))
        conn.commit()
        conn.close()
        logger.info(f"Added group {group_id} to DB (no name yet).")
    except Exception as e:
        logger.error(f"Error adding group {group_id}: {e}")
        raise

def set_group_name(group_id, group_name):
    """
    Update the name of a group in the DB.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, group_id))
        conn.commit()
        conn.close()
        logger.info(f"Set group {group_id} name to '{group_name}' in DB.")
    except Exception as e:
        logger.error(f"Error setting group name for {group_id}: {e}")
        raise

def group_exists(group_id):
    """
    Return True if group_id is in the 'groups' table.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM groups WHERE group_id = ?', (group_id,))
        exists = c.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Error checking existence of group {group_id}: {e}")
        return False

def is_bypass_user(user_id):
    """
    Check if user_id is in 'bypass_users'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (user_id,))
        found = c.fetchone() is not None
        conn.close()
        return found
    except Exception as e:
        logger.error(f"Error checking bypass user {user_id}: {e}")
        return False

def add_bypass_user(user_id):
    """
    Insert user_id into 'bypass_users' if not present.
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
    Remove user_id from 'bypass_users'. Return True if found/removed, else False.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM bypass_users WHERE user_id = ?', (user_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"User {user_id} removed from bypass list.")
            return True
        else:
            logger.warning(f"User {user_id} not in bypass list.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from bypass list: {e}")
        return False

def enable_deletion(group_id):
    """
    Enable Arabic message deletion for group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 1)
            ON CONFLICT(group_id) DO UPDATE SET enabled=1
        ''', (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"Enabled Arabic deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error enabling deletion for group {group_id}: {e}")
        raise

def disable_deletion(group_id):
    """
    Disable Arabic message deletion for group_id.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, 0)
            ON CONFLICT(group_id) DO UPDATE SET enabled=0
        ''', (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"Disabled Arabic deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error disabling deletion for group {group_id}: {e}")
        raise

def is_deletion_enabled(group_id):
    """
    Return True if 'enabled' is set for the given group_id in 'deletion_settings'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (group_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"Error checking deletion status for group {group_id}: {e}")
        return False

# -------------- removed_users table queries --------------

def remove_user_from_removed_users(group_id, user_id):
    """
    Remove a user from the 'removed_users' table. Return True if removed, False if missing.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()

        if changes > 0:
            logger.info(f"Removed user {user_id} from 'removed_users' for group {group_id}.")
            return True
        else:
            logger.warning(f"User {user_id} not found in 'removed_users' for group {group_id}.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from removed_users: {e}")
        return False

def revoke_user_permissions(user_id):
    """
    Revoke all permissions for a user by setting role='removed'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE permissions SET role = ? WHERE user_id = ?', ('removed', user_id))
        conn.commit()
        conn.close()
        logger.info(f"Revoked permissions for user {user_id} (role='removed').")
    except Exception as e:
        logger.error(f"Error revoking permissions for {user_id}: {e}")
        raise

def list_removed_users(group_id=None):
    """
    Return all removed_users data. If group_id is given, filter by that group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        if group_id is not None:
            c.execute('''
                SELECT user_id, removal_reason, removal_time
                FROM removed_users
                WHERE group_id = ?
            ''', (group_id,))
            rows = c.fetchall()
        else:
            c.execute('''
                SELECT group_id, user_id, removal_reason, removal_time
                FROM removed_users
            ''')
            rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Error listing removed users: {e}")
        return []

def add_removed_user(group_id, user_id, reason="Manually added"):
    """
    Insert user into removed_users for a given group. 
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        # check if already present
        c.execute('SELECT 1 FROM removed_users WHERE group_id=? AND user_id=?', (group_id, user_id))
        if c.fetchone():
            conn.close()
            return False  # already in removed_users
        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (group_id, user_id, reason))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error adding user {user_id} to removed_users: {e}")
        return False

# ------------------- Short-term Deletion Flag -------------------

delete_all_messages_after_removal = {}

# ------------------- Command Handlers -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start – readiness check
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown("✅ Bot is running and ready.", version=2),
        parse_mode='MarkdownV2'
    )

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_add <group_id> – register a group, then wait for user to send name.
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
        msg = "⚠️ group_id must be an integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    if group_exists(g_id):
        msg = "⚠️ That group is already registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    add_group(g_id)
    pending_group_names[user.id] = g_id
    msg = f"✅ Group `{g_id}` added.\nNow send me the group name in **any** message (not a command)."
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')

async def handle_group_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Catch the first non-command text from ALLOWED_USER_ID after /group_add.
    That text is the group's name. Then confirm & remove from pending.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if user.id not in pending_group_names:
        return  # not expecting a group name from this user

    message_text = (update.message.text or "").strip()
    if not message_text:
        logger.debug("User typed an empty line while pending group name. Ignoring.")
        return

    # we have a pending group_id
    group_id = pending_group_names.pop(user.id)
    # set name
    set_group_name(group_id, message_text)

    cf = f"✅ Group `{group_id}` name set to: *{message_text}*"
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    logger.info(f"Group {group_id} name set to '{message_text}' by user {user.id}")

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_group <group_id> – remove a group from the DB
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
    except ValueError:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id = ?', (g_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()

        if changes > 0:
            cf = f"✅ Group `{g_id}` removed from DB."
            await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
        else:
            wr = f"⚠️ Group `{g_id}` not found."
            await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error removing group {g_id}: {e}")
        msg = "⚠️ Could not remove group. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /show or /list – show all groups & whether Arabic deletion is enabled.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT group_id, group_name FROM groups')
        groups_data = c.fetchall()
        conn.close()

        if not groups_data:
            msg = "⚠️ No groups have been added yet."
            await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
            return

        msg = "*Groups Information:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "(no name set)"
            msg += f"*Group:* {escape_markdown(g_name_display, version=2)}\n*Group ID:* `{g_id}`\n"

            # Check if deletion is enabled
            status = "Enabled" if is_deletion_enabled(g_id) else "Disabled"
            msg += f"*Deletion:* `{status}`\n\n"

        # chunk if large
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await context.bot.send_message(chat_id=user.id, text=msg[i:i+4000], parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /show: {e}")
        err = "⚠️ Failed to get group list. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /link <group_id> – create a one-time-use invite link for the group
    (Must be an admin in that group to succeed).
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
    except ValueError:
        wr = "⚠️ group_id must be an integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        msg = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    # create link
    try:
        link_obj = await context.bot.create_chat_invite_link(
            chat_id=g_id,
            member_limit=1,  # becomes invalid after 1 use
            name="One-Time Link"
        )
        cf = f"✅ One-time invite link for group `{g_id}`:\n{link_obj.invite_link}"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error creating link for {g_id}: {e}")
        msg = "⚠️ Could not create invite link. Check bot's admin rights & logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_user <group_id> <user_id> – forcibly remove user from group & DB
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        war = "⚠️ Usage: `/rmove_user <group_id> <user_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(war, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        msg = "⚠️ Both IDs must be integers."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    # Remove from bypass
    remove_bypass_user(u_id)
    # Remove from removed_users
    remove_user_from_removed_users(g_id, u_id)
    # Revoke perms
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Failed to revoke perms for {u_id}: {e}")

    # Ban from group
    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=u_id)
    except Exception as e:
        err = f"⚠️ Could not ban `{u_id}` from group `{g_id}` (check bot perms)."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(err, version=2), parse_mode='MarkdownV2')
        logger.error(f"Ban error for user {u_id} in group {g_id}: {e}")
        return

    # short-term delete mode
    delete_all_messages_after_removal[g_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    asyncio.create_task(remove_deletion_flag_after_timeout(g_id))

    cf = f"✅ Removed `{u_id}` from group `{g_id}`.\nMessages for next {MESSAGE_DELETE_TIMEFRAME}s will be deleted."
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')

# ... you can add your other commands: /bypass, /unbypass, /add_removed_user, /list_removed_users, /unremove_user, etc.
# For brevity, not repeating them here—just add them like above. They will still work.

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
    except ValueError:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return
    try:
        enable_deletion(g_id)
        cf = f"✅ Arabic deletion enabled for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error enabling deletion for {g_id}: {e}")
        er = "⚠️ Could not enable. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(er, version=2), parse_mode='MarkdownV2')

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
    except ValueError:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return
    try:
        disable_deletion(g_id)
        cf = f"✅ Arabic deletion disabled for group `{g_id}`."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(cf, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error disabling deletion for {g_id}: {e}")
        er = "⚠️ Could not disable. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(er, version=2), parse_mode='MarkdownV2')

# You could also add /check <group_id>, /info, /help, etc. The logic is the same as above.

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help – show available commands
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    text = """*Commands*:
• `/start` – Check if the bot is running
• `/group_add <group_id>` – Register a group, then send a non-command message for its name
• `/rmove_group <group_id>` – Remove group from DB
• `/be_sad <group_id>` – Enable Arabic message deletion
• `/be_happy <group_id>` – Disable Arabic message deletion
• `/show` or `/list` – Show all groups & whether deletion is enabled
• `/link <group_id>` – Create one-time invite link
• `/rmove_user <group_id> <user_id>` – Force remove user from group & DB
... plus your other commands...
"""
    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown(text, version=2),
        parse_mode='MarkdownV2'
    )

# ------------------- Deletion / Filtering Handlers -------------------

def has_arabic(text):
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If deletion is enabled, remove any text, PDF, or image containing Arabic.
    """
    msg = update.message
    if not msg:
        return
    user = msg.from_user
    chat_id = msg.chat.id

    if not is_deletion_enabled(chat_id):
        return
    if is_bypass_user(user.id):
        return

    # check text or caption
    text_or_caption = (msg.text or msg.caption or "")
    if text_or_caption and has_arabic(text_or_caption):
        try:
            await msg.delete()
            logger.info(f"Deleted Arabic text from user {user.id} in group {chat_id}")
        except Exception as e:
            logger.error(f"Error deleting Arabic text message: {e}")
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
                            reader = PyPDF2.PdfReader(pdf_file)
                            all_text = ""
                            for page in reader.pages:
                                all_text += page.extract_text() or ""
                            if all_text and has_arabic(all_text):
                                await msg.delete()
                                logger.info(f"Deleted PDF with Arabic from user {user.id} in group {chat_id}")
                        except Exception as e:
                            logger.error(f"PyPDF2 read error: {e}")
                except Exception as e:
                    logger.error(f"PDF parse error: {e}")
                finally:
                    try:
                        os.remove(tmp_pdf.name)
                    except:
                        pass

    # Photo check
    if msg.photo:
        if pytesseract_available and pillow_available:
            photo_obj = msg.photo[-1]  # highest resolution
            file_id = photo_obj.file_id
            file_ref = await context.bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_img:
                await file_ref.download_to_drive(tmp_img.name)
                tmp_img.flush()
                try:
                    from PIL import Image
                    extracted = pytesseract.image_to_string(Image.open(tmp_img.name)) or ""
                    if extracted and has_arabic(extracted):
                        await msg.delete()
                        logger.info(f"Deleted image with Arabic from user {user.id} in group {chat_id}")
                except Exception as e:
                    logger.error(f"Image OCR error: {e}")
                finally:
                    try:
                        os.remove(tmp_img.name)
                    except:
                        pass

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If we've forcibly removed a user from a group, we might have a short-term
    "delete all messages" window in that group.
    """
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat.id
    if chat_id not in delete_all_messages_after_removal:
        return

    expiry = delete_all_messages_after_removal[chat_id]
    now = datetime.utcnow()
    if now > expiry:
        delete_all_messages_after_removal.pop(chat_id, None)
        logger.info(f"Short-term deletion window expired for group {chat_id}")
        return

    try:
        await msg.delete()
        logger.info(f"Deleted a message in group {chat_id} under short-term removal window.")
    except Exception as e:
        logger.error(f"Failed to delete message in group {chat_id}: {e}")

# ------------------- Check Command Example (Optional) -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check <group_id> – verify 'Removed Users' vs. actual membership.
    If any are still inside, auto-ban them.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        wr = "⚠️ Usage: `/check <group_id>`"
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        wr = "⚠️ group_id must be integer."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(wr, version=2), parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        msg = f"⚠️ Group `{g_id}` is not registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    try:
        # gather removed users
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id=?', (g_id,))
        removed_list = [row[0] for row in c.fetchall()]
        conn.close()
    except Exception as e:
        logger.error(f"Error listing removed users for {g_id}: {e}")
        ef = "⚠️ DB error. Check logs."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(ef, version=2), parse_mode='MarkdownV2')
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
            logger.error(f"get_chat_member error for user {uid} group {g_id}: {e}")
            not_in.append(uid)

    resp = f"*Check Results for Group `{g_id}`:*\n\n"
    if still_in:
        resp += "*Still in group (despite removal):*\n"
        for x in still_in:
            resp += f"• `{x}`\n"
    else:
        resp += "All removed users are out of the group.\n"
    resp += "\n"
    if not_in:
        resp += "*Confirmed not in group (OK):*\n"
        for x in not_in:
            resp += f"• `{x}`\n"

    await context.bot.send_message(chat_id=user.id, text=escape_markdown(resp, version=2), parse_mode='MarkdownV2')

    # Optionally auto-ban still_in
    for x in still_in:
        try:
            await context.bot.ban_chat_member(chat_id=g_id, user_id=x)
            logger.info(f"Auto-banned user {x} from group {g_id} via /check.")
        except Exception as e:
            logger.error(f"Failed to ban user {x} from group {g_id}: {e}")

# ------------------- remove_deletion_flag_after_timeout -------------------

async def remove_deletion_flag_after_timeout(group_id):
    """
    After MESSAGE_DELETE_TIMEFRAME seconds, remove the short-term group message deletion flag.
    """
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    if group_id in delete_all_messages_after_removal:
        delete_all_messages_after_removal.pop(group_id, None)
        logger.info(f"Short-term deletion flag expired for group {group_id}.")

# ------------------- Global Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

# ------------------- main() -------------------

def main():
    """
    Initialize DB, build the Application, run in polling mode.
    """
    # init DB
    init_db()

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN is not set in environment.")
        sys.exit("No BOT_TOKEN found.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[4:].strip()

    app = ApplicationBuilder().token(TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("group_add", group_add_cmd))
    app.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    app.add_handler(CommandHandler("show", show_groups_cmd))
    app.add_handler(CommandHandler("list", show_groups_cmd))
    app.add_handler(CommandHandler("link", link_cmd))
    app.add_handler(CommandHandler("rmove_user", rmove_user_cmd))
    app.add_handler(CommandHandler("be_sad", be_sad_cmd))
    app.add_handler(CommandHandler("be_happy", be_happy_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # Add your other commands (/bypass, /unbypass, /add_removed_user, etc.) similarly

    # Message Handlers:
    # 1) For deleting Arabic text/caption/PDF/photo
    app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO,
        delete_arabic_messages
    ))

    # 2) For short-term group deletion after forcibly removing a user
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))

    # 3) Catch the group name from ALLOWED_USER_ID’s non-command text
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_group_name_reply
    ))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Bot starting up... Only one instance can run (due to file lock).")
    app.run_polling()

if __name__ == "__main__":
    main()
