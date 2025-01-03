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
    ChatPermissions,  # needed to restrict/unrestrict chat
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

# ------------------- CONFIG -------------------

DATABASE = 'warnings.db'
ALLOWED_USER_ID = 123456789  # <-- Replace with your own ID!
LOCK_FILE = '/tmp/telegram_bot.lock'
MESSAGE_DELETE_TIMEFRAME = 15

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# A dictionary to store which admin user is toggling which (group_id, target_user_id)
awaiting_provision = {}  # { admin_user_id: (group_id, target_user_id) }

# A dictionary that stores the toggles (on/off) for each (group_id, user_id)
# e.g. {(group_id, user_id): {1: True, 2: False, 3: True, ...}}
provisions_status = {}

# A minimal example of toggles. Adjust to your needs:
# 1) Mute (disables can_send_messages)
# 2) Send Media (disables can_send_media_messages)
# 3) Send Stickers/GIFs (disables can_send_other_messages)
# 4) Send Polls (disables can_send_polls)
provision_labels = {
    1: "Mute",
    2: "Send Media",
    3: "Send Stickers/GIFs",
    4: "Send Polls",
}

# For group name creation (like in your old code)
pending_group_names = {}  # { admin_user_id: group_id }
delete_all_messages_after_removal = {}

# ------------------- LOCK Mechanism -------------------

import atexit

def acquire_lock():
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. Starting bot.")
        return lock
    except IOError:
        logger.error("Another instance is running. Exiting.")
        sys.exit("Another instance is running.")

def release_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

lock = acquire_lock()
atexit.register(release_lock, lock)

# ------------------- DB initialization (Minimal Example) -------------------

def init_db():
    # If you have more tables, etc., put them here
    pass

# A placeholder for set_group_name (like your old code)
def set_group_name(group_id, group_name):
    logger.info(f"[FAKE DB] set_group_name group={group_id} name={group_name}")
    # If you have a real DB, do so here

def group_exists(group_id):
    # For testing, assume all group IDs exist or just return True
    return True

# ------------------- Permissions Logic -------------------

def build_chat_permissions(toggles: dict) -> ChatPermissions:
    """
    Convert your local toggles (True/False) into a ChatPermissions object.
    
    toggles is something like: {1: True, 2: False, 3: True, 4: False}
    which correspond to [Mute, Send Media, ...].
    We'll map them to ChatPermissions fields.
    """
    # Start by granting everything, then remove as needed
    # In reality, if 'Mute' is ON, that means can_send_messages = False
    # If 'Send Media' is OFF, that means can_send_media_messages = False, etc.

    # For simpler logic, assume everything is allowed by default
    can_send_messages = True
    can_send_media_messages = True
    can_send_other_messages = True
    can_send_polls = True
    can_add_web_page_previews = True

    # 1) Mute
    if toggles.get(1, False) is True:
        # If "Mute" is "ENABLED" => user is muted => can_send_messages = False
        can_send_messages = False

    # 2) Send Media
    if toggles.get(2, False) is False:
        # If "Send Media" is DISABLED => can_send_media_messages = False
        # (assuming toggles "ENABLED" means we allow it; so if toggles[x] is True => user is allowed)
        # but you might invert the logic if you prefer
        can_send_media_messages = False

    # 3) Send Stickers/GIFs
    if toggles.get(3, False) is False:
        # If "Send Stickers/GIFs" is disabled => can_send_other_messages = False
        can_send_other_messages = False

    # 4) Send Polls
    if toggles.get(4, False) is False:
        # If "Send Polls" is disabled => can_send_polls = False
        can_send_polls = False

    return ChatPermissions(
        can_send_messages=can_send_messages,
        can_send_media_messages=can_send_media_messages,
        can_send_other_messages=can_send_other_messages,
        can_send_polls=can_send_polls,
        can_add_web_page_previews=can_add_web_page_previews
    )

# ------------------- Private Message Handler -------------------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    text = (update.message.text or "").strip()
    logger.info(f"Private message from {user.id}: {text}")

    # 1) If user is waiting to set group name
    if user.id in pending_group_names:
        group_id = pending_group_names.pop(user.id)
        if not text:
            await update.message.reply_text("⚠️ Group name cannot be empty. Please retry.")
            return
        set_group_name(group_id, text)
        await update.message.reply_text(f"✅ Set group {group_id} name to: {text}")
        return

    # 2) If we are toggling user provisions
    if user.id in awaiting_provision:
        (g_id, tgt_uid) = awaiting_provision[user.id]
        # Expect numeric toggles
        if re.match(r'^[0-9 ]+$', text):
            # parse toggles
            toggles = provisions_status.get((g_id, tgt_uid), {})
            changed = []
            for n_str in text.split():
                n_int = int(n_str)
                if n_int in provision_labels:
                    prev = toggles.get(n_int, False)
                    new_val = not prev
                    toggles[n_int] = new_val
                    changed.append(n_int)
            provisions_status[(g_id, tgt_uid)] = toggles
            # Summarize
            summary = "*Toggled:*\n"
            for c in changed:
                st = "ENABLED" if toggles[c] else "DISABLED"
                summary += f"• {provision_labels[c]} -> {st}\n"
            await update.message.reply_text(summary, parse_mode="MarkdownV2")

            # Now we actually call restrictChatMember to apply
            try:
                new_permissions = build_chat_permissions(toggles)
                await context.bot.restrict_chat_member(
                    chat_id=g_id,
                    user_id=tgt_uid,
                    permissions=new_permissions
                )
                logger.info(f"Updated real chat permissions for user {tgt_uid} in group {g_id}")
            except Exception as e:
                logger.error(f"Failed to restrict user {tgt_uid} in {g_id}: {e}")
                await update.message.reply_text(f"⚠️ Could not apply real permissions. Error: {e}")

        else:
            await update.message.reply_text("⚠️ Please type numbers, e.g. `1 2`.")
        return

    # If none matched
    logger.info("No recognized action in private chat.")

