#!/usr/bin/env python3

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

DATABASE = 'warnings.db'             # Path to the SQLite database
ALLOWED_USER_ID = 6177929931         # Replace with your actual authorized user ID!
LOCK_FILE = '/tmp/telegram_bot.lock' # Path to lock file
MESSAGE_DELETE_TIMEFRAME = 15        # Seconds to delete msgs after user removal

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG if needed
)
logger = logging.getLogger(__name__)

# ------------------- Pending Actions -------------------

# 1) For waiting on a new group name after /group_add
pending_group_names = {}           # {admin_user_id: group_id}

# 2) For waiting to remove user from removed_users (from /list_rmoved_rmove, etc.)
pending_user_removals = {}         # {admin_user_id: group_id}

# 3) For waiting to see which user’s toggles we are adjusting
awaiting_provision = {}            # {admin_user_id: (group_id, target_user_id)}

# 4) The dictionary that stores “permissions” toggles
provisions_status = {}             # { (group_id, target_user_id): {1: bool, 2: bool, ...} }

# Example labels for toggles
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

# Dictionary to track group IDs that should delete all messages for a timeframe
delete_all_messages_after_removal = {}  # {group_id: datetime_expiration}

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """Acquire a file lock so only one instance runs."""
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. Starting bot...")
        return lock
    except IOError:
        logger.error("Another instance of the bot is already running. Exiting.")
        sys.exit("Another instance of the bot is already running.")

def release_lock(lock):
    """Release the file lock on exit."""
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
    """Initialize the permissions and removed_users tables."""
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
    """Initialize the SQLite database and create necessary tables."""
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

# ------------------- DB Helper Functions -------------------

def add_group(group_id):
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)', (group_id, None))
        conn.commit()
        conn.close()
        logger.info(f"Added group {group_id} to DB with no name.")
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
        logger.info(f"Set group name for {g_id}: {group_name}")
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
            logger.info(f"Removed user {user_id} from removed_users list for group {group_id}.")
            return True
        else:
            logger.warning(f"User {user_id} not found in removed_users for group {group_id}.")
            return False
    except Exception as e:
        logger.error(f"Error removing user {user_id} from removed_users for group {group_id}: {e}")
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
        if group_id:
            c.execute('SELECT user_id, removal_reason, removal_time FROM removed_users WHERE group_id = ?', (group_id,))
        else:
            c.execute('SELECT group_id, user_id, removal_reason, removal_time FROM removed_users')
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Error fetching removed users: {e}")
        return []

# ------------------- Utility / Deletion Flag -------------------

async def remove_deletion_flag_after_timeout(group_id):
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"Removed message deletion flag for group {group_id} after timeout.")

