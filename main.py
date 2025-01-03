# main.py

import os
import sys
import logging
import sqlite3
import re
import fcntl
import asyncio
from datetime import datetime, timedelta

from telegram import Update, ChatMember
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import escape_markdown

# -------------- Additional Libraries for OCR and PDF --------------
import pytesseract
from PIL import Image
import fitz  # PyMuPDF

# If Tesseract is not in your PATH, specify its location:
# pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

# ------------------- Configuration -------------------

DATABASE = 'warnings.db'
ALLOWED_USER_ID = 6177929931  # Replace with your own ID
LOCK_FILE = '/tmp/telegram_bot.lock'

# If you remove a user from group, all messages in the next N seconds are deleted
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging Configuration -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Use DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# ------------------- Lock Mechanism -------------------

def acquire_lock():
    """
    Acquire a file-based lock to ensure a single bot instance.
    """
    try:
        lockfile = open(LOCK_FILE, 'w')
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("Lock acquired. Starting bot.")
        return lockfile
    except IOError:
        logger.error("Another instance is running. Exiting.")
        sys.exit(1)

def release_lock(lockfile):
    """
    Release the file lock on exit.
    """
    try:
        fcntl.flock(lockfile, fcntl.LOCK_UN)
        lockfile.close()
        os.remove(LOCK_FILE)
        logger.info("Lock released. Bot stopped.")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")

lockfile = acquire_lock()

import atexit
atexit.register(release_lock, lockfile)

# ------------------- Database Setup -------------------

def init_db():
    """
    Initialize tables if they don't exist.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("PRAGMA foreign_keys = 1")
        c = conn.cursor()

        # Minimal tables for demonstration
        c.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT
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
            CREATE TABLE IF NOT EXISTS bypass_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        # For demonstration, a table for removed_users if you want to manage that:
        c.execute('''
            CREATE TABLE IF NOT EXISTS removed_users (
                group_id INTEGER,
                user_id INTEGER,
                removal_reason TEXT,
                removal_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, user_id)
            )
        ''')
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"DB initialization failed: {e}")
        sys.exit(1)

def is_deletion_enabled(group_id: int) -> bool:
    """
    Return True if Arabic deletion is enabled for group.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT enabled FROM deletion_settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"Check deletion failed: {e}")
        return False

