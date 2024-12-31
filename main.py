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
MESSAGE_DELETE_TIMEFRAME = 15  # Increased to 15 seconds to better capture system messages

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more verbose output
)
logger = logging.getLogger(__name__)

# ------------------- Pending Actions -------------------

# Dictionary to keep track of pending group names
pending_group_names = {}

# Dictionary to keep track of pending user removals
# Format: {user_id: group_id}
pending_user_removals = {}

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
        c.execute('INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)', (group_id, None))
        conn.commit()
        conn.close()
        logger.info(f"Added group {group_id} to database with no name.")
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

def add_user_to_removed_users(group_id, user_id, removal_reason="Removed via /rmove_user"):
    """
    Add a user to the removed_users table for a specific group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (group_id, user_id, removal_reason))
        conn.commit()
        conn.close()
        logger.info(f"Added user {user_id} to removed_users list for group {group_id}.")
    except sqlite3.IntegrityError:
        logger.warning(f"User {user_id} is already in removed_users list for group {group_id}.")
    except Exception as e:
        logger.error(f"Error adding user {user_id} to removed_users for group {group_id}: {e}")
        raise

def remove_user_from_removed_users(group_id, user_id):
    """
    Remove a user from the removed_users table for a specific group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            logger.info(f"Removed user {user_id} from removed_users list for group {group_id}.")
            return True
        else:
            logger.warning(f"User {user_id} not found in removed_users list for group {group_id}.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from removed_users for group {group_id}: {e}")
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
    Retrieve all users from the removed_users table.
    If group_id is provided, filter by that group.
    Returns a list of tuples containing user_id, removal_reason, and removal_time.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        if group_id:
            c.execute('SELECT user_id, removal_reason, removal_time FROM removed_users WHERE group_id = ?', (group_id,))
        else:
            c.execute('SELECT group_id, user_id, removal_reason, removal_time FROM removed_users')
        users = c.fetchall()
        conn.close()
        logger.info("Fetched list of removed users.")
        return users
    except Exception as e:
        logger.error(f"Error fetching removed users: {e}")
        return []

# ------------------- Flag for Message Deletion -------------------

# Dictionary to track groups that should delete messages after removal
# Format: {group_id: expiration_time}
delete_all_messages_after_removal = {}

