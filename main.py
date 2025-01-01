# main.py

import os
import sys
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
import re

from telegram import Update
from telegram.constants import ChatType, ChatMemberStatus
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
ALLOWED_USER_ID = 6177929931  # Replace with your actual authorized user ID

# Timeframe (in seconds) to delete messages after user removal
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more verbose output
)
logger = logging.getLogger(__name__)

# ------------------- Database Initialization -------------------

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

        # Create permissions table
        c.execute('''
            CREATE TABLE IF NOT EXISTS permissions (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize the database: {e}")
        sys.exit(1)

# ------------------- Helper Functions -------------------

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
        logger.info(f"Added group {group_id} to database.")
    except Exception as e:
        logger.error(f"Error adding group {group_id}: {e}")
        raise

def set_group_name(group_id, group_name):
    """
    Set the name of a group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, group_id))
        conn.commit()
        conn.close()
        logger.info(f"Set group name for {group_id}: {group_name}")
    except Exception as e:
        logger.error(f"Error setting group name for {group_id}: {e}")
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

# ------------------- Pending Actions -------------------

# Dictionary to keep track of pending group names
pending_group_names = {}

# Dictionary to keep track of pending user removals
# Format: {user_id: group_id}
pending_user_removals = {}

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
        removed = remove_user_from_removed_users(group_id, target_user_id)
        if not removed:
            message = escape_markdown(f"⚠️ User `{target_user_id}` is not in the 'Removed Users' list for group `{group_id}`.", version=2)
            await context.bot.send_message(
                chat_id=user.id,
                text=message,
                parse_mode='MarkdownV2'
            )
            logger.warning(f"User {target_user_id} not found in 'Removed Users' for group {group_id} during removal by user {user.id}")
            return
        
        # Revoke user permissions
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

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /be_sad command to enable Arabic message deletion for a specific group.
    Usage: /be_sad <group_id>
    """
    user = update.effective_user
    logger.debug(f"/be_sad called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {user.id} attempted to use /be_sad.")
        return  # Only respond to authorized user

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_sad <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_sad by user {user.id}")
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
        logger.warning(f"Non-integer group_id provided to /be_sad by user {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ Group `{group_id}` is not registered. Please add it using `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Attempted to enable deletion for unregistered group {group_id} by user {user.id}")
        return

    try:
        enable_deletion(group_id)
        confirmation_message = escape_markdown(
            f"✅ Enabled Arabic message deletion for group `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Enabled deletion for group {group_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to enable message deletion. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error enabling deletion for group {group_id} by user {user.id}: {e}")

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle the /be_happy command to disable Arabic message deletion for a specific group.
    Usage: /be_happy <group_id>
    """
    user = update.effective_user
    logger.debug(f"/be_happy called by user {user.id} with args: {context.args}")
    
    if user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized user {user.id} attempted to use /be_happy.")
        return  # Only respond to authorized user

    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/be_happy <group_id>`", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Incorrect usage of /be_happy by user {user.id}")
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
        logger.warning(f"Non-integer group_id provided to /be_happy by user {user.id}")
        return

    if not group_exists(group_id):
        message = escape_markdown(f"⚠️ Group `{group_id}` is not registered. Please add it using `/group_add {group_id}`.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.warning(f"Attempted to disable deletion for unregistered group {group_id} by user {user.id}")
        return

    try:
        disable_deletion(group_id)
        confirmation_message = escape_markdown(
            f"✅ Disabled Arabic message deletion for group `{group_id}`.",
            version=2
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Disabled deletion for group {group_id} by user {user.id}")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to disable message deletion. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error disabling deletion for group {group_id} by user {user.id}: {e}")

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

    # Remove user from removed_users table
    try:
        removed = remove_user_from_removed_users(group_id, target_user_id)
        if removed:
            logger.info(f"Removed user {target_user_id} from 'Removed Users' in permissions for group {group_id} by user {user.id}")
        else:
            logger.warning(f"User {target_user_id} was not in 'Removed Users' for group {group_id}.")
    except Exception as e:
        message = escape_markdown("⚠️ Failed to update permissions system. Please try again later.", version=2)
        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode='MarkdownV2'
        )
        logger.error(f"Error removing user {target_user_id} from 'Removed Users' in permissions for group {group_id}: {e}")
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
        f"✅ Removed user `{target_user_id}` from group `{group_id}` and from 'Removed Users' in permissions.\nAny messages sent to the group within the next {MESSAGE_DELETE_TIMEFRAME} seconds will be deleted.",
        version=2
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=confirmation_message,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Sent confirmation to user {user.id} about removing user {target_user_id} from group {group_id} and permissions.")
    except Exception as e:
        logger.error(f"Error sending confirmation message for /rmove_user: {e}")