def enable_deletion(group_id: int, enable=True):
    """
    Enable or disable message deletion for a group.
    """
    val = 1 if enable else 0
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            INSERT INTO deletion_settings (group_id, enabled)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET enabled = ?
        ''', (group_id, val, val))
        conn.commit()
        conn.close()
        logger.info(f"Set deletion_enabled={val} for group {group_id}")
    except Exception as e:
        logger.error(f"Error enabling/disabling deletion: {e}")
        raise

def is_bypass_user(user_id: int) -> bool:
    """
    Return True if user is bypassed from Arabic deletion.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT 1 FROM bypass_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.error(f"is_bypass_user check failed: {e}")
        return False

def add_bypass_user(user_id: int):
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO bypass_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"Added user {user_id} to bypass list.")
    except Exception as e:
        logger.error(f"Add bypass user failed: {e}")
        raise

def remove_bypass_user(user_id: int) -> bool:
    """
    Remove from bypass. Return True if user was present.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM bypass_users WHERE user_id = ?", (user_id,))
        changes = c.rowcount
        conn.commit()
        conn.close()
        return changes > 0
    except Exception as e:
        logger.error(f"Remove bypass user failed: {e}")
        return False

# For demonstration, removed_users logic:
def remove_user_from_removed_users(group_id: int, user_id: int) -> bool:
    """
    Remove user from removed_users table. Return True if found.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM removed_users WHERE group_id=? AND user_id=?", (group_id, user_id))
        changes = c.rowcount
        conn.commit()
        conn.close()
        return changes > 0
    except Exception as e:
        logger.error(f"remove_user_from_removed_users failed: {e}")
        return False

# ------------------- Arabic Detection Helpers -------------------

def has_arabic(text: str) -> bool:
    """
    True if any Arabic character is in text.
    """
    return bool(re.search(r'[\u0600-\u06FF]', text))

def ocr_image(filepath: str) -> str:
    """
    OCR an image file using pytesseract.
    """
    try:
        img = Image.open(filepath)
        txt = pytesseract.image_to_string(img)
        img.close()
        return txt
    except Exception as e:
        logger.error(f"OCR error on {filepath}: {e}")
        return ""

def extract_text_from_pdf(filepath: str) -> str:
    """
    Extract text from all pages of a PDF using PyMuPDF.
    """
    try:
        doc = fitz.open(filepath)
        text_list = []
        for page in doc:
            text_list.append(page.get_text())
        doc.close()
        return "\n".join(text_list)
    except Exception as e:
        logger.error(f"PDF text extraction error {filepath}: {e}")
        return ""

# ------------------- Deletion Flag After Removal -------------------

delete_all_messages_after_removal = {}

async def remove_deletion_flag_after_timeout(group_id: int):
    """
    Remove the deletion flag after MESSAGE_DELETE_TIMEFRAME seconds.
    """
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    delete_all_messages_after_removal.pop(group_id, None)
    logger.info(f"Deletion flag removed for group {group_id}")

# ------------------- Main Handler to Detect Arabic in ANY Message -------------------

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Checks text, images, or PDFs for Arabic. If found => delete message.
    Also checks if group is flagged to delete everything after removal.
    """
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else None

    # 1) If group is flagged to delete ANY message (after rmove_user)
    if chat_id in delete_all_messages_after_removal:
        await try_delete_message(msg)
        return

    # 2) Check if deletion of Arabic is enabled in DB
    if not is_deletion_enabled(chat_id):
        return

    # 3) Check if user is bypassed
    if user_id and is_bypass_user(user_id):
        return  # Skip deletion

    # 4) Now let's see if there's Arabic in text, images, or PDFs
    found_arabic = False

    # A) Plain text
    if msg.text:
        if has_arabic(msg.text):
            found_arabic = True

    # B) Photos
    if not found_arabic and msg.photo:
        largest = msg.photo[-1]  # Typically the largest version is last
        photo_file = await largest.get_file()
        local_photo = f"/tmp/photo_{chat_id}_{msg.message_id}.jpg"
        await photo_file.download_to_drive(local_photo)
        ocr_result = ocr_image(local_photo)
        os.remove(local_photo)
        if has_arabic(ocr_result):
            found_arabic = True

    # C) Documents (check if it's a PDF)
    if not found_arabic and msg.document:
        doc = msg.document
        if doc.mime_type == "application/pdf":
            local_pdf = f"/tmp/pdf_{chat_id}_{msg.message_id}.pdf"
            doc_file = await doc.get_file()
            await doc_file.download_to_drive(local_pdf)
            pdf_text = extract_text_from_pdf(local_pdf)
            os.remove(local_pdf)
            if has_arabic(pdf_text):
                found_arabic = True

    # If Arabic is found, delete
    if found_arabic:
        await try_delete_message(msg)

async def try_delete_message(msg):
    try:
        await msg.delete()
        logger.info(f"Deleted message {msg.message_id} containing Arabic content.")
    except Exception as e:
        logger.error(f"Failed to delete message {msg.message_id}: {e}")

# ------------------- Basic Commands -------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    await context.bot.send_message(user.id, "Bot is up and running.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    text = """*Available Commands*:
• `/start` – Check if bot is running
• `/be_sad <group_id>` – Enable Arabic detection/deletion
• `/be_happy <group_id>` – Disable Arabic detection
• `/bypass <user_id>` – Add user to bypass
• `/unbypass <user_id>` – Remove user from bypass
• `/rmove_user <group_id> <user_id>` – Example remove user from group
• More commands can be added as needed.
"""
    await context.bot.send_message(user.id, escape_markdown(text, version=2), parse_mode='MarkdownV2')

async def be_sad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_sad <group_id> => enable Arabic detection
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 1:
        await context.bot.send_message(user.id, "Usage: /be_sad <group_id>")
        return
    try:
        g_id = int(context.args[0])
        enable_deletion(g_id, True)
        await context.bot.send_message(user.id, f"Arabic detection enabled for group {g_id}.")
    except ValueError:
        await context.bot.send_message(user.id, "group_id must be an integer.")
    except Exception as e:
        logger.error(f"Error enabling: {e}")
        await context.bot.send_message(user.id, "Failed to enable be_sad.")

async def be_happy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /be_happy <group_id> => disable Arabic detection
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    if len(context.args) != 1:
        await context.bot.send_message(user.id, "Usage: /be_happy <group_id>")
        return
    try:
        g_id = int(context.args[0])
        enable_deletion(g_id, False)
        await context.bot.send_message(user.id, f"Arabic detection disabled for group {g_id}.")
    except ValueError:
        await context.bot.send_message(user.id, "group_id must be an integer.")
    except Exception as e:
        logger.error(f"Error disabling: {e}")
        await context.bot.send_message(user.id, "Failed to disable be_happy.")

# ------------------- Bypass Commands -------------------

async def bypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        await context.bot.send_message(user.id, "Usage: /bypass <user_id>")
        return
    try:
        bypass_uid = int(context.args[0])
        add_bypass_user(bypass_uid)
        await context.bot.send_message(user.id, f"User {bypass_uid} bypassed.")
    except Exception as e:
        logger.error(f"bypass_cmd: {e}")
        await context.bot.send_message(user.id, "Failed to bypass user.")

async def unbypass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 1:
        await context.bot.send_message(user.id, "Usage: /unbypass <user_id>")
        return
    try:
        ub_uid = int(context.args[0])
        res = remove_bypass_user(ub_uid)
        if res:
            await context.bot.send_message(user.id, f"User {ub_uid} unbypassed.")
        else:
            await context.bot.send_message(user.id, f"User {ub_uid} was not bypassed.")
    except Exception as e:
        logger.error(f"unbypass_cmd: {e}")
        await context.bot.send_message(user.id, "Failed to unbypass user.")

# ------------------- Example "Remove User" Logic -------------------

async def rmove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rmove_user <group_id> <user_id>
    1) attempt to ban user from group
    2) set a deletion flag for next MESSAGE_DELETE_TIMEFRAME
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    if len(context.args) != 2:
        await context.bot.send_message(user.id, "Usage: /rmove_user <group_id> <user_id>")
        return

    try:
        g_id = int(context.args[0])
        target_uid = int(context.args[1])
    except ValueError:
        await context.bot.send_message(user.id, "Both group_id and user_id must be integers.")
        return

    # Possibly remove from bypass
    remove_bypass_user(target_uid)
    # Possibly remove from "removed_users"
    remove_user_from_removed_users(g_id, target_uid)

    # Ban from group
    try:
        await context.bot.ban_chat_member(chat_id=g_id, user_id=target_uid)
        logger.info(f"Banned user {target_uid} from group {g_id}.")
    except Exception as e:
        logger.error(f"Failed to ban user {target_uid}: {e}")
        await context.bot.send_message(
            user.id,
            f"Failed to ban user {target_uid} from group {g_id}. Check bot permissions."
        )
        return

    # Flag the group to delete all messages for next N seconds
    delete_all_messages_after_removal[g_id] = datetime.utcnow() + timedelta(seconds=MESSAGE_DELETE_TIMEFRAME)
    asyncio.create_task(remove_deletion_flag_after_timeout(g_id))

    await context.bot.send_message(
        user.id,
        f"Removed user {target_uid} from group {g_id}. Any messages in next {MESSAGE_DELETE_TIMEFRAME}s will be deleted."
    )

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Bot error:", exc_info=context.error)

# ------------------- main() -------------------

def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN not set.")
        sys.exit(1)

    app = ApplicationBuilder().token(token.strip()).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("be_sad", be_sad_cmd))
    app.add_handler(CommandHandler("be_happy", be_happy_cmd))
    app.add_handler(CommandHandler("bypass", bypass_cmd))
    app.add_handler(CommandHandler("unbypass", unbypass_cmd))
    app.add_handler(CommandHandler("rmove_user", rmove_user_cmd))
    # ...Add more commands if desired (like /unremove_user)...

    # Main message handler (text, photos, PDFs)
    # We use filters.ALL & group chats => handle_incoming_message
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_incoming_message
    ))

    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