# ------------------- Command Handler Functions -------------------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle private messages for setting group names or removing users from the removed_users list.
    """
    user = update.effective_user
    message_text = update.message.text.strip()
    logger.debug(f"Received private message from user {user.id}: {message_text}")
    
    if user.id in pending_group_names:
        # Handle group name assignment
        group_id = pending_group_names.pop(user.id)
        group_name = message_text
        if group_name:
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
                logger.info(f"Set group name for {group_id} to {group_name} by user {user.id}")
            except Exception as e:
                error_message = escape_markdown("⚠️ Failed to set group name. Please try `/group_add` again.", version=2)
                await context.bot.send_message(
                    chat_id=user.id,
                    text=error_message,
                    parse_mode='MarkdownV2'
                )
                logger.error(f"Error setting group name for {group_id} by user {user.id}: {e}")
        else:
            warning_message = escape_markdown("⚠️ Group name cannot be empty. Please try `/group_add` again.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Received empty group name from user {user.id} for group {group_id}")
    
    elif user.id in pending_user_removals:
        # Handle user removal from removed_users list
        group_id = pending_user_removals.pop(user.id)
        try:
            target_user_id = int(message_text)
        except ValueError:
            message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Received invalid user_id '{message_text}' from user {user.id} for removal from group {group_id}")
            return
        
        # Check if the user is in the removed_users list for the group
        try:
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute('SELECT 1 FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
            if not c.fetchone():
                conn.close()
                message = escape_markdown(f"⚠️ User `{target_user_id}` is not in the 'Removed Users' list for group `{group_id}`.", version=2)
                await context.bot.send_message(
                    chat_id=user.id,
                    text=message,
                    parse_mode='MarkdownV2'
                )
                logger.warning(f"User {target_user_id} not found in 'Removed Users' for group {group_id} during removal by user {user.id}")
                return
            # Proceed to remove
            c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
            conn.commit()
            conn.close()
        except Exception as e:
            message = escape_markdown("⚠️ Failed to remove user from 'Removed Users'. Please try again later.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.error(f"Error removing user {target_user_id} from 'Removed Users' for group {group_id}: {e}")
            return
        
        # Optionally, revoke permissions if necessary
        try:
            revoke_user_permissions(target_user_id)
        except Exception as e:
            logger.error(f"Error revoking permissions for user {target_user_id}: {e}")
            # Not critical to send message; user is removed from 'Removed Users' list
            # So we can proceed
        
        confirmation_message = escape_markdown(
            f"✅ User `{target_user_id}` has been removed from the 'Removed Users' list for group `{group_id}`.",
            version=2
        )
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=confirmation_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Removed user {target_user_id} from 'Removed Users' for group {group_id} by user {user.id}")
        except Exception as e:
            logger.error(f"Error sending confirmation message for user removal: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /start command.
    """
    try:
        user = update.effective_user
        if user.id != ALLOWED_USER_ID:
            return  # Ignore unauthorized users
        message = escape_markdown("✅ Bot is running and ready.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"/start invoked by user {user.id}")
    except Exception as e:
        logger.error(f"Error handling /start command: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /group_add command to register a group.
    Usage: /group_add <group_id>
    """
    user = update.effective_user
    logger.debug(f"/group_add called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/group_add <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /group_add by user {user.id}")
        return

    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /group_add by user {user.id}")
        return

    if group_exists(group_id):
        message = escape_markdown("⚠️ Group already added.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.debug(f"Group {group_id} already exists.")
        return

    try:
        add_group(group_id)
        logger.debug(f"Added group {group_id} to database.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add group. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Failed to add group {group_id} by user {user.id}: {e}")
        return

    pending_group_names[user.id] = group_id
    logger.info(f"Added group {group_id}, awaiting group name from user {user.id} in private chat.")
    
    try:
        confirmation_message = escape_markdown(
            f"✅ Group `{group_id}` added.\nPlease send the group name in a private message to the bot.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error sending confirmation for /group_add: {e}")

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /rmove_group command to remove a registered group.
    Usage: /rmove_group <group_id>
    """
    user = update.effective_user
    logger.debug(f"/rmove_group called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/rmove_group <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /rmove_group by user {user.id}")
        return
    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /rmove_group by user {user.id}")
        return

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM groups WHERE group_id = ?', (group_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        if changes > 0:
            confirm_message = escape_markdown(
                f"✅ Group `{group_id}` has been removed from registration.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=confirm_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Removed group {group_id} by user {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ Group `{group_id}` does not exist.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to remove non-existent group {group_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to remove group. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing group {group_id} by user {user.id}: {e}")

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /bypass command to add a user to the bypass list.
    Usage: /bypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"/bypass called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/bypass <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /bypass by user {user.id}")
        return

    try:
        target_user_id = int(context.args[0])
        logger.debug(f"Parsed target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Invalid user_id provided to /bypass by user {user.id}")
        return

    # Check if user is already in bypass list
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (target_user_id,))
        if c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ User `{target_user_id}` is already in the bypass list.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to add already bypassed user {target_user_id} by user {user.id}")
            return
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ Failed to check bypass status. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error checking bypass status for user {target_user_id}: {e}")
        return

    try:
        add_bypass_user(target_user_id)
        confirmation_message = escape_markdown(
            f"✅ Added user `{target_user_id}` to the bypass list.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Added user {target_user_id} to bypass list by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add user to bypass list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error adding user {target_user_id} to bypass list by user {user.id}: {e}")

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /unbypass command to remove a user from the bypass list.
    Usage: /unbypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"/unbypass called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/unbypass <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /unbypass by user {user.id}")
        return
    try:
        target_user_id = int(context.args[0])
        logger.debug(f"Parsed target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Invalid user_id provided to /unbypass by user {user.id}")
        return

    try:
        if remove_bypass_user(target_user_id):
            confirmation_message = escape_markdown(
                f"✅ Removed user `{target_user_id}` from the bypass list.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=confirmation_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Removed user {target_user_id} from bypass list by user {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ User `{target_user_id}` was not in the bypass list.",
                version=2
            )
            await context.bot.send_message(
                chat_id=user.id,
                text=warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to remove non-bypassed user {target_user_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to remove user from bypass list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing user {target_user_id} from bypass list by user {user.id}: {e}")

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /show or /list command to display all groups and their settings.
    """
    user = update.effective_user
    logger.debug(f"/show called by user {user.id}")
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT group_id, group_name FROM groups')
        groups_data = c.fetchall()
        conn.close()

        if not groups_data:
            message = escape_markdown("⚠️ No groups have been added.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.debug("No groups found in the database.")
            return

        msg = "*Groups Information:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "Name not set"
            g_name_esc = escape_markdown(g_name_display, version=2)
            msg += f"*Group:* {g_name_esc}\n*Group ID:* `{g_id}`\n"

            # Fetch deletion settings
            try:
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute('SELECT enabled FROM deletion_settings WHERE group_id = ?', (g_id,))
                row = c.fetchone()
                conn.close()
                deletion_status = "Enabled" if row and row[0] else "Disabled"
                msg += f"*Deletion Status:* `{deletion_status}`\n"
            except Exception as e:
                msg += "⚠️ Error fetching deletion status.\n"
                logger.error(f"Error fetching deletion status for group {g_id}: {e}")

            msg += "\n"

        try:
            # Telegram message length limit is 4096 characters
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
            logger.info("Displayed comprehensive bot information.")
        except Exception as e:
            logger.error(f"Error sending /show information: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the list.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error handling /show command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve list information. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /group_id command to retrieve the current group or user ID.
    """
    user = update.effective_user
    group = update.effective_chat
    user_id = user.id
    logger.debug(f"/group_id called by user {user_id} in chat {group.id}")
    
    if user_id != ALLOWED_USER_ID:
        return  # Only respond to authorized user
    
    try:
        if group.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            group_id = group.id
            message = escape_markdown(f"🔢 *Group ID:* `{group_id}`", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Sent group ID {group_id} to user {user_id}")
        else:
            # If it's a private chat
            message = escape_markdown(f"🔢 *Your User ID:* `{user_id}`", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Sent user ID {user_id} in private chat.")
    except Exception as e:
        logger.error(f"Error handling /group_id command: {e}")
        message = escape_markdown("⚠️ An error occurred while processing the command.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /help command to display available commands.
    """
    user = update.effective_user
    logger.debug(f"/help called by user {user.id}, ALLOWED_USER_ID={ALLOWED_USER_ID}")
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user
    help_text = """*Available Commands:*
• `/start` - Check if the bot is running
• `/group_add <group_id>` - Register a group (use the actual chat ID of the group)
• `/rmove_group <group_id>` - Remove a registered group
• `/bypass <user_id>` - Add a user to bypass warnings
• `/unbypass <user_id>` - Remove a user from bypass warnings
• `/group_id` - Retrieve current group ID or user ID
• `/show` - Display all groups and their deletion settings
• `/info` - Display current bot configuration
• `/help` - Display this help message
• `/list` - Overview of groups and bypassed users
• `/be_sad <group_id>` - Enable Arabic message deletion in the group
• `/be_happy <group_id>` - Disable Arabic message deletion in the group
• `/rmove_user <group_id> <user_id>` - Remove a user from a group without sending notifications
• `/add_removed_user <group_id> <user_id>` - Add a user to the 'Removed Users' list for a specific group
• `/list_removed_users` - Display all users in the 'Removed Users' list for each group
• `/list_rmoved_rmove <group_id>` - Request removal of a user from the 'Removed Users' list for a specific group
• `/check <group_id>` - Verify the 'Removed Users' list against actual group members and remove discrepancies
"""
    try:
        # Escape special characters for MarkdownV2
        help_text_esc = escape_markdown(help_text, version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=help_text_esc,
            parse_mode='MarkdownV2'
        )
        logger.info("Displayed help information to user.")
    except Exception as e:
        logger.error(f"Error sending help information: {e}")
        message = escape_markdown("⚠️ An error occurred while sending the help information.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /info command to display current configuration.
    """
    user = update.effective_user
    user_id = user.id
    logger.debug(f"/info called by user {user_id}")

    if user_id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        # Fetch all groups and their deletion settings
        c.execute('''
            SELECT g.group_id, g.group_name, ds.enabled
            FROM groups g
            LEFT JOIN deletion_settings ds ON g.group_id = ds.group_id
        ''')
        groups = c.fetchall()

        # Fetch all bypassed users
        c.execute('''
            SELECT user_id FROM bypass_users
        ''')
        bypass_users = c.fetchall()

        conn.close()

        msg = "*Bot Information:*\n\n"
        msg += "*Registered Groups:*\n"
        if groups:
            for g_id, g_name, enabled in groups:
                g_name_display = g_name if g_name else "Name not set"
                deletion_status = "Enabled" if enabled else "Disabled"
                msg += f"• *Group Name:* {escape_markdown(g_name_display, version=2)}\n"
                msg += f"  *Group ID:* `{g_id}`\n"
                msg += f"  *Deletion:* `{deletion_status}`\n\n"
        else:
            msg += "⚠️ No groups have been added.\n\n"

        msg += "*Bypassed Users:*\n"
        if bypass_users:
            for (b_id,) in bypass_users:
                msg += f"• *User ID:* `{b_id}`\n"
        else:
            msg += "⚠️ No users are being bypassed.\n"

        try:
            # Telegram message length limit is 4096 characters
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
            logger.info("Displayed bot information.")
        except Exception as e:
            logger.error(f"Error sending /info information: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the information.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error handling /info command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve information. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

# ------------------- New Commands: /add_removed_user & /list_removed_users -------------------

async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /add_removed_user command to add a user to the 'Removed Users' list for a specific group.
    Usage: /add_removed_user <group_id> <user_id>
    """
    user = update.effective_user
    logger.debug(f"/add_removed_user called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    if len(context.args) != 2:
        message = escape_markdown("⚠️ Usage: `/add_removed_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /add_removed_user by user {user.id}")
        return

    try:
        group_id = int(context.args[0])
        target_user_id = int(context.args[1])
        logger.debug(f"Parsed group_id: {group_id}, user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ Both `group_id` and `user_id` must be integers.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Invalid group_id or user_id provided to /add_removed_user by user {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ Group `{group_id}` is not registered. Please add it using `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Attempted to add removed user to unregistered group {group_id} by user {user.id}")
        return

    # Check if user is already in removed_users list for the group
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        if c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ User `{target_user_id}` is already in the 'Removed Users' list for group `{group_id}`.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to add already removed user {target_user_id} to group {group_id} by user {user.id}")
            return
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ Failed to check 'Removed Users' list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error checking 'Removed Users' for group {group_id}: {e}")
        return

    try:
        # Add user to 'Removed Users' list
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO removed_users (group_id, user_id, removal_reason)
            VALUES (?, ?, ?)
        ''', (group_id, target_user_id, "Manually added via /add_removed_user"))
        conn.commit()
        conn.close()
        confirmation_message = escape_markdown(
            f"✅ Added user `{target_user_id}` to the 'Removed Users' list for group `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Added user {target_user_id} to 'Removed Users' for group {group_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add user to 'Removed Users' list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error adding user {target_user_id} to 'Removed Users' for group {group_id}: {e}")

async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /list_removed_users command to display all users in the 'Removed Users' list for each group.
    Usage: /list_removed_users
    """
    user = update.effective_user
    logger.debug(f"/list_removed_users called by user {user.id}")
    
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    try:
        removed_users = list_removed_users()
        if not removed_users:
            message = escape_markdown("⚠️ The 'Removed Users' list is empty.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.info("Displayed empty 'Removed Users' list.")
            return

        # Organize removed users by group
        groups = {}
        for record in removed_users:
            if len(record) == 3:
                # If group_id is not included
                group_id, user_id, removal_reason, removal_time = record[0], record[1], record[2], record[3]
            else:
                group_id, user_id, removal_reason, removal_time = record
            if group_id not in groups:
                groups[group_id] = []
            groups[group_id].append((user_id, removal_reason, removal_time))

        msg = "*Removed Users:*\n\n"
        for group_id, users in groups.items():
            msg += f"*Group ID:* `{group_id}`\n"
            for user_id, reason, time in users:
                msg += f"• *User ID:* `{user_id}`\n"
                msg += f"  *Reason:* {escape_markdown(reason, version=2)}\n"
                msg += f"  *Removed At:* {time}\n"
            msg += "\n"

        try:
            # Telegram message length limit is 4096 characters
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
            logger.info("Displayed 'Removed Users' list.")
        except Exception as e:
            logger.error(f"Error sending 'Removed Users' list: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the list.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error handling /list_removed_users command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve 'Removed Users' list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )

# ------------------- New /list_rmoved_rmove Command -------------------

async def list_rmoved_rmove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /list_rmoved_rmove command to request removal of a user from the 'Removed Users' list for a specific group.
    Usage: /list_rmoved_rmove <group_id>
    """
    user = update.effective_user
    logger.debug(f"/list_rmoved_rmove called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by user {user.id} for /list_rmoved_rmove command.")
        return  # Only respond to authorized user

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/list_rmoved_rmove <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /list_rmoved_rmove by user {user.id}")
        return

    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /list_rmoved_rmove by user {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ Group `{group_id}` is not registered. Please add it using `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Attempted to use /list_rmoved_rmove for unregistered group {group_id} by user {user.id}")
        return

    # Set pending removal for the user
    pending_user_removals[user.id] = group_id
    logger.info(f"Set pending user removal from group {group_id} by user {user.id}")

    # Prompt the user to send the user_id to remove
    try:
        prompt_message = escape_markdown(
            f"Please send the `user_id` of the user you wish to remove from the 'Removed Users' list for group `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=prompt_message,
            parse_mode='MarkdownV2'
        )
        logger.debug(f"Sent removal prompt for group {group_id} to user {user.id}")
    except Exception as e:
        logger.error(f"Error sending removal prompt for group {group_id}: {e}")

# ------------------- Existing /rmove_user Command -------------------

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /rmove_user command to remove a user from a group without sending notifications.
    Usage: /rmove_user <group_id> <user_id>
    """
    user = update.effective_user
    logger.debug(f"/rmove_user called by user {user.id} with args: {context.args}")

    # Check if the user is authorized
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    # Check for correct number of arguments
    if len(context.args) != 2:
        message = escape_markdown("⚠️ Usage: `/rmove_user <group_id> <user_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /rmove_user by user {user.id}")
        return

    # Parse group_id and user_id
    try:
        group_id = int(context.args[0])
        target_user_id = int(context.args[1])
        logger.debug(f"Parsed group_id: {group_id}, user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ Both `group_id` and `user_id` must be integers.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Invalid group_id or user_id provided to /rmove_user by user {user.id}")
        return

    # Remove user from bypass list
    try:
        if remove_bypass_user(target_user_id):
            logger.info(f"Removed user {target_user_id} from bypass list by user {user.id}")
        else:
            logger.info(f"User {target_user_id} was not in bypass list.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to update bypass list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing user {target_user_id} from bypass list: {e}")
        return

    # Add user to removed_users list
    try:
        add_user_to_removed_users(group_id, target_user_id)
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add user to 'Removed Users' list. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error adding user {target_user_id} to 'Removed Users' for group {group_id}: {e}")
        return

    # Revoke user permissions
    try:
        revoke_user_permissions(target_user_id)
        logger.info(f"Revoked permissions for user {target_user_id} in permissions system.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to revoke user permissions. Please check the permissions system.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error revoking permissions for user {target_user_id}: {e}")
        return

    # Attempt to remove the user from the group
    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=target_user_id)
        logger.info(f"Removed user {target_user_id} from group {group_id} via bot.")
    except Exception as e:
        message = escape_markdown(f"⚠️ Failed to remove user `{target_user_id}` from group `{group_id}`. Ensure the bot has the necessary permissions.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing user {target_user_id} from group {group_id}: {e}")
        return

    # Set flag to delete any messages in the group during MESSAGE_DELETE_TIMEFRAME seconds
    delete_all_messages_after_removal[group_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    logger.info(f"Set message deletion flag for group {group_id} for {MESSAGE_DELETE_TIMEFRAME} seconds.")

    # Schedule removal of the flag after MESSAGE_DELETE_TIMEFRAME seconds
    asyncio.create_task(remove_deletion_flag_after_timeout(group_id))

    # Send confirmation to the authorized user privately
    confirmation_message = escape_markdown(
        f"✅ Removed user `{target_user_id}` from group `{group_id}` and added to 'Removed Users' list.\nAny messages sent to the group within the next {MESSAGE_DELETE_TIMEFRAME} seconds will be deleted.",
        version=2
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Sent confirmation to user {user.id} about removing user {target_user_id} from group {group_id} and adding to 'Removed Users'.")
    except Exception as e:
        logger.error(f"Error sending confirmation message for /rmove_user: {e}")

# ------------------- Message Handler Functions -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete messages containing Arabic text in groups where deletion is enabled.
    """
    message = update.message
    if not message or not message.text:
        logger.debug("Received a non-text or empty message.")
        return  # Ignore non-text messages or empty messages

    user = message.from_user
    chat = message.chat
    group_id = chat.id

    logger.debug(f"Checking message in group {group_id} from user {user.id}: {message.text}")

    # Check if deletion is enabled for this group
    if not is_deletion_enabled(group_id):
        logger.debug(f"Deletion not enabled for group {group_id}.")
        return

    # Check if the user is bypassed
    if is_bypass_user(user.id):
        logger.debug(f"User {user.id} is bypassed. Message will not be deleted.")
        return

    # Check if the message contains Arabic
    if is_arabic(message.text):
        try:
            await message.delete()
            logger.info(f"Deleted Arabic message from user {user.id} in group {group_id}.")
            # Warning message removed to only delete the message without notifying the user
        except Exception as e:
            logger.error(f"Error deleting message in group {group_id}: {e}")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete any message sent to the group if the deletion flag is active.
    This includes messages from users and system messages.
    """
    message = update.message
    if not message:
        return

    chat = message.chat
    group_id = chat.id

    # Check if the group is flagged for message deletion
    if group_id in delete_all_messages_after_removal:
        try:
            await message.delete()
            logger.info(f"Deleted message in group {group_id}: {message.text or 'Non-text message.'}")
        except Exception as e:
            logger.error(f"Failed to delete message in group {group_id}: {e}")

# ------------------- Utility Function -------------------

def is_arabic(text):
    """
    Check if the text contains any Arabic characters.
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle errors that occur during updates.
    """
    logger.error("An error occurred:", exc_info=context.error)

# ------------------- Additional Utility Function -------------------

async def remove_deletion_flag_after_timeout(group_id):
    """
    Remove the deletion flag for a group after a specified timeout.
    """
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"Removed message deletion flag for group {group_id} after timeout.")

# ------------------- Be Sad and Be Happy Commands -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /be_sad command to enable message deletion in a group.
    Usage: /be_sad <group_id>
    """
    user = update.effective_user
    args = context.args
    logger.debug(f"/be_sad called by user {user.id} with args: {args}")

    # Check if the user is authorized
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    if len(args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_sad <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_sad by user {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /be_sad by user {user.id}")
        return

    # Enable deletion
    try:
        enable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ Failed to enable message deletion. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # Confirm to the user
    confirmation_message = escape_markdown(
        f"✅ Message deletion enabled for group `{group_id}`.",
        version=2
    )
    await context.bot.send_message(
        chat_id=user.id,
        text=confirmation_message,
        parse_mode='MarkdownV2'
    )
    logger.info(f"User {user.id} enabled message deletion for group {group_id}.")

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /be_happy command to disable message deletion in a group.
    Usage: /be_happy <group_id>
    """
    user = update.effective_user
    args = context.args
    logger.debug(f"/be_happy called by user {user.id} with args: {args}")

    # Check if the user is authorized
    if user.id != ALLOWED_USER_ID:
        return  # Only respond to authorized user

    if len(args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_happy <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_happy by user {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /be_happy by user {user.id}")
        return

    # Disable deletion
    try:
        disable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ Failed to disable message deletion. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # Confirm to the user
    confirmation_message = escape_markdown(
        f"✅ Message deletion disabled for group `{group_id}`.",
        version=2
    )
    await context.bot.send_message(
        chat_id=user.id,
        text=confirmation_message,
        parse_mode='MarkdownV2'
    )
    logger.info(f"User {user.id} disabled message deletion for group {group_id}.")

# ------------------- Check Command -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /check command to verify the 'Removed Users' list for a specific group.
    Usage: /check <group_id>
    """
    user = update.effective_user
    logger.debug(f"/check command called by user {user.id} with args: {context.args}")

    # Verify that the command is used by the authorized user
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by user {user.id} for /check command.")
        return  # Do not respond to unauthorized users

    # Check if the correct number of arguments is provided
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/check <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /check by user {user.id}. Provided args: {context.args}")
        return

    # Parse the group_id
    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /check by user {user.id}: {context.args[0]}")
        return

    # Check if the group exists in the database
    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ Group `{group_id}` is not registered. Please add it using `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Attempted to check unregistered group {group_id} by user {user.id}")
        return

    # Fetch removed users from the database for the specified group
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM removed_users WHERE group_id = ?', (group_id,))
        removed_users = [row[0] for row in c.fetchall()]
        conn.close()
        logger.debug(f"Fetched removed users for group {group_id}: {removed_users}")
    except Exception as e:
        logger.error(f"Error fetching removed users for group {group_id}: {e}")
        message = escape_markdown("⚠️ Failed to retrieve removed users from the database.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    if not removed_users:
        message = escape_markdown(f"⚠️ No removed users found for group `{group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"No removed users to check for group {group_id} by user {user.id}")
        return

    # Initialize lists to track user statuses
    users_still_in_group = []
    users_not_in_group = []

    # Check each user's membership status in the group
    for user_id in removed_users:
        try:
            member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
            status = member.status
            if status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                users_still_in_group.append(user_id)
                logger.debug(f"User {user_id} is still a member of group {group_id}. Status: {status}")
            else:
                users_not_in_group.append(user_id)
                logger.debug(f"User {user_id} is not a member of group {group_id}. Status: {status}")
        except Exception as e:
            # If the bot cannot fetch the member's status, assume the user is not in the group
            users_not_in_group.append(user_id)
            logger.error(f"Error fetching chat member status for user {user_id} in group {group_id}: {e}")

    # Prepare the report message
    msg = f"*Check Results for Group `{group_id}`:*\n\n"

    if users_still_in_group:
        msg += "*Users still in the group:* \n"
        for uid in users_still_in_group:
            msg += f"• `{uid}`\n"
        msg += "\n"
    else:
        msg += "*All removed users are not present in the group.*\n\n"

    if users_not_in_group:
        msg += "*Users not in the group:* \n"
        for uid in users_not_in_group:
            msg += f"• `{uid}`\n"
        msg += "\n"

    # Send the report to the authorized user
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=escape_markdown(msg, version=2),
            parse_mode='MarkdownV2'
        )
        logger.info(f"Check completed for group {group_id} by user {user.id}")
    except Exception as e:
        logger.error(f"Error sending check results to user {user.id}: {e}")
        message = escape_markdown("⚠️ An error occurred while sending the check results.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        return

    # Optionally, automatically remove users who are still in the group
    if users_still_in_group:
        for uid in users_still_in_group:
            try:
                await context.bot.ban_chat_member(chat_id=group_id, user_id=uid)
                logger.info(f"User {uid} has been removed from group {group_id} via /check command.")
            except Exception as e:
                logger.error(f"Failed to remove user {uid} from group {group_id}: {e}")

# ------------------- Handle Pending Removal -------------------

async def handle_pending_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the user's response to remove a user from the 'Removed Users' list.
    Expected to receive a user_id in the message.
    """
    user = update.effective_user
    message_text = update.message.text.strip()
    logger.debug(f"handle_pending_removal called by user {user.id} with message: {message_text}")
    
    if user.id not in pending_user_removals:
        # No pending removal
        warning_message = escape_markdown("⚠️ No pending removal found. Please use the appropriate command.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=warning_message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"User {user.id} attempted to remove a user without pending removal.")
        return
    
    group_id = pending_user_removals.pop(user.id)
    
    try:
        target_user_id = int(message_text)
    except ValueError:
        message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer user_id provided to handle_pending_removal by user {user.id}: {message_text}")
        return
    
    # Check if the user is in 'Removed Users' list for the group
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        if not c.fetchone():
            conn.close()
            message = escape_markdown(f"⚠️ User `{target_user_id}` is not in the 'Removed Users' list for group `{group_id}`.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"User {target_user_id} not in 'Removed Users' for group {group_id} during removal by user {user.id}")
            return
        # Proceed to remove
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, target_user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        message = escape_markdown("⚠️ Failed to remove user from 'Removed Users'. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing user {target_user_id} from 'Removed Users' for group {group_id}: {e}")
        return
    
    # Optionally, revoke permissions if necessary
    try:
        revoke_user_permissions(target_user_id)
    except Exception as e:
        logger.error(f"Error revoking permissions for user {target_user_id}: {e}")
        # Not critical to send message; user is removed from 'Removed Users' list
        # So we can proceed
    
    confirmation_message = escape_markdown(
        f"✅ User `{target_user_id}` has been removed from the 'Removed Users' list for group `{group_id}`.",
        version=2
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Removed user {target_user_id} from 'Removed Users' for group {group_id} by user {user.id}")
    except Exception as e:
        logger.error(f"Error sending confirmation message for handle_pending_removal: {e}")

# ------------------- Main Function -------------------

def main():
    """
    Main function to initialize the bot and register handlers.
    """
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Bot cannot start due to database initialization failure: {e}")
        sys.exit(f"Bot cannot start due to database initialization failure: {e}")

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("⚠️ BOT_TOKEN is not set.")
        sys.exit("⚠️ BOT_TOKEN is not set.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[len('bot='):].strip()
        logger.warning("BOT_TOKEN should not include 'bot=' prefix. Stripping it.")

    try:
        application = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"Failed to build the application with the provided TOKEN: {e}")
        sys.exit(f"Failed to build the application with the provided TOKEN: {e}")

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("group_add", group_add_cmd))
    application.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    application.add_handler(CommandHandler("bypass", bypass_cmd))
    application.add_handler(CommandHandler("unbypass", unbypass_cmd))
    application.add_handler(CommandHandler("group_id", group_id_cmd))
    application.add_handler(CommandHandler("show", show_groups_cmd))
    application.add_handler(CommandHandler("info", info_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("list", show_groups_cmd))  # Assuming /list is similar to /show
    application.add_handler(CommandHandler("be_sad", be_sad_cmd))
    application.add_handler(CommandHandler("be_happy", be_happy_cmd))
    application.add_handler(CommandHandler("rmove_user", rmove_user_cmd))  # Existing Command
    application.add_handler(CommandHandler("add_removed_user", add_removed_user_cmd))  # New Command
    application.add_handler(CommandHandler("list_removed_users", list_removed_users_cmd))  # New Command
    application.add_handler(CommandHandler("list_rmoved_rmove", list_rmoved_rmove_cmd))  # New Command
    application.add_handler(CommandHandler("check", check_cmd))  # Ensure only one /check handler

    # Register message handler for private messages
    # This single handler will manage both group name assignments and user removals
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message
    ))

    # Register message handlers for group chats
    # 1. Handle deleting Arabic messages
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_arabic_messages
    ))

    # 2. Handle any messages to delete during the deletion flag
    application.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))

    # Register error handler
    application.add_error_handler(error_handler)

    logger.info("🚀 Bot starting...")
    try:
        application.run_polling()
    except Exception as e:
        logger.critical(f"Bot encountered a critical error and is shutting down: {e}")
        sys.exit(f"Bot encountered a critical error and is shutting down: {e}")

if __name__ == '__main__':
    main()
