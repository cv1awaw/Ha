#!/usr/bin/env python3

import os
import sys
import sqlite3
import logging
import fcntl
from datetime import datetime, timedelta
import re
import asyncio
from telegram import (
    Update,
    ChatMember,
    ChatInviteLink,
)
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
ALLOWED_USER_ID = 6177929931  # <-- REPLACE with your actual Telegram user ID
LOCK_FILE = '/tmp/telegram_bot.lock'  # Adjust path if needed
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- "Awaiting" Dictionaries -------------------

# 1) For waiting on a new group name:
pending_group_names = {}         # { user_id: group_id }

# 2) For waiting on toggling user provisions:
awaiting_provision = {}          # { user_id: (group_id, user_id_being_provisioned) }

# 3) The dictionary that stores actual toggles:
provisions_status = {}           # { (group_id, target_user): {1: bool, 2: bool, ...} }

# 4) If the user toggles "Mute" (item #1) to ENABLED, we can wait for minutes:
awaiting_mute_time = {}          # { (group_id, user_id): True }

provision_labels = {
    1:  "Mute",
    2:  "Kick",
    3:  "Send Messages",
    4:  "Send Photos",
    5:  "Send Videos",
    6:  "Send Files",
    7:  "Send Music",
    8:  "Send Voice Messages",
    9:  "Send Video Messages",
    10: "Send Stickers",
    11: "Send GIFs",
    12: "Send Games",
    13: "Send Inline Bots",
    14: "Send Polls",
    15: "Embed Links",
    16: "Add Users",
    17: "Pin Messages",
    18: "Change Chat Info"
}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. Starting bot...")
        return lock
    except IOError:
        logger.error("Another instance of the bot is already running. Exiting.")
        sys.exit("Another instance of the bot is already running.")

def release_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

lock = acquire_lock()
import atexit
atexit.register(release_lock, lock)

# ------------------- Database Initialization -------------------

