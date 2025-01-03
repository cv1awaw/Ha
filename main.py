# main.py

import os
import sys
import sqlite3
import logging
import html
import fcntl
from datetime import datetime, timedelta
import re
import asyncio
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

# Lock file path
LOCK_FILE = '/tmp/telegram_bot.lock'  # Change path as needed

# Timeframe (in seconds) to delete messages after user removal
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for highly detailed output
)
logger = logging.getLogger(__name__)

# ------------------- Pending Actions -------------------

# Dictionary to keep track of pending group names (for /group_add flow)
pending_group_names = {}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    Acquire a lock to ensure only one instance of the bot is running.
    """
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. Starting bot...")
        return lock
    except IOError:
        logger.error("Another instance of the bot is already running. Exiting.")
        sys.exit("Another instance of the bot is already running.")

def release_lock(lock):
    """
    Release the acquired lock.
    """
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

# Acquire lock at the start
lock = acquire_lock()

# Ensure lock is released on exit
import atexit
atexit.register(release_lock, lock)

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
        
        # Create removed_users table with group_id
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
        
        # Initialize permissions-related tables
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
        logger.info(f"Added group {group_id} to database (no name yet).")
    except Exception as e:
        logger.error(f"Error adding group {group_id}: {e}")
        raise

def set_group_name(g_id, group_name):
    """
    Set the name of a group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, g_id))
        conn.commit()
        conn.close()
        logger.info(f"Set group name for {g_id} to {group_name}")
    except Exception as e:
        logger.error(f"Error setting group name for {g_id}: {e}")
        raise

def group_exists(group_id):
    """
    Check if a group exists in the database.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM groups WHERE group_id = ?', (group_id,))
        exists = c.fetchone() is not None
        conn.close()
        logger.debug(f"Check if group {group_id} exists: {exists}")
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
        res = c.fetchone() is not None
        conn.close()
        logger.debug(f"Check if user {user_id} is bypassed: {res}")
        return res
    except Exception as e:
        logger.error(f"Error checking bypass status for user {user_id}: {e}")
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
        enabled = row[0] if row else False
        logger.debug(f"Is deletion enabled for group {group_id}: {enabled}")
        return bool(enabled)
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
        
        # Debug: show all rows in removed_users prior
        c.execute("SELECT group_id, user_id FROM removed_users")
        all_rows = c.fetchall()
        logger.debug(f"[remove_user_from_removed_users] Currently in removed_users: {all_rows}")

        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()

        if changes > 0:
            logger.info(f"Removed user {user_id} from removed_users for group {group_id}.")
            return True
        else:
            logger.warning(
                f"User {user_id} not in removed_users for group {group_id} (no rows deleted)."
            )
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from group {group_id} in removed_users: {e}")
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
        logger.info(f"Revoked permissions for user {user_id}. Set role to 'removed'.")
    except Exception as e:
        logger.error(f"Error revoking permissions for user {user_id}: {e}")
        raise

def list_removed_users(group_id=None):
    """
    Retrieve users from the removed_users table.
      - If group_id is None, returns a list of tuples: (group_id, user_id, removal_reason, removal_time)
      - Otherwise, returns (user_id, removal_reason, removal_time)
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
        logger.info("Fetched removed_users entries.")
        return data
    except Exception as e:
        logger.error(f"Error fetching removed_users: {e}")
        return []

# ------------------- Flag for Message Deletion -------------------

delete_all_messages_after_removal = {}