# ------------------- Private Message Handler -------------------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle private messages:
      - If user in pending_group_names -> interpret as group name
      - If user in pending_user_removals -> interpret as user_id to remove from 'Removed Users'
      - If user in awaiting_provision -> interpret numeric toggles for provisioning
    """
    user = update.effective_user
    message_text = (update.message.text or "").strip()
    logger.debug(f"[PRIVATE] user {user.id} says: {message_text}")

    # Only handle if user is authorized
    if user.id != ALLOWED_USER_ID:
        return

    # 1) If user is about to provide a group name
    if user.id in pending_group_names:
        group_id = pending_group_names.pop(user.id)
        group_name = message_text
        if not group_name:
            warning = escape_markdown("⚠️ Group name cannot be empty. Please try `/group_add` again.", version=2)
            await context.bot.send_message(chat_id=user.id, text=warning, parse_mode='MarkdownV2')
            return
        try:
            set_group_name(group_id, group_name)
            confirmation = escape_markdown(f"✅ Set group `{group_id}` name to: *{group_name}*", version=2)
            await context.bot.send_message(chat_id=user.id, text=confirmation, parse_mode='MarkdownV2')
        except Exception as e:
            err = escape_markdown("⚠️ Failed to set group name. Please try again.", version=2)
            await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')
            logger.error(f"Error setting group name for {group_id}: {e}")
        return

    # 2) If user is about to remove someone from removed_users
    elif user.id in pending_user_removals:
        group_id = pending_user_removals.pop(user.id)
        try:
            target_user_id = int(message_text)
        except ValueError:
            message = escape_markdown("⚠️ `user_id` must be an integer.", version=2)
            await context.bot.send_message(chat_id=user.id, text=message, parse_mode='MarkdownV2')
            return
        removed = remove_user_from_removed_users(group_id, target_user_id)
        if not removed:
            msg = escape_markdown(
                f"⚠️ User `{target_user_id}` is not in 'Removed Users' for group `{group_id}`.",
                version=2
            )
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
            return
        # Revoke perms
        try:
            revoke_user_permissions(target_user_id)
        except Exception as e:
            logger.error(f"Error revoking perms for user {target_user_id}: {e}")
        conf = escape_markdown(
            f"✅ User `{target_user_id}` removed from 'Removed Users' for group `{group_id}`.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=conf, parse_mode='MarkdownV2')
        return

    # 3) If user is toggling provisions
    elif user.id in awaiting_provision:
        (g_id, t_uid) = awaiting_provision[user.id]
        # If the text is numeric toggles, e.g. "1 2"
        if re.match(r'^[0-9 ]+$', message_text):
            p_dict = provisions_status.get((g_id, t_uid), {})
            toggled_nums = []
            for n_str in message_text.split():
                try:
                    n_int = int(n_str)
                except ValueError:
                    continue
                if n_int in provision_labels:
                    new_val = not p_dict.get(n_int, False)
                    p_dict[n_int] = new_val
                    toggled_nums.append(n_int)
            provisions_status[(g_id, t_uid)] = p_dict

            if toggled_nums:
                result = "*Toggled:* \n"
                for x in toggled_nums:
                    state = "ENABLED" if p_dict[x] else "DISABLED"
                    label = provision_labels[x]
                    result += f"• {label} -> {state}\n"
                await context.bot.send_message(
                    chat_id=user.id,
                    text=escape_markdown(result, version=2),
                    parse_mode='MarkdownV2'
                )
            else:
                msg = escape_markdown("⚠️ No valid toggles found in your input.", version=2)
                await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        else:
            # Not numeric
            msg = escape_markdown("⚠️ Please send number(s), e.g. `1 2` to toggle Mute/Kick etc.", version=2)
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    # If none of the above, do nothing
    logger.debug("No recognized pending action in private message.")


# ------------------- Commands -------------------
# (preserving your existing commands while adding /provision)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /start command """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    msg = escape_markdown("✅ Bot is running and ready.", version=2)
    await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /group_add <group_id>: Register group, then wait in private chat for name. """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 1:
        message = escape_markdown("⚠️ Usage: `/group_add <group_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=message, parse_mode='MarkdownV2')
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        message = escape_markdown("⚠️ `group_id` must be an integer.", version=2)
        await context.bot.send_message(chat_id=user.id, text=message, parse_mode='MarkdownV2')
        return

    if group_exists(group_id):
        msg = escape_markdown("⚠️ Group already added.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    try:
        add_group(group_id)
        pending_group_names[user.id] = group_id
        conf = escape_markdown(
            f"✅ Group `{group_id}` added.\nPlease send the group name **in a private message** now.",
            version=2
        )
        await context.bot.send_message(chat_id=user.id, text=conf, parse_mode='MarkdownV2')
    except Exception as e:
        err = escape_markdown("⚠️ Failed to add group. Try again.", version=2)
        await context.bot.send_message(chat_id=user.id, text=err, parse_mode='MarkdownV2')
        logger.error(f"Error adding group {group_id}: {e}")

async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /rmove_group <group_id> : remove group from DB """
    # ... Unchanged ...
    # (Keeping your existing logic exactly, omitted here for brevity)
    # but you should keep the code you had in the old snippet for rmove_group_cmd.

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /bypass <user_id> : add user to bypass """
    # ... Keep your old logic ...

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /unbypass <user_id> : remove user from bypass """
    # ... Keep your old logic ...

async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /group_id : show group or user ID """
    # ... Keep your old logic ...

async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /show or /list : display all groups """
    # ... Keep your old logic ...

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /info : show config """
    # ... Keep your old logic ...

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /help : show help text """
    # ... Keep your old logic ...

# ------------------- The /provision Command -------------------