def init_permissions_db():
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS permissions (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL
            )
        ''')
        
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
        logger.info("Database initialized successfully.")
        
        init_permissions_db()
    except Exception as e:
        logger.error(f"Failed to initialize the database: {e}")
        raise

# ------------------- Database Helper Functions -------------------

def add_group(group_id):
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
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, g_id))
        conn.commit()
        conn.close()
        logger.info(f"Set group name for {g_id} to '{group_name}'")
    except Exception as e:
        logger.error(f"Error setting group name for {g_id}: {e}")
        raise

def group_exists(group_id):
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM groups WHERE group_id = ?', (group_id,))
        exists = (c.fetchone() is not None)
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Error checking existence of group {group_id}: {e}")
        return False

def is_bypass_user(user_id):
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM bypass_users WHERE user_id = ?', (user_id,))
        res = (c.fetchone() is not None)
        conn.close()
        return res
    except Exception as e:
        logger.error(f"Error checking bypass status for user {user_id}: {e}")
        return False

def add_bypass_user(user_id):
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
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('DELETE FROM removed_users WHERE group_id = ? AND user_id = ?', (group_id, user_id))
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
        logger.error(f"Error removing user {user_id} from group {group_id} in removed_users: {e}")
        return False

def revoke_user_permissions(user_id):
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
    Handle ANY message in private chat:
      - If user is waiting to set a group name, store that name
      - If user is waiting to provision toggles, parse numbers
      - If user toggles Mute=ON, then ask for minutes
    """
    user = update.effective_user
    message_text = (update.message.text or "").strip()
    logger.debug(f"[PRIVATE CHAT] user {user.id} said: {message_text}")

    # Only allow ALLOWED_USER_ID
    if user.id != ALLOWED_USER_ID:
        logger.debug(f"Ignoring private message from non-admin user {user.id}")
        return

    # --- 1) If user is about to provide a group name ---
    if user.id in pending_group_names:
        g_id = pending_group_names.pop(user.id)
        group_name = message_text
        if not group_name:
            warning_msg = escape_markdown(
                "⚠️ Group name cannot be empty. Please try `/group_add <group_id>` again.",
                version=2
            )
            await context.bot.send_message(chat_id=user.id, text=warning_msg, parse_mode='MarkdownV2')
            logger.warning(f"User {user.id} gave an empty group name for group {g_id}")
            return

        try:
            set_group_name(g_id, group_name)
            confirmation = escape_markdown(
                f"✅ Set group `{g_id}` name to: *{group_name}*",
                version=2
            )
            await context.bot.send_message(chat_id=user.id, text=confirmation, parse_mode='MarkdownV2')
            logger.info(f"Group name for {g_id} set to {group_name} by user {user.id}")
        except Exception as e:
            error_msg = escape_markdown(
                "⚠️ Failed to set group name. Please try `/group_add` again.",
                version=2
            )
            await context.bot.send_message(chat_id=user.id, text=error_msg, parse_mode='MarkdownV2')
            logger.error(f"Error setting group name for {g_id}: {e}")
        return

    # --- 2) If user is about to toggle provisions for someone ---
    if user.id in awaiting_provision:
        (grp, uid) = awaiting_provision[user.id]
        # Also check if we are waiting for "mute time" specifically:
        if (grp, uid) in awaiting_mute_time and awaiting_mute_time[(grp, uid)]:
            # The next message is presumably the # of minutes
            try:
                minutes = int(message_text)
                # You could do an actual "mute" call or record it somewhere
                logger.info(f"Muting user {uid} in group {grp} for {minutes} minutes (not implemented).")
                awaiting_mute_time.pop((grp, uid), None)
                txt = escape_markdown(f"✅ Mute set for {minutes} minutes (pretend).", version=2)
                await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
                return
            except ValueError:
                wr = escape_markdown("⚠️ Please enter a valid integer for minutes.", version=2)
                await context.bot.send_message(chat_id=user.id, text=wr, parse_mode='MarkdownV2')
                return

        # If the message is numeric toggles, e.g. "3", "1 2", etc.:
        if re.match(r'^[0-9 ]+$', message_text):
            nums = message_text.split()
            p_dict = provisions_status.get((grp, uid), {})
            toggled_list = []
            for n_str in nums:
                try:
                    n_int = int(n_str)
                except ValueError:
                    continue
                if n_int in p_dict:
                    new_val = not p_dict[n_int]
                    p_dict[n_int] = new_val
                    toggled_list.append(n_int)
                    # If it's Mute turned ON, ask for minutes next:
                    if (n_int == 1) and new_val:
                        awaiting_mute_time[(grp, uid)] = True
                        prompt = escape_markdown(
                            "Enter the *number of minutes* to mute:",
                            version=2
                        )
                        await context.bot.send_message(chat_id=user.id, text=prompt, parse_mode='MarkdownV2')

            # Summarize toggles
            if toggled_list:
                summary = "*Toggled:* \n"
                for x in toggled_list:
                    state = "ENABLED" if p_dict[x] else "DISABLED"
                    summary += f"• {provision_labels[x]} -> {state}\n"
                await context.bot.send_message(
                    chat_id=user.id,
                    text=escape_markdown(summary, version=2),
                    parse_mode='MarkdownV2'
                )
            else:
                await context.bot.send_message(
                    chat_id=user.id,
                    text="⚠️ No valid toggles found.",
                    parse_mode='MarkdownV2'
                )
        else:
            # Not numeric => prompt again
            err_msg = escape_markdown("⚠️ Please enter number(s), e.g. `1 2`", version=2)
            await context.bot.send_message(chat_id=user.id, text=err_msg, parse_mode='MarkdownV2')
        return

    # If no recognized state, do nothing
    logger.debug("No recognized pending action in private message.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    try:
        msg = escape_markdown("✅ Bot is running and ready.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /start: {e}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/group_add <group_id> -> add group in DB, then wait for user to send group name in private."""
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
        msg = escape_markdown(f"⚠️ Group `{g_id}` already registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        add_group(g_id)
        # store that we are waiting for the group name
        pending_group_names[user.id] = g_id
        confirmation = escape_markdown(
            f"✅ Group `{g_id}` added.\n"
            "Please send the group name **in a private message** to me now.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=confirmation, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error adding group {g_id}: {e}")
        msg = escape_markdown("⚠️ Failed to add group. Please try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                chunk = msg[i:i+4000]
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error showing groups: {e}")
        err_msg = escape_markdown("⚠️ Failed to get group list. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err_msg, parse_mode='MarkdownV2')

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    help_text = """*Commands:*
• `/start` – Check if the bot is running
• `/group_add <group_id>` – Register a group by its chat ID (then send the name in private)
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
• `/add_removed_user <group_id> <user_id>` – Add a user to 'Removed Users'
• `/list_removed_users` – Show all users in 'Removed Users'
• `/unremove_user <group_id> <user_id>` – Remove a user from 'Removed Users'
• `/check <group_id>` – Validate 'Removed Users' vs. actual membership
• `/provision <group_id> <user_id>` – Toggle user’s “permissions” or “actions” in private
• `/link <group_id>` – Create a single-use invite link
"""
    try:
        help_esc = escape_markdown(help_text, version=2)
        await context.bot.send_message(chat_id=user.id, text=help_esc, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /help: {e}")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        msg = "*Bot Info:*\n\n*Groups:*\n"
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
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /info: {e}")
        er = escape_markdown("⚠️ Failed to retrieve info. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=er, parse_mode='MarkdownV2')

# ------------------- Additional Commands -------------------

async def provision_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /provision <group_id> <user_id> -> show toggles, then wait in private chat 
    for numeric input to toggle them.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 2:
        txt = escape_markdown("⚠️ Usage: `/provision <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
        u_id = int(context.args[1])
    except ValueError:
        txt = escape_markdown("⚠️ Both <group_id> and <user_id> must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        warn = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    if (g_id, u_id) not in provisions_status:
        provisions_status[(g_id, u_id)] = {k: False for k in provision_labels}

    # Mark that we are now awaiting numeric toggles from this admin user
    awaiting_provision[user.id] = (g_id, u_id)

    p_dict = provisions_status[(g_id, u_id)]
    lines = [
        f"Provision list for user `{u_id}` in group `{g_id}`:",
        "Type the number(s) in *private chat* to toggle.\n"
    ]
    for num, label in provision_labels.items():
        state = "ENABLED" if p_dict[num] else "DISABLED"
        lines.append(f"{num}) {label} -> {state}")
    lines.append("\nExample: Type `1 2` in private chat to toggle Mute and Kick.")

    msg = escape_markdown("\n".join(lines), version=2)
    await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 1:
        txt = escape_markdown("⚠️ Usage: `/link <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
        return

    try:
        g_id = int(context.args[0])
    except ValueError:
        txt = escape_markdown("⚠️ <group_id> must be integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        warn = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=warn, parse_mode='MarkdownV2')
        return

    try:
        link: ChatInviteLink = await context.bot.create_chat_invite_link(
            chat_id=g_id,
            member_limit=1
        )
        txt = escape_markdown(f"Single-use link created:\n{link.invite_link}", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error creating invite link for group {g_id}: {e}")
        txt = escape_markdown("⚠️ Could not create invite link. Check if the bot is admin.", version=2)
        await context.bot.send_message(chat_id=user.id, text=txt, parse_mode='MarkdownV2')

# ------------------- Commands to manage removed_users -------------------

async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    try:
        removed_data = list_removed_users()
        if not removed_data:
            msg = escape_markdown("⚠️ 'Removed Users' is empty.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return

        grouped = {}
        for row in removed_data:
            # row might be (group_id, user_id, reason, time) or (user_id, reason, time)
            if len(row) == 4:
                (g_id, u_id, reason, tstamp) = row
            elif len(row) == 3:
                (u_id, reason, tstamp) = row
                g_id = -999999999  # unknown
            else:
                continue

            if g_id not in grouped:
                grouped[g_id] = []
            grouped[g_id].append((u_id, reason, tstamp))

        output = "*Removed Users:*\n\n"
        for gg_id, items in grouped.items():
            output += f"*Group:* `{gg_id}`\n"
            for (usr, reas, tm) in items:
                output += f"• *User:* `{usr}`\n"
                output += f"  *Reason:* {escape_markdown(reas, version=2)}\n"
                output += f"  *Removed At:* {tm}\n"
            output += "\n"

        if len(output) > 4000:
            for i in range(0, len(output), 4000):
                chunk = output[i:i+4000]
                await context.bot.send_message(chat_id=user.id, text=chunk, parse_mode='MarkdownV2')
        else:
            await context.bot.send_message(chat_id=user.id, text=output, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /list_removed_users: {e}")
        msg = escape_markdown("⚠️ Failed to list removed users.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def unremove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"⚠️ User `{u_id}` is not in 'Removed Users' for group `{g_id}`.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Error revoking perms for user {u_id}: {e}")

    cf = escape_markdown(f"✅ User `{u_id}` removed from 'Removed Users' for group `{g_id}`.", version=2)
    await context.bot.send_message(chat_id=user.id, text=cf, parse_mode='MarkdownV2')

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    remove_bypass_user(u_id)
    remove_user_from_removed_users(g_id, u_id)
    try:
        revoke_user_permissions(u_id)
    except Exception as e:
        logger.error(f"Failed to revoke perms for {u_id}: {e}")

    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=u_id)
    except Exception as e:
        err = escape_markdown(
            f"⚠️ Could not ban `{u_id}` from group `{g_id}` (check bot perms).", version=2
        )
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')
        logger.error(f"Ban error for user {u_id} in group {g_id}: {e}")
        return

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
    msg = update.message
    if not msg:
        return
    user = msg.from_user
    g_id = msg.chat.id
    if not is_deletion_enabled(g_id):
        return
    if is_bypass_user(user.id):
        return
    text_or_caption = msg.text or msg.caption
    if text_or_caption and re.search(r'[\u0600-\u06FF]', text_or_caption):
        try:
            await msg.delete()
            logger.info(f"Deleted Arabic message from user {user.id} in group {g_id}")
        except Exception as e:
            logger.error(f"Error deleting msg in group {g_id}: {e}")

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

async def remove_deletion_flag_after_timeout(group_id):
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"Deletion flag removed for group {group_id}")

# ------------------- Commands to toggle Arabic deletion -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            logger.error(f"Error getting chat member (group={g_id}, user={uid}): {e}")
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
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(resp, version=2), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error sending check results: {e}")
        msg = escape_markdown("⚠️ Error sending check results.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

    if still_in:
        for x in still_in:
            try:
                await context.bot.ban_chat_member(chat_id=g_id, user_id=x)
                logger.info(f"Auto-banned user {x} from group {g_id} via /check.")
            except Exception as e:
                logger.error(f"Failed to ban user {x} from group {g_id}: {e}")

# ------------------- main() -------------------

def main():
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

    # Additional commands
    app.add_handler(CommandHandler("provision", provision_cmd))
    app.add_handler(CommandHandler("link", link_cmd))

    # Message handlers:
    # 1) Delete Arabic text in groups (if enabled)
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, delete_arabic_messages))

    # 2) Delete all messages in a group if within the removal timeframe
    app.add_handler(
        MessageHandler(filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
                       delete_any_messages)
    )

    # 3) Capture *all* messages in private chat
    # This ensures we "wait" for the user's next message after /group_add or /provision:
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE, handle_private_message)
    )

    app.add_error_handler(error_handler)

    logger.info("Bot starting up...")
    try:
        app.run_polling()
    except Exception as e:
        logger.critical(f"Critical error, shutting down: {e}")
        sys.exit("Bot crashed.")


if __name__ == "__main__":
    main()
