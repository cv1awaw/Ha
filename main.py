# main.py

import os
import sys
import sqlite3
import logging
import html
import fcntl
from datetime import datetime
import re
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ------------------- Configuration -------------------

# Define the path to the SQLite database
DATABASE = 'warnings.db'

# Define the allowed user ID
ALLOWED_USER_ID = 6177929931  # Replace with the actual authorized user ID

# Define the lock file path
LOCK_FILE = '/tmp/telegram_bot.lock'  # Change path as needed

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more verbose output
)
logger = logging.getLogger(__name__)

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

def init_db():
    """
    Initialize the SQLite database and create necessary tables if they don't exist.
    Removed tables related to TARAs and warnings.
    """
    try:
        conn = sqlite3.connect(DATABASE)
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
        logger.info(f"Set name for group {g_id}: {group_name}")
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
        logger.debug(f"Checked existence of group {group_id}: {exists}")
        return exists
    except Exception as e:
        logger.error(f"Error checking group existence for {group_id}: {e}")
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
        logger.debug(f"Checked if user {user_id} is bypassed: {res}")
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
    Returns True if a record was deleted, False otherwise.
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
        logger.debug(f"Deletion enabled for group {group_id}: {enabled}")
        return bool(enabled)
    except Exception as e:
        logger.error(f"Error checking deletion status for group {group_id}: {e}")
        return False

# ------------------- Command Handler Functions -------------------

