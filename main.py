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

# Path to the SQLite database
DATABASE = 'warnings.db'

# Allowed user ID (Replace with your actual authorized user ID)
ALLOWED_USER_ID = 6177929931  # Example: 6177929931

# Lock file path (for preventing multiple instances)
LOCK_FILE = '/tmp/telegram_bot.lock'

# Timeframe (in seconds) to delete messages after user removal
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed output
)
logger = logging.getLogger(__name__)

# ------------------- Pending Actions -------------------

pending_group_names = {}  # { user_id: group_id } for /group_add -> name

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    Acquire a lock to ensure only one instance of the bot is running.
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
    Release the acquired lock at exit.
    """
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot is stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

# Acquire lock at import time
lock_file = acquire_lock()

import atexit
atexit.register(release_lock, lock_file)

# ------------------- Database Initialization -------------------

def init_permissions_db():
    """
    Initialize the permissions and removed_users tables.
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
    Initialize the SQLite database and create necessary tables if they don't exist.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("PRAGMA foreign_keys = 1")  # Enable foreign key constraints
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
        logger.error(f"Failed to initialize the database: {e}")
        raise

# ------------------- Database Helper Functions -------------------

def add_group(group_id):
    """
    Add a group by its chat ID.
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
        logger.info(f"Added group {group_id} to the DB with no initial name.")
    except Exception as e:
        logger.error(f"Error adding group {group_id}: {e}")
        raise

def set_group_name(g_id, group_name):
    """
    Set the name of a group in the DB.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, g_id))
        conn.commit()
        conn.close()
        logger.info(f"Set group name for {g_id} to '{group_name}' in DB.")
    except Exception as e:
        logger.error(f"Error setting group name for {g_id}: {e}")
        raise

def group_exists(group_id):
    """
    Check if a group exists in the DB.
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
    Check if a user is in the bypass list.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (user_id,))
        found = c.fetchone() is not None
        conn.close()
        return found
    except Exception as e:
        logger.error(f"Error checking if user {user_id} is bypassed: {e}")
        return False

def add_bypass_user(user_id):
    """
    Add a user to the bypass list.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO bypass_users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"Added user {user_id} to bypass list.")
    except Exception as e:
        logger.error(f"Error adding user {user_id} to bypass list: {e}")
        raise

def remove_bypass_user(user_id):
    """
    Remove a user from the bypass list.
    Returns True if removed, False if not found.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM bypass_users WHERE user_id = ?', (user_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"Removed user {user_id} from bypass list.")
            return True
        else:
            logger.warning(f"User {user_id} not found in bypass list.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from bypass list: {e}")
        return False

def enable_deletion(group_id):
    """
    Enable message deletion for a specific group.
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
        logger.info(f"Enabled message deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error enabling deletion for group {group_id}: {e}")
        raise

def disable_deletion(group_id):
    """
    Disable message deletion for a specific group.
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
        logger.info(f"Disabled message deletion for group {group_id}.")
    except Exception as e:
        logger.error(f"Error disabling deletion for group {group_id}: {e}")
        raise

def is_deletion_enabled(group_id):
    """
    Check if message deletion is enabled for a specific group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (group_id,))
        row = c.fetchone()
        conn.close()
        return bool(row[0]) if row else False
    except Exception as e:
        logger.error(f"Error checking deletion status for group {group_id}: {e}")
        return False

def remove_user_from_removed_users(group_id, user_id):
    """
    Remove a user from the removed_users table for a specific group.
    Returns True if row was deleted, False if not found.
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
        logger.error(f"Error removing user {user_id} from 'removed_users': {e}")
        return False

def revoke_user_permissions(user_id):
    """
    Revoke all permissions for a user by setting their role to 'removed'.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE permissions SET role = ? WHERE user_id = ?', ('removed', user_id))
        conn.commit()
        conn.close()
        logger.info(f"Revoked permissions for user {user_id} (set role='removed').")
    except Exception as e:
        logger.error(f"Error revoking permissions for user {user_id}: {e}")
        raise