# ------------------- Commands -------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("✅ Bot is running and ready.")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Usage: /group_add <group_id>")
        return
    try:
        g_id = int(context.args[0])
    except:
        await update.message.reply_text("⚠️ group_id must be int")
        return

    # pretend to check if group exists
    if group_exists(g_id):
        # store in pending
        pending_group_names[user.id] = g_id
        await update.message.reply_text(f"✅ Group {g_id} added. Please send the group name in private.")
    else:
        await update.message.reply_text("⚠️ That group does not exist (for demonstration).")

async def provision_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Usage: /provision <group_id> <user_id>")
        return
    try:
        g_id = int(context.args[0])
        tgt_uid = int(context.args[1])
    except:
        await update.message.reply_text("⚠️ Both args must be int.")
        return

    # create or get toggles
    toggles = provisions_status.get((g_id, tgt_uid), {})
    # if empty, init
    for num in provision_labels:
        toggles.setdefault(num, False)
    provisions_status[(g_id, tgt_uid)] = toggles

    # Mark that we are awaiting numeric toggles from this admin
    awaiting_provision[user.id] = (g_id, tgt_uid)

    # show the user
    lines = [
        f"Provision list for user {tgt_uid} in group {g_id}:",
        "Type the number(s) in private chat to toggle.\n"
    ]
    for k, lbl in provision_labels.items():
        st = "ENABLED" if toggles[k] else "DISABLED"
        lines.append(f"{k}) {lbl} -> {st}")
    lines.append("\nExample: `1 2` to toggle Mute & Send Media.")
    msg = "\n".join(lines)
    await update.message.reply_text(msg)

# A minimal /help
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    text = """*Commands*:
`/start` - Check the bot
`/group_add <group_id>` - Register a group & then in private message you send the name
`/provision <group_id> <user_id>` - Toggle user perms
"""
    await update.message.reply_text(escape_markdown(text, version=2), parse_mode="MarkdownV2")

# Example placeholders for other commands:
async def rmove_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def show_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def group_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def add_removed_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def list_removed_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def list_rmoved_rmove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass


# ------------------- Deletion Logic (optional) -------------------

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

# ------------------- Main -------------------

def main():
    init_db()  # if you have any DB migrations

    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        logger.error("BOT_TOKEN not set.")
        sys.exit("BOT_TOKEN not set.")
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith("bot="):
        TOKEN = TOKEN[len("bot="):].strip()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("group_add", group_add_cmd))
    app.add_handler(CommandHandler("provision", provision_cmd))
    # your other commands here...
    app.add_handler(CommandHandler("rmove_group", rmove_group_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("show", show_groups_cmd))
    app.add_handler(CommandHandler("group_id", group_id_cmd))
    app.add_handler(CommandHandler("be_sad", be_sad_cmd))
    app.add_handler(CommandHandler("be_happy", be_happy_cmd))
    app.add_handler(CommandHandler("rmove_user", rmove_user_cmd))
    app.add_handler(CommandHandler("add_removed_user", add_removed_user_cmd))
    app.add_handler(CommandHandler("list_removed_users", list_removed_users_cmd))
    app.add_handler(CommandHandler("list_rmoved_rmove", list_rmoved_rmove_cmd))
    app.add_handler(CommandHandler("check", check_cmd))

    # Private chat text -> handle toggles or group name
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private_message
        )
    )

    # If you want to delete Arabic, etc.
    app.add_handler(
        MessageHandler(filters.TEXT & (filters.ChatType.GROUP|filters.ChatType.SUPERGROUP), delete_arabic_messages)
    )

    # If you want to delete messages after removal:
    app.add_handler(
        MessageHandler(filters.ALL & (filters.ChatType.GROUP|filters.ChatType.SUPERGROUP), delete_any_messages)
    )

    app.add_error_handler(error_handler)

    logger.info("Bot starting up...")
    app.run_polling()

if __name__ == "__main__":
    main()