# ------------------- Command Handler Functions -------------------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle private messages for setting group names (after /group_add).
    """
    user = update.effective_user
    message_text = update.message.text.strip()
    logger.debug(f"Received private message from {user.id}: {message_text}")

    if user.id in pending_group_names:
        group_id = pending_group_names.pop(user.id)
        group_name = message_text

        if not group_name:
            warning_message = escape_markdown(
                "⚠️ Group name cannot be empty. Please try `/group_add` again.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(
                f"User {user.id} gave an empty group name for group {group_id}"
            )
            return

        try:
            set_group_name(group_id, group_name)
            confirmation_message = escape_markdown(
                f"✅ Set group `{group_id}` name to: *{group_name}*",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=confirmation_message,
                parse_mode='MarkdownV2'
            )
            logger.info(
                f"Group name for {group_id} set to {group_name} by user {user.id}"
            )
        except Exception as e:
            error_message = escape_markdown(
                "⚠️ Failed to set group name. Please try `/group_add` again.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=error_message,
                parse_mode='MarkdownV2'
            )
            logger.error(f"Error setting group name for {group_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - simple readiness check, only for the ALLOWED_USER_ID
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        msg = escape_markdown("✅ Bot is running and ready.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=msg,
            parse_mode='MarkdownV2'
        )
        logger.info(f"/start used by {user.id}")
    except Exception as e:
        logger.error(f"Error in /start: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_add <group_id> – register a group in the DB
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/group_add <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ group_id must be an integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if group_exists(g_id):
        msg = escape_markdown("⚠️ Group already registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        add_group(g_id)
        pending_group_names[user.id] = g_id
        confirmation = escape_markdown(
            f"✅ Group `{g_id}` added.\nPlease send the group name in a private message to the bot.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation,
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error adding group {g_id}: {e}")
        msg = escape_markdown("⚠️ Failed to add group. Please try again.", version=2)
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
        msg = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id = ?', (g_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()

        if changes > 0:
            cf = escape_markdown(f"✅ Group `{g_id}` removed.", version=2)
            await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
        else:
            warn = escape_markdown(f"⚠️ Group `{g_id}` not found.", version=2)
            await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error removing group {g_id}: {e}")
        msg = escape_markdown("⚠️ Failed to remove group. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bypass <user_id> – add a user to bypass list
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/bypass <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ user_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # Already bypassed?
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (uid,))
        already = c.fetchone()
        conn.close()

        if already:
            msg = escape_markdown(f"⚠️ User `{uid}` is already bypassed.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return
    except Exception as e:
        logger.error(f"Error checking bypass status for {uid}: {e}")
        msg = escape_markdown("⚠️ Internal check failed. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        add_bypass_user(uid)
        msg = escape_markdown(f"✅ User `{uid}` is now bypassed.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error bypassing user {uid}: {e}")
        msg = escape_markdown("⚠️ Failed to add bypass. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unbypass <user_id> – remove user from bypass
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/unbypass <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ user_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    res = remove_bypass_user(uid)
    if res:
        cf = escape_markdown(f"✅ Removed `{uid}` from bypass list.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    else:
        wr = escape_markdown(f"⚠️ `{uid}` not in bypass list.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')

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
            msg = escape_markdown("⚠️ No groups have been added.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        msg = "*Groups Information:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "Name not set"
            g_name_esc = escape_markdown(g_name_display, version=2)
            msg += f"*Group:* {g_name_esc}\n*Group ID:* `{g_id}`\n"

            # deletion setting
            try:
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (g_id,))
                row = c.fetchone()
                conn.close()
                status = "Enabled" if row and row[0] else "Disabled"
                msg += f"*Deletion:* `{status}`\n\n"
            except Exception as e:
                msg += "⚠️ Error fetching deletion status.\n"
                logger.error(f"Deletion status error for group {g_id}: {e}")

        # Send in chunks if too long
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                chunk = msg[i:i+4000]
                await context.bot.send_message(
                    chat_id=user.id,
                    text=chunk,
                    parse_mode='MarkdownV2'
                )
        else:
            await context.bot.send_message(
                chat_id=user.id,
                text=msg,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error showing groups: {e}")
        err_msg = escape_markdown("⚠️ Failed to get group list. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err_msg, parse_mode='MarkdownV2')

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_id – show the ID of the current group or the user’s ID if in private
    """
    user = update.effective_user
    chat = update.effective_chat

    if user.id != ALLOWED_USER_ID:
        return

    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        g_id = chat.id
        msg = escape_markdown(f"Group ID: `{g_id}`", version=2)
    else:
        msg = escape_markdown(f"Your User ID: `{user.id}`", version=2)

    try:
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /group_id: {e}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help – list available commands
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    help_text = """*Commands:*
• `/start` – Check if the bot is running
• `/group_add <group_id>` – Register a group by its chat ID
• `/rmove_group <group_id>` – Remove a registered group
• `/bypass <user_id>` – Add a user to the bypass list
• `/unbypass <user_id>` – Remove a user from the bypass list
• `/group_id` – Show the current group ID or your user ID (if private)
• `/show` – Display all groups & their settings
• `/info` – Display current config
• `/help` – Display this help text
• `/list` – Same as `/show`
• `/be_sad <group_id>` – Enable Arabic message deletion
• `/be_happy <group_id>` – Disable Arabic message deletion
• `/rmove_user <group_id> <user_id>` – Remove user from group + DB
• `/add_removed_user <group_id> <user_id>` – Add a user to the 'Removed Users'
• `/list_removed_users` – Show all users in 'Removed Users'
• `/unremove_user <group_id> <user_id>` – Remove a user from 'Removed Users'
• `/check <group_id>` – Validate 'Removed Users' vs. actual group membership
"""
    try:
        help_esc = escape_markdown(help_text, version=2)
        await context.bot.send_message(chat_id=user.id, text=help_esc, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /help: {e}")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /info – show groups, bypassed users, etc.
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
                name_disp = g_name if g_name else "Name not set"
                deletion_status = "Enabled" if enab else "Disabled"
                msg += f"• *Name:* {escape_markdown(name_disp, version=2)}\n"
                msg += f"  *ID:* `{g_id}`\n"
                msg += f"  *Deletion:* `{deletion_status}`\n\n"
        else:
            msg += "No groups added.\n\n"

        msg += "*Bypassed Users:*\n"
        if bypassed:
            for (uid,) in bypassed:
                msg += f"• `{uid}`\n"
        else:
            msg += "No bypassed users.\n"

        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                chunk = msg[i:i+4000]
                await context.bot.send_message(
                    chat_id=user.id, text=chunk, parse_mode='MarkdownV2'
                )
        else:
            await context.bot.send_message(
                chat_id=user.id, text=msg, parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error in /info: {e}")
        er = escape_markdown("⚠️ Failed to retrieve info. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=er, parse_mode='MarkdownV2')

# ------------------- Commands to manage removed_users -------------------

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
        msg = escape_markdown("⚠️ Both group_id and user_id must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        warn = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    # Check if user is already there
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id=? AND user_id=?', (g_id, u_id))
        already = c.fetchone()
        if already:
            conn.close()
            msg = escape_markdown(
                f"⚠️ User `{u_id}` is already in 'Removed Users' for group `{g_id}`.",
                version=2
            )
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (g_id, u_id, "Manually added"))
        conn.commit()
        conn.close()

        msg = escape_markdown(
            f"✅ Added user `{u_id}` to 'Removed Users' for group `{g_id}`.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error adding user {u_id} to removed_users: {e}")
        err = escape_markdown("⚠️ Failed to add user. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /list_removed_users – list all users in the 'Removed Users' table
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    try:
        removed_data = list_removed_users()
        if not removed_data:
            msg = escape_markdown("⚠️ 'Removed Users' is empty.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        # group them by group_id
        grouped = {}
        for g_id, u_id, reason, tstamp in removed_data:
            if g_id not in grouped:
                grouped[g_id] = []
            grouped[g_id].append((u_id, reason, tstamp))

        output = "*Removed Users:*\n\n"
        for g_id, items in grouped.items():
            output += f"*Group:* `{g_id}`\n"
            for (usr, reas, tm) in items:
                output += f"• *User:* `{usr}`\n"
                output += f"  *Reason:* {escape_markdown(reas, version=2)}\n"
                output += f"  *Removed At:* {tm}\n"
            output += "\n"

        if len(output) > 4000:
            for i in range(0, len(output), 4000):
                chunk = output[i:i+4000]
                await context.bot.send_message(
                    chat_id=user.id, text=chunk, parse_mode='MarkdownV2'
                )
        else:
            await context.bot.send_message(
                chat_id=user.id, text=output, parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error in /list_removed_users: {e}")
        msg = escape_markdown("⚠️ Failed to list removed users.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def unremove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unremove_user <group_id> <user_id> – remove user from 'Removed Users'
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = escape_markdown("⚠️ Usage: `/unremove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        msg = escape_markdown("⚠️ Both group_id and user_id must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        warn = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    removed = remove_user_from_removed_users(g_id, u_id)
    if not removed:
        msg = escape_markdown(
            f"⚠️ User `{u_id}` is not in the 'Removed Users' list for group `{g_id}`.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # Optionally revoke user permissions
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Error revoking perms for user {u_id}: {e}")

    cf = escape_markdown(
        f"✅ User `{u_id}` removed from 'Removed Users' for group `{g_id}`.",
        version=2
    )
    await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')

# ------------------- Remove or forcibly ban user from group -------------------

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_user <group_id> <user_id> – forcibly remove user from group & table
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        msg = escape_markdown("⚠️ Usage: `/rmove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        msg = escape_markdown("⚠️ Both group_id and user_id must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # 1) remove from bypass, if present
    remove_bypass_user(u_id)

    # 2) remove from removed_users
    remove_user_from_removed_users(g_id, u_id)

    # 3) revoke permissions
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Failed to revoke perms for {u_id}: {e}")

    # 4) ban from group
    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=u_id)
    except Exception as e:
        err = escape_markdown(
            f"⚠️ Could not ban `{u_id}` from group `{g_id}` (check bot perms).", version=2
        )
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')
        logger.error(f"Ban error for user {u_id} in group {g_id}: {e}")
        return

    # 5) set short-term deletion flag
    delete_all_messages_after_removal[g_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    asyncio.create_task(remove_deletion_flag_after_timeout(g_id))

    msg = escape_markdown(
        f"✅ Removed `{u_id}` from group `{g_id}`.\n"
        f"Messages for next {MESSAGE_DELETE_TIMEFRAME}s will be deleted.",
        version=2
    )
    await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

# ------------------- Deletion / Filtering Handlers -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete Arabic text messages in groups with deletion enabled.
    """
    msg = update.message
    if not msg or not msg.text:
        return

    user = msg.from_user
    g_id = msg.chat.id

    if not is_deletion_enabled(g_id):
        return

    if is_bypass_user(user.id):
        return

    if has_arabic(msg.text):
        try:
            await msg.delete()
            logger.info(f"Deleted Arabic text from {user.id} in group {g_id}")
        except Exception as e:
            logger.error(f"Error deleting msg in group {g_id}: {e}")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete any messages if the group is flagged after a removal for MESSAGE_DELETE_TIMEFRAME seconds.
    """
    msg = update.message
    if not msg:
        return

    g_id = msg.chat.id
    if g_id in delete_all_messages_after_removal:
        try:
            await msg.delete()
            logger.info(f"Deleted message in group {g_id}")
        except Exception as e:
            logger.error(f"Failed to delete a flagged message in group {g_id}: {e}")

# ------------------- Utility Functions -------------------

def has_arabic(text):
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

async def remove_deletion_flag_after_timeout(group_id):
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"Deletion flag removed for group {group_id}")

# ------------------- Commands to toggle Arabic deletion -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_sad <group_id> – enable deletion of Arabic messages
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/be_sad <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        enable_deletion(g_id)
        cf = escape_markdown(f"✅ Arabic deletion enabled for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error enabling deletion for group {g_id}: {e}")
        err = escape_markdown("⚠️ Could not enable. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_happy <group_id> – disable deletion of Arabic messages
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/be_happy <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        disable_deletion(g_id)
        cf = escape_markdown(f"✅ Arabic deletion disabled for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error disabling deletion for group {g_id}: {e}")
        err = escape_markdown("⚠️ Could not disable. Check logs.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')

# ------------------- Check Command -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /check <group_id> – verify 'Removed Users' vs. actual group membership
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        msg = escape_markdown("⚠️ Usage: `/check <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        msg = escape_markdown("⚠️ group_id must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        wr = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
        return

    # fetch removed users
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id = ?', (g_id,))
        removed_list = [row[0] for row in c.fetchall()]
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching removed users from group {g_id}: {e}")
        ef = escape_markdown("⚠️ DB error while fetching removed users.", version=2)
        await context.bot.send_message(chat_id=user.id, text=ef, parse_mode='MarkdownV2')
        return

    if not removed_list:
        msg = escape_markdown(f"⚠️ No removed users found for group `{g_id}`.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
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
            logger.error(f"Error getting chat member: user={uid}, group={g_id}. {e}")
            # If it fails to fetch, we assume user is not in group
            not_in.append(uid)

    resp = f"*Check Results for Group `{g_id}`:*\n\n"

    if still_in:
        resp += "*Users still in the group (despite being in removed list):*\n"
        for x in still_in:
            resp += f"• `{x}`\n"
        resp += "\n"
    else:
        resp += "All removed users are indeed out of the group.\n\n"

    if not_in:
        resp += "*Users not in the group (OK):*\n"
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
        msg = escape_markdown("⚠️ Error sending check results.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

    # Optionally auto-ban those still in group
    if still_in:
        for x in still_in:
            try:
                await context.bot.ban_chat_member(chat_id=g_id, user_id=x)
                logger.info(f"Auto-banned user {x} from group {g_id} via /check.")
            except Exception as e:
                logger.error(f"Failed to ban user {x} from group {g_id}: {e}")

# ------------------- main() -------------------

def main():
    """
    Initialize DB and run the bot with all handlers.
    """
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Cannot start bot: DB init failure: {e}")
        sys.exit("DB initialization failed.")

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN not set.")
        sys.exit("BOT_TOKEN not set.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[len('bot='):].strip()
        logger.warning("Stripped 'bot=' prefix from BOT_TOKEN.")

    try:
        app = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"Failed to build the application: {e}")
        sys.exit(f"Failed to build the application: {e}")

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

    # Message handlers
    # 1) Delete Arabic
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_arabic_messages
    ))
    # 2) Delete flagged
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))
    # 3) Private (handle group name)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message
    ))

    # Errors
    app.add_error_handler(error_handler)

    logger.info("Bot starting up...")
    try:
        app.run_polling()
    except Exception as e:
        logger.critical(f"Critical error, shutting down: {e}")
        sys.exit("Bot crashed.")

if __name__ == "__main__":
    main()