def list_removed_users(group_id=None):
    """
    Retrieve users from the removed_users table.
      - If group_id is None, returns all: (group_id, user_id, removal_reason, removal_time)
      - Otherwise, returns (user_id, removal_reason, removal_time) for that group
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
            data = c.fetchall()
        else:
            c.execute('''
                SELECT group_id, user_id, removal_reason, removal_time
                FROM removed_users
            ''')
            data = c.fetchall()
        conn.close()
        return data
    except Exception as e:
        logger.error(f"Error fetching 'removed_users': {e}")
        return []

# ------------------- Flag for Message Deletion -------------------

delete_all_messages_after_removal = {}

# ------------------- Command Handler Functions -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - readiness check, only for ALLOWED_USER_ID.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=escape_markdown("✅ Bot is up and running.", version=2),
            parse_mode='MarkdownV2'
        )
        logger.info(f"/start used by {user.id}")
    except Exception as e:
        logger.error(f"Error in /start: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_add <group_id> – register a group in the DB,
    then wait for the user to send the group name in any chat message.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/group_add <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # Validate group_id
    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ group_id must be an integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # Check if group is already registered
    if group_exists(g_id):
        msg = escape_markdown("⚠️ That group is already registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # Insert group
    try:
        add_group(g_id)
        # Mark user as pending name input
        pending_group_names[user.id] = g_id
        msg = escape_markdown(
            f"✅ Group `{g_id}` added.\nNow send me the group name in any message (not a command).",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error adding group {g_id}: {e}")
        msg = escape_markdown("⚠️ Failed to add group to DB. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def handle_group_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Whenever the ALLOWED_USER_ID sends a non-command text, check if they're pending a group name.
    If yes, set the group name and confirm.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return  # only handle the special user’s text

    # If user is in pending_group_names, they owe us a group name for group_id
    if user.id not in pending_group_names:
        return  # They’re not setting a group name right now

    message_text = (update.message.text or "").strip()
    if not message_text:
        # They sent something empty or not text-based, do nothing
        logger.warning("User provided empty group name text. Ignoring.")
        return

    # Pop the group_id from our dictionary
    group_id = pending_group_names.pop(user.id)
    try:
        set_group_name(group_id, message_text)
        reply = escape_markdown(
            f"✅ Group `{group_id}` name set to: *{message_text}*",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=reply, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error setting group name for {group_id}: {e}")
        msg = escape_markdown("⚠️ Could not set group name. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_group <group_id> – remove a group from registration
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/rmove_group <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        warn = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id = ?', (g_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()

        if changes > 0:
            cf = escape_markdown(f"✅ Group `{g_id}` removed from DB.", version=2)
            await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
        else:
            wr = escape_markdown(f"⚠️ Group `{g_id}` not found in DB.", version=2)
            await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error removing group {g_id}: {e}")
        msg = escape_markdown("⚠️ Could not remove group. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bypass <user_id> – add a user to bypass list
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        warn = escape_markdown("⚠️ Usage: `/bypass <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        warn = escape_markdown("⚠️ user_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    # check if already bypassed
    if is_bypass_user(uid):
        wr = escape_markdown(f"⚠️ User `{uid}` is already bypassed.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    try:
        add_bypass_user(uid)
        cf = escape_markdown(f"✅ User `{uid}` added to bypass list.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error bypassing user {uid}: {e}")
        msg = escape_markdown("⚠️ Failed to add user to bypass. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unbypass <user_id> – remove user from bypass
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        warn = escape_markdown("⚠️ Usage: `/unbypass <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        wr = escape_markdown("⚠️ user_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    res = remove_bypass_user(uid)
    if res:
        cf = escape_markdown(f"✅ User `{uid}` removed from bypass list.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    else:
        wr = escape_markdown(f"⚠️ User `{uid}` was not in bypass list.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_id – show the ID of the current group or the user’s ID if in private
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    chat = update.effective_chat
    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        g_id = chat.id
        msg = escape_markdown(f"Group ID: `{g_id}`", version=2)
    else:
        msg = escape_markdown(f"Your User ID: `{user.id}`", version=2)

    try:
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /group_id: {e}")

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /show or /list – show all groups and their deletion settings
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
            msg = escape_markdown("⚠️ No groups have been added yet.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        msg = "*Groups Information:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "(no name set)"
            g_name_esc = escape_markdown(g_name_display, version=2)
            msg += f"*Group:* {g_name_esc}\n*Group ID:* `{g_id}`\n"

            # Deletion setting
            try:
                conn2 = sqlite3.connect(DATABASE)
                c2 = conn2.cursor()
                c2.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (g_id,))
                row = c2.fetchone()
                conn2.close()
                status = "Enabled" if row and row[0] else "Disabled"
                msg += f"*Deletion:* `{status}`\n\n"
            except Exception as e:
                msg += "⚠️ Error fetching deletion status.\n"
                logger.error(f"Deletion status error for group {g_id}: {e}")

        # Send in chunks if message is large
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                chunk = msg[i:i+4000]
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

    except Exception as e:
        logger.error(f"Error showing groups: {e}")
        err = escape_markdown("⚠️ Failed to load group list. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help – list available commands
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    help_text = """*Available Commands:*
• `/start` – Check if the bot is alive
• `/group_add <group_id>` – Register a new group by its numeric ID
• (Then send a normal text with the group name to set it)
• `/rmove_group <group_id>` – Remove a group from the DB
• `/bypass <user_id>` – Add user to bypass list
• `/unbypass <user_id>` – Remove user from bypass list
• `/group_id` – Show the current group ID (or your user ID if private chat)
• `/show` or `/list` – List all groups & their statuses
• `/info` – Show more detailed config info
• `/help` – This help text