async def provision_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /provision <group_id> <user_id> – Show toggles in private chat, then wait for numeric input to toggle them.
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 2:
        usage = escape_markdown("⚠️ Usage: `/provision <group_id> <user_id>`", version=2)
        await context.bot.send_message(chat_id=user.id, text=usage, parse_mode='MarkdownV2')
        return
    try:
        g_id = int(context.args[0])
        tgt_uid = int(context.args[1])
    except ValueError:
        msg = escape_markdown("⚠️ Both <group_id> and <user_id> must be integers.", version=2)
        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')
        return

    if not group_exists(g_id):
        wrn = escape_markdown(f"⚠️ Group `{g_id}` is not registered.", version=2)
        await context.bot.send_message(chat_id=user.id, text=wrn, parse_mode='MarkdownV2')
        return

    # If not existing, create a fresh dict
    if (g_id, tgt_uid) not in provisions_status:
        provisions_status[(g_id, tgt_uid)] = {k: False for k in provision_labels}

    # Mark that we’re awaiting numeric toggles from this admin user
    awaiting_provision[user.id] = (g_id, tgt_uid)

    p_dict = provisions_status[(g_id, tgt_uid)]
    lines = [
        f"Provision list for user `{tgt_uid}` in group `{g_id}`:",
        "Type the number(s) in *private chat* to toggle.\n"
    ]
    for num, label in provision_labels.items():
        state = "ENABLED" if p_dict.get(num, False) else "DISABLED"
        lines.append(f"{num}) {label} -> {state}")
    lines.append("\nExample: Type `1 2` in private chat to toggle Mute and Kick.")

    msg = escape_markdown("\n".join(lines), version=2)
    await context.bot.send_message(chat_id=user.id, text=msg, parse_mode='MarkdownV2')

# ------------------- The /link Command (if you had it) -------------------
# etc.

# ------------------- New Commands: add_removed_user_cmd, list_removed_users_cmd, etc. -------------------
# (We keep your existing logic from the old code.)

async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

async def list_rmoved_rmove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

# ------------------- "Be Sad"/"Be Happy" to toggle Arabic deletion -------------------

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

# ------------------- The /check Command -------------------

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

# ------------------- Message Handler Functions -------------------

def is_arabic(text):
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... Keep your old logic ...
    pass

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("An error occurred:", exc_info=context.error)

# ------------------- main() -------------------

def main():
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Bot cannot start due to DB init failure: {e}")
        sys.exit("DB init failure.")

    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN not set.")
        sys.exit("BOT_TOKEN not set.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[len('bot='):].strip()
        logger.warning("Stripped 'bot=' from BOT_TOKEN.")

    try:
        application = ApplicationBuilder().token(TOKEN).build()
    except Exception as e:
        logger.critical(f"Failed to build application: {e}")
        sys.exit(f"Failed to build application: {e}")

    # Register your commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("group_add", group_add_cmd))
    application.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    application.add_handler(CommandHandler("bypass", bypass_cmd))
    application.add_handler(CommandHandler("unbypass", unbypass_cmd))
    application.add_handler(CommandHandler("group_id", group_id_cmd))
    application.add_handler(CommandHandler("show", show_groups_cmd))
    application.add_handler(CommandHandler("list", show_groups_cmd))
    application.add_handler(CommandHandler("info", info_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("be_sad", be_sad_cmd))
    application.add_handler(CommandHandler("be_happy", be_happy_cmd))
    application.add_handler(CommandHandler("rmove_user", rmove_user_cmd))
    application.add_handler(CommandHandler("add_removed_user", add_removed_user_cmd))
    application.add_handler(CommandHandler("list_removed_users", list_removed_users_cmd))
    application.add_handler(CommandHandler("list_rmoved_rmove", list_rmoved_rmove_cmd))
    application.add_handler(CommandHandler("check", check_cmd))

    # Our new /provision command
    application.add_handler(CommandHandler("provision", provision_cmd))

    # If you have a /link command, add it here

    # Private messages: capture all text (not commands) for name or toggles
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                       handle_private_message)
    )

    # For Arabic text deletion
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP|filters.ChatType.SUPERGROUP),
                       delete_arabic_messages)
    )

    # For deleting any messages if flagged
    application.add_handler(
        MessageHandler(filters.ALL & (filters.ChatType.GROUP|filters.ChatType.SUPERGROUP),
                       delete_any_messages)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot starting up...")
    application.run_polling()


if __name__ == '__main__':
    main()