async def handle_private_message_for_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle private messages sent by the authorized user to set group names.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    message = update.message
    user = message.from_user
    logger.debug(f"Received private message from user {user.id}: {message.text}")
    if user.id == ALLOWED_USER_ID and user.id in pending_group_names:
        g_id = pending_group_names.pop(user.id)
        group_name = message.text.strip()
        if group_name:
            try:
                escaped_group_name = escape_markdown(group_name, version=2)
                set_group_name(g_id, group_name)
                confirmation_message = escape_markdown(
                    f"✅ Group name for `{g_id}` set to: *{escaped_group_name}*",
                    version=2
                )
                await message.reply_text(
                    confirmation_message,
                    parse_mode='MarkdownV2'
                )
                logger.info(f"Group name for {g_id} set to {group_name} by user {user.id}")
            except Exception as e:
                error_message = escape_markdown("⚠️ Failed to set group name. Please try `/group_add` again.", version=2)
                await message.reply_text(
                    error_message,
                    parse_mode='MarkdownV2'
                )
                logger.error(f"Error setting group name for {g_id} by user {user.id}: {e}")
        else:
            warning_message = escape_markdown("⚠️ Group name cannot be empty. Please try `/group_add` again.", version=2)
            await message.reply_text(
                warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Empty group name received from user {user.id} for group {g_id}")
    else:
        warning_message = escape_markdown("⚠️ No pending group to set name for.", version=2)
        await message.reply_text(
            warning_message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Received group name from user {user.id} with no pending group.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /start command.
    """
    try:
        user = update.effective_user
        if user.id != ALLOWED_USER_ID:
            return  # Ignore messages from unauthorized users
        message = escape_markdown("✅ Bot is running and ready.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"/start called by user {user.id}")
    except Exception as e:
        logger.error(f"Error handling /start command: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /group_add command to register a group.
    Usage: /group_add <group_id>
    """
    user = update.effective_user
    logger.debug(f"/group_add command called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/group_add <group_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /group_add by user {user.id}")
        return
    
    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /group_add by user {user.id}")
        return
    
    if group_exists(group_id):
        message = escape_markdown("⚠️ Group already added.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.debug(f"Group {group_id} is already registered.")
        return
    
    try:
        add_group(group_id)
        logger.debug(f"Added group {group_id} to database.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add group. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Failed to add group {group_id} by user {user.id}: {e}")
        return
    
    pending_group_names[user.id] = group_id
    logger.info(f"Group {group_id} added, awaiting name from user {user.id} in private chat.")
    
    try:
        confirmation_message = escape_markdown(
            f"✅ Group `{group_id}` added.\nPlease send the group name in a private message to the bot.",
            version=2
        )
        await update.message.reply_text(
            confirmation_message,
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error sending confirmation for /group_add command: {e}")

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /rmove_group command to remove a registered group.
    Usage: /rmove_group <group_id>
    """
    user = update.effective_user
    logger.debug(f"/rmove_group command called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/rmove_group <group_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /rmove_group by user {user.id}")
        return
    try:
        group_id = int(context.args[0])
        logger.debug(f"Parsed group_id: {group_id}")
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
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
                f"✅ Removed group `{group_id}` from registration.",
                version=2
            )
            await update.message.reply_text(
                confirm_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Removed group {group_id} by user {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ Group `{group_id}` not found.",
                version=2
            )
            await update.message.reply_text(
                warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to remove non-existent group {group_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to remove group. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing group {group_id} by user {user.id}: {e}")

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /bypass command to add a user to bypass warnings.
    Usage: /bypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"/bypass command called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/bypass <user_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /bypass by user {user.id}")
        return
    try:
        target_user_id = int(context.args[0])
        logger.debug(f"Parsed target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer user_id provided to /bypass by user {user.id}")
        return
    try:
        add_bypass_user(target_user_id)
        logger.debug(f"Added bypass user {target_user_id} to database.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to add bypass user. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error adding bypass user {target_user_id} by user {user.id}: {e}")
        return
    try:
        confirmation_message = escape_markdown(
            f"✅ User `{target_user_id}` has been added to bypass warnings.",
            version=2
        )
        await update.message.reply_text(
            confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Added user {target_user_id} to bypass list by user {user.id}")
    except Exception as e:
        logger.error(f"Error sending reply for /bypass command: {e}")

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /unbypass command to remove a user from bypass warnings.
    Usage: /unbypass <user_id>
    """
    user = update.effective_user
    logger.debug(f"/unbypass command called by user {user.id} with args: {context.args}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/unbypass <user_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /unbypass by user {user.id}")
        return
    try:
        target_user_id = int(context.args[0])
        logger.debug(f"Parsed target_user_id: {target_user_id}")
    except ValueError:
        message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer user_id provided to /unbypass by user {user.id}")
        return
    try:
        if remove_bypass_user(target_user_id):
            confirmation_message = escape_markdown(
                f"✅ User `{target_user_id}` has been removed from bypass warnings.",
                version=2
            )
            await update.message.reply_text(
                confirmation_message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Removed user {target_user_id} from bypass list by user {user.id}")
        else:
            warning_message = escape_markdown(
                f"⚠️ User `{target_user_id}` was not in the bypass list.",
                version=2
            )
            await update.message.reply_text(
                warning_message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"Attempted to remove non-existent bypass user {target_user_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to remove bypass user. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing bypass user {target_user_id} by user {user.id}: {e}")

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /show command to display all groups and their settings.
    """
    user = update.effective_user
    logger.debug(f"/show command called by user {user.id}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT group_id, group_name FROM groups')
        groups_data = c.fetchall()
        conn.close()

        if not groups_data:
            message = escape_markdown("⚠️ No groups added.", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
            logger.debug("No groups found in the database.")
            return

        msg = "*Groups Information:*\n\n"
        for g_id, g_name in groups_data:
            g_name_display = g_name if g_name else "No Name Set"
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
                msg += "⚠️ Error retrieving deletion status.\n"
                logger.error(f"Error retrieving deletion status for group {g_id}: {e}")

            # Fetch bypassed users
            try:
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute('''
                    SELECT u.user_id, u.first_name, u.last_name, u.username
                    FROM users u
                    WHERE u.user_id IN (
                        SELECT user_id FROM bypass_users
                    )
                ''')
                bypass_users = c.fetchall()
                conn.close()
                if bypass_users:
                    msg += "*Bypassed Users:*\n"
                    for b_id, b_first, b_last, b_username in bypass_users:
                        full_name = f"{b_first or ''} {b_last or ''}".strip() or "N/A"
                        username_display = f"@{b_username}" if b_username else "NoUsername"
                        full_name_esc = escape_markdown(full_name, version=2)
                        username_esc = escape_markdown(username_display, version=2)
                        msg += f"• *User ID:* `{b_id}`\n"
                        msg += f"  *Full Name:* {full_name_esc}\n"
                        msg += f"  *Username:* {username_esc}\n"
                else:
                    msg += "*Bypassed Users:* None.\n"
            except Exception as e:
                msg += "*Bypassed Users:* Error retrieving bypassed users.\n"
                logger.error(f"Error retrieving bypassed users: {e}")

            msg += "\n"

        try:
            # Telegram has a message length limit (4096 characters)
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await update.message.reply_text(
                        chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await update.message.reply_text(
                    msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("Displayed comprehensive bot overview.")
        except Exception as e:
            logger.error(f"Error sending /show information: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the list information.", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error processing /show command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve list information. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /group_id command to retrieve the group ID.
    Only the authorized user can use this command in private chat.
    """
    user = update.effective_user
    group = update.effective_chat
    user_id = user.id
    logger.debug(f"/group_id command called by user {user_id} in chat {group.id}")
    
    if user_id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    
    try:
        if group.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            group_id = group.id
            message = escape_markdown(f"🔢 *Group ID:* `{group_id}`", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Sent Group ID {group_id} to user {user_id}")
        else:
            # If in private chat
            message = escape_markdown(f"🔢 *Your User ID:* `{user_id}`", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
            logger.info(f"Sent User ID {user_id} to user in private chat")
    except Exception as e:
        logger.error(f"Error handling /group_id command: {e}")
        message = escape_markdown("⚠️ An error occurred while processing the command.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /help command to display available commands.
    """
    user = update.effective_user
    logger.debug(f"/help command called by user {user.id}, ALLOWED_USER_ID={ALLOWED_USER_ID}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    help_text = """*Available Commands:*
• `/start` - Check if bot is running
• `/group_add <group_id>` - Register a group (use the exact chat_id of the group)
• `/rmove_group <group_id>` - Remove a registered group
• `/bypass <user_id>` - Add a user to bypass warnings
• `/unbypass <user_id>` - Remove a user from bypass warnings
• `/group_id` - Retrieve the current group or your user ID
• `/show` - Show all groups and their deletion status
• `/info` - Show current bot configuration
• `/help` - Show this help
• `/list` - Comprehensive overview of groups and bypassed users
• `/be_sad <group_id>` - Enable automatic deletion of Arabic messages in a group
• `/be_happy <group_id>` - Disable automatic deletion of Arabic messages in a group
"""
    try:
        # Escape special characters for MarkdownV2
        help_text_esc = escape_markdown(help_text, version=2)
        await update.message.reply_text(
            help_text_esc,
            parse_mode='MarkdownV2'
        )
        logger.info("Displayed help information to user.")
    except Exception as e:
        logger.error(f"Error sending help information: {e}")
        message = escape_markdown("⚠️ An error occurred while sending the help information.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /info command to show current configuration.
    """
    user = update.effective_user
    user_id = user.id
    logger.debug(f"/info command called by user {user_id}")

    if user_id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat

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
                g_name_display = g_name if g_name else "No Name Set"
                deletion_status = "Enabled" if enabled else "Disabled"
                msg += f"• *Group Name:* {escape_markdown(g_name_display, version=2)}\n"
                msg += f"  *Group ID:* `{g_id}`\n"
                msg += f"  *Deletion:* `{deletion_status}`\n\n"
        else:
            msg += "⚠️ No groups registered.\n\n"

        msg += "*Bypassed Users:*\n"
        if bypass_users:
            for (b_id,) in bypass_users:
                msg += f"• *User ID:* `{b_id}`\n"
        else:
            msg += "⚠️ No users have bypassed message deletion.\n"

        try:
            # Telegram has a message length limit (4096 characters)
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await update.message.reply_text(
                        chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await update.message.reply_text(
                    msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("Displayed bot information.")
        except Exception as e:
            logger.error(f"Error sending /info information: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the information.", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error processing /info command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve information. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

# ------------------- Deletion Control Commands -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /be_sad command to enable message deletion in a group.
    Usage: /be_sad <group_id>
    """
    user = update.effective_user
    args = context.args
    logger.debug(f"/be_sad called by user {user.id} with args: {args}")

    # Check if the user is authorized
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat

    if len(args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_sad <group_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_sad by user {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /be_sad by user {user.id}")
        return

    # Enable deletion
    try:
        enable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ Failed to enable message deletion. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        return

    # Confirm to the user
    confirmation_message = escape_markdown(
        f"✅ Message deletion enabled for group `{group_id}`.",
        version=2
    )
    await update.message.reply_text(
        confirmation_message,
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
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat

    if len(args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_happy <group_id>`", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_happy by user {user.id}")
        return

    try:
        group_id = int(args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Non-integer group_id provided to /be_happy by user {user.id}")
        return

    # Disable deletion
    try:
        disable_deletion(group_id)
    except Exception:
        message = escape_markdown("⚠️ Failed to disable message deletion. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )
        return

    # Confirm to the user
    confirmation_message = escape_markdown(
        f"✅ Message deletion disabled for group `{group_id}`.",
        version=2
    )
    await update.message.reply_text(
        confirmation_message,
        parse_mode='MarkdownV2'
    )
    logger.info(f"User {user.id} disabled message deletion for group {group_id}.")

# ------------------- Message Handler Function -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete messages containing Arabic text in groups where deletion is enabled.
    """
    message = update.message
    if not message or not message.text:
        logger.debug("Received a non-text or empty message.")
        return  # Ignore non-text messages

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
            # Optionally, send a warning to the user
            warning_message = escape_markdown(
                "⚠️ Arabic messages are not allowed in this group.",
                version=2
            )
            await message.reply_text(
                warning_message,
                parse_mode='MarkdownV2'
            )
            logger.debug(f"Sent warning to user {user.id} for Arabic message in group {group_id}.")
        except Exception as e:
            logger.error(f"Error deleting message in group {group_id}: {e}")

# ------------------- Utility Function -------------------

def is_arabic(text):
    """
    Check if the text contains any Arabic characters.
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

# ------------------- Information Commands -------------------

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /info command to show current configuration.
    """
    user = update.effective_user
    user_id = user.id
    logger.debug(f"/info command called by user {user_id}")

    if user_id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat

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
                g_name_display = g_name if g_name else "No Name Set"
                deletion_status = "Enabled" if enabled else "Disabled"
                msg += f"• *Group Name:* {escape_markdown(g_name_display, version=2)}\n"
                msg += f"  *Group ID:* `{g_id}`\n"
                msg += f"  *Deletion:* `{deletion_status}`\n\n"
        else:
            msg += "⚠️ No groups registered.\n\n"

        msg += "*Bypassed Users:*\n"
        if bypass_users:
            for (b_id,) in bypass_users:
                msg += f"• *User ID:* `{b_id}`\n"
        else:
            msg += "⚠️ No users have bypassed message deletion.\n"

        try:
            # Telegram has a message length limit (4096 characters)
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    chunk = msg[i:i+4000]
                    await update.message.reply_text(
                        chunk,
                        parse_mode='MarkdownV2'
                    )
            else:
                await update.message.reply_text(
                    msg,
                    parse_mode='MarkdownV2'
                )
            logger.info("Displayed bot information.")
        except Exception as e:
            logger.error(f"Error sending /info information: {e}")
            message = escape_markdown("⚠️ An error occurred while sending the information.", version=2)
            await update.message.reply_text(
                message,
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error processing /info command: {e}")
        message = escape_markdown("⚠️ Failed to retrieve information. Please try again later.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

# ------------------- Help Command -------------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /help command to display available commands.
    """
    user = update.effective_user
    logger.debug(f"/help command called by user {user.id}, ALLOWED_USER_ID={ALLOWED_USER_ID}")
    if user.id != ALLOWED_USER_ID or update.effective_chat.type != ChatType.PRIVATE:
        return  # Only respond to authorized user in private chat
    help_text = """*Available Commands:*
• `/start` - Check if bot is running
• `/group_add <group_id>` - Register a group (use the exact chat_id of the group)
• `/rmove_group <group_id>` - Remove a registered group
• `/bypass <user_id>` - Add a user to bypass warnings
• `/unbypass <user_id>` - Remove a user from bypass warnings
• `/group_id` - Retrieve the current group or your user ID
• `/show` - Show all groups and their deletion status
• `/info` - Show current bot configuration
• `/help` - Show this help
• `/list` - Comprehensive overview of groups and bypassed users
• `/be_sad <group_id>` - Enable automatic deletion of Arabic messages in a group
• `/be_happy <group_id>` - Disable automatic deletion of Arabic messages in a group
"""
    try:
        # Escape special characters for MarkdownV2
        help_text_esc = escape_markdown(help_text, version=2)
        await update.message.reply_text(
            help_text_esc,
            parse_mode='MarkdownV2'
        )
        logger.info("Displayed help information to user.")
    except Exception as e:
        logger.error(f"Error sending help information: {e}")
        message = escape_markdown("⚠️ An error occurred while sending the help information.", version=2)
        await update.message.reply_text(
            message,
            parse_mode='MarkdownV2'
        )

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle errors that occur during updates.
    """
    logger.error("An error occurred:", exc_info=context.error)

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

    # Handle private messages for setting group name
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message_for_group_name
    ))

    # Handle group messages for deleting Arabic messages
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_arabic_messages
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