• `/be_sad <group_id>` – Enable Arabic message deletion for a group
• `/be_happy <group_id>` – Disable Arabic message deletion for that group
• `/rmove_user <group_id> <user_id>` – Remove a user forcibly from group + DB
• `/add_removed_user <group_id> <user_id>` – Mark user as "removed" in DB only
• `/list_removed_users` – Show all "removed" users
• `/unremove_user <group_id> <user_id>` – Remove from "removed" list
• `/check <group_id>` – Check group for "removed" users still present
• `/link <group_id>` – Create a one-time invite link (for that group, if in private chat)
"""
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=escape_markdown(help_text, version=2),
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error in /help: {e}")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /info – show config (groups & bypass list, etc.)
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        c.execute('''
            SELECT g.group_id, g.group_name, ds.enabled
            FROM groups g
            LEFT JOIN deletion_settings ds ON g.group_id = ds.group_id
        ''')
        groups = c.fetchall()

        c.execute('SELECT user_id FROM bypass_users')
        bypassed = c.fetchall()

        conn.close()

        msg = "*Bot Info:*\n\n"
        msg += "*Groups:*\n"
        if groups:
            for g_id, g_name, enab in groups:
                name_disp = g_name if g_name else "(no name set)"
                deletion_status = "Enabled" if enab else "Disabled"
                msg += f"• *Name:* {escape_markdown(name_disp, version=2)}\n"
                msg += f"  *ID:* `{g_id}`\n"
                msg += f"  *Deletion:* `{deletion_status}`\n\n"
        else:
            msg += "No groups.\n\n"

        msg += "*Bypassed Users:*\n"
        if bypassed:
            for (uid,) in bypassed:
                msg += f"• `{uid}`\n"
        else:
            msg += "(none)\n"

        # send in chunks if big
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                chunk = msg[i:i+4000]
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

    except Exception as e:
        logger.error(f"Error in /info: {e}")
        ef = escape_markdown("⚠️ Could not retrieve info from DB. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=ef, parse_mode='MarkdownV2')

# ------------------- Additional Commands for "Removed Users" flow -------------------

async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_removed_user <group_id> <user_id> – manually add a user to 'Removed Users'
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = escape_markdown("⚠️ Usage: `/add_removed_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        war = escape_markdown("⚠️ group_id and user_id must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        warn = escape_markdown(f"⚠️ Group `{g_id}` not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    # Check if user is already in removed_users
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id=? AND user_id=?', (g_id, u_id))
        already = c.fetchone()
        if already:
            conn.close()
            msg = escape_markdown(f"⚠️ That user is already in 'Removed Users' for group `{g_id}`.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (g_id, u_id, "Manually added"))
        conn.commit()
        conn.close()

        cf = escape_markdown(f"✅ Added user `{u_id}` to 'Removed Users' for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error adding user {u_id} to removed_users: {e}")
        er = escape_markdown("⚠️ Could not add user to 'Removed Users'. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=er, parse_mode='MarkdownV2')

async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /list_removed_users – list all users in the 'Removed Users' table
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        data = list_removed_users()
        if not data:
            msg = escape_markdown("⚠️ 'Removed Users' table is empty.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        # group them by group_id
        grouped = {}
        for (g_id, u_id, reason, tstamp) in data:
            if g_id not in grouped:
                grouped[g_id] = []
            grouped[g_id].append((u_id, reason, tstamp))

        out = "*Removed Users:*\n\n"
        for g_id, items in grouped.items():
            out += f"*Group:* `{g_id}`\n"
            for (usr, reas, tm) in items:
                out += f"• *User:* `{usr}`\n"
                out += f"  *Reason:* {escape_markdown(reas, version=2)}\n"
                out += f"  *Removed At:* {tm}\n"
            out += "\n"

        if len(out) > 4000:
            for i in range(0, len(out), 4000):
                chunk = out[i:i+4000]
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=out, parse_mode='MarkdownV2')

    except Exception as e:
        logger.error(f"Error listing removed users: {e}")
        msg = escape_markdown("⚠️ Could not list 'Removed Users'. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def unremove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unremove_user <group_id> <user_id> – remove user from 'Removed Users'
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        war = escape_markdown("⚠️ Usage: `/unremove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        msg = escape_markdown("⚠️ Both IDs must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        wr = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    removed = remove_user_from_removed_users(g_id, u_id)
    if not removed:
        wrn = escape_markdown(
            f"⚠️ User `{u_id}` not found in 'Removed Users' for group `{g_id}`.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=wrn, parse_mode='MarkdownV2')
        return

    # Optionally revoke permissions
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Error revoking perms for user {u_id}: {e}")

    cf = escape_markdown(f"✅ Removed user `{u_id}` from 'Removed Users' for group `{g_id}`.", version=2)
    await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')

# ------------------- Forcible Remove from Group -------------------

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_user <group_id> <user_id> – forcibly remove user from group & DB
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        war = escape_markdown("⚠️ Usage: `/rmove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        msg = escape_markdown("⚠️ Both IDs must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # 1) remove from bypass
    remove_bypass_user(u_id)

    # 2) remove from removed_users
    remove_user_from_removed_users(g_id, u_id)

    # 3) revoke perms
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Failed to revoke perms for {u_id}: {e}")

    # 4) ban from group
    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=u_id)
    except Exception as e:
        err = escape_markdown(
            f"⚠️ Could not ban user `{u_id}` from group `{g_id}` (check bot perms).",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')
        logger.error(f"Ban error for user {u_id} in group {g_id}: {e}")
        return

    # 5) set short-term deletion flag
    delete_all_messages_after_removal[g_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    asyncio.create_task(remove_deletion_flag_after_timeout(g_id))

    cf = escape_markdown(
        f"✅ Removed `{u_id}` from group `{g_id}`.\n"
        f"All messages in the next {MESSAGE_DELETE_TIMEFRAME}s will be deleted.",
        version=2
    )
    await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')

# ------------------- Deletion / Filtering Handlers -------------------

def has_arabic(text):
    """
    Return True if text has any Arabic characters (Unicode range 0600-06FF).
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete messages (including PDF/image contents) if Arabic text is detected.
    Only if is_deletion_enabled for that group.
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    g_id = msg.chat.id

    # 1) Check if deletion is enabled for this group
    if not is_deletion_enabled(g_id):
        return

    # 2) Bypass check
    if is_bypass_user(user.id):
        return

    # 3) Check text or caption
    text_or_caption = (msg.text or msg.caption or "")
    if text_or_caption and has_arabic(text_or_caption):
        try:
            await msg.delete()
            logger.info(f"Deleted Arabic text from user {user.id} in group {g_id}.")
        except Exception as e:
            logger.error(f"Error deleting Arabic message in group {g_id}: {e}")
        return

    # 4) If there's a PDF
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
                        except Exception as e:
                            logger.error(f"PyPDF2 error reading PDF: {e}")
                            reader = None

                        if reader:
                            extracted_text = ""
                            for page in reader.pages:
                                extracted_text += page.extract_text() or ""
                            if extracted_text and has_arabic(extracted_text):
                                try:
                                    await msg.delete()
                                    logger.info(f"Deleted PDF with Arabic text from user {user.id} in group {g_id}.")
                                except Exception as e:
                                    logger.error(f"Error deleting PDF in group {g_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to parse PDF: {e}")
                finally:
                    try:
                        os.remove(tmp_pdf.name)
                    except:
                        pass
        else:
            logger.warning("PDF check skipped - PyPDF2 not installed.")

    # 5) If there's a photo
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
                    text_extracted = pytesseract.image_to_string(Image.open(tmp_img.name)) or ""
                    if text_extracted and has_arabic(text_extracted):
                        try:
                            await msg.delete()
                            logger.info(f"Deleted image with Arabic text from user {user.id} in group {g_id}.")
                        except Exception as e:
                            logger.error(f"Error deleting image in group {g_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to OCR image: {e}")
                finally:
                    try:
                        os.remove(tmp_img.name)
                    except:
                        pass
        else:
            logger.warning("Image OCR check skipped - pytesseract or Pillow not installed.")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If the group is in the short-term deletion dict after forcibly removing a user,
    delete all messages for that timeframe.
    """
    msg = update.message
    if not msg:
        return

    g_id = msg.chat.id

    # If group is flagged for short-term mass deletion
    if g_id in delete_all_messages_after_removal:
        # Check if the timeframe expired
        expiry = delete_all_messages_after_removal[g_id]
        if datetime.utcnow() > expiry:
            delete_all_messages_after_removal.pop(g_id, None)
            logger.info(f"Short-term deletion window expired for group {g_id}.")
            return

        # Otherwise, delete the message
        try:
            await msg.delete()
            logger.info(f"Deleted message in group {g_id} under short-term deletion window.")
        except Exception as e:
            logger.error(f"Failed to delete message in group {g_id}: {e}")

# ------------------- Commands to toggle Arabic deletion -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_sad <group_id> – enable deletion of Arabic messages
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        war = escape_markdown("⚠️ Usage: `/be_sad <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        wr = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    try:
        enable_deletion(g_id)
        msg = escape_markdown(f"✅ Arabic deletion enabled for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error enabling Arabic deletion for {g_id}: {e}")
        err = escape_markdown("⚠️ Could not enable deletion. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_happy <group_id> – disable deletion of Arabic messages
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        war = escape_markdown("⚠️ Usage: `/be_happy <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        wr = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    try:
        disable_deletion(g_id)
        cf = escape_markdown(f"✅ Arabic deletion disabled for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error disabling Arabic deletion for {g_id}: {e}")
        err = escape_markdown("⚠️ Could not disable. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

# ------------------- /check & /link Commands -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check <group_id> – verify 'Removed Users' vs. actual group membership, auto-ban any found still inside
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        war = escape_markdown("⚠️ Usage: `/check <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        wr = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        msg = escape_markdown(f"⚠️ Group `{g_id}` not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # fetch removed users
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id=?', (g_id,))
        removed_list = [row[0] for row in c.fetchall()]
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching removed users from group {g_id}: {e}")
        ef = escape_markdown("⚠️ DB error fetching removed_users.", version=2)
        await context.bot.send_message(chat_id=user.id, text=ef, parse_mode='MarkdownV2')
        return

    if not removed_list:
        msg = escape_markdown(f"⚠️ No removed users found for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    still_in, not_in = [], []
    for uid in removed_list:
        try:
            member = await context.bot.get_chat_member(chat_id=g_id, user_id=uid)
            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                still_in.append(uid)
            else:
                not_in.append(uid)
        except Exception as e:
            logger.error(f"Error get_chat_member for user {uid} in group {g_id}: {e}")
            # If we fail to fetch, we assume user is not in group
            not_in.append(uid)

    resp = f"*Check Results for Group `{g_id}`:*\n\n"

    if still_in:
        resp += "*These removed users are still in the group:*\n"
        for x in still_in:
            resp += f"• `{x}`\n"
        resp += "\n"
    else:
        resp += "All removed users are out of the group.\n\n"

    if not_in:
        resp += "*Not in the group (OK):*\n"
        for x in not_in:
            resp += f"• `{x}`\n"
        resp += "\n"

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=escape_markdown(resp, version=2),
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error sending check results: {e}")

    # Optional: auto-ban those still in group
    for x in still_in:
        try:
            await context.bot.ban_chat_member(chat_id=g_id, user_id=x)
            logger.info(f"Auto-banned user {x} in group {g_id} after /check command.")
        except Exception as e:
            logger.error(f"Failed to ban user {x} in group {g_id}: {e}")

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /link <group_id> – create a one-time-use invite link for the group (must be admin in that group).
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        war = escape_markdown("⚠️ Usage: `/link <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=war, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        wr = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        msg = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # create one-time invite link
    try:
        invite_link_obj = await context.bot.create_chat_invite_link(
            chat_id=g_id,
            member_limit=1,  # becomes invalid after 1 use
            name="One-Time Link"
        )
        confirmation = escape_markdown(
            f"✅ One-time invite link for group `{g_id}`:\n{invite_link_obj.invite_link}",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=confirmation, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error creating one-time invite link for group {g_id}: {e}")
        err = escape_markdown("⚠️ Could not create invite link. Check bot admin rights & logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

# ------------------- Utility / Cleanup -------------------

async def remove_deletion_flag_after_timeout(group_id):
    """
    After MESSAGE_DELETE_TIMEFRAME seconds, remove the short-term deletion flag for that group.
    """
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    if group_id in delete_all_messages_after_removal:
        delete_all_messages_after_removal.pop(group_id, None)
        logger.info(f"Short-term deletion flag cleared for group {group_id} after timeout.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Global error handler
    """
    logger.error("Error in the bot:", exc_info=context.error)

# ------------------- main() -------------------

def main():
    """
    The main entry point – Initializes the DB, sets up the Telegram Application,
    and runs in polling mode.
    """
    # Initialize the DB
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Cannot start bot due to DB init failure: {e}")
        sys.exit("DB init failed, exiting.")

    # Get BOT_TOKEN from environment or a safe place
    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN is not set in the environment.")
        sys.exit("No BOT_TOKEN found. Exiting.")
    TOKEN = TOKEN.strip()

    if TOKEN.lower().startswith('bot='):
        # If there's a 'bot=' prefix, strip it
        TOKEN = TOKEN[4:].strip()
        logger.warning("Stripped 'bot=' prefix from BOT_TOKEN.")

    try:
        app = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"Failed to build the telegram application: {e}")
        sys.exit("Bot initialization error.")

    # Register commands
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CommandHandler("unremove_user", unremove_user_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("link", link_cmd))

    # Message Handlers:
    # 1) If the message is text/caption/PDF/photo, check for Arabic
    app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO,
        delete_arabic_messages
    ))

    # 2) Short-term group deletion after forcibly removing a user
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))

    # 3) Catch any text from ALLOWED_USER_ID that might set a group name
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_group_name_reply
    ))

    # Global error handler
    app.add_error_handler(error_handler)

    # Start polling
    logger.info("Bot is starting... Only one instance should run (due to file lock).")
    try:
        app.run_polling()
    except Exception as e:
        logger.critical(f"Critical error, shutting down bot: {e}")
        sys.exit("Bot crashed.")

if __name__ == "__main__":
    main()
