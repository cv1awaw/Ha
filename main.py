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

DATABASE = 'warnings.db'
ALLOWED_USER_ID = 6177929931  # Replace with your personal Telegram user ID
LOCK_FILE = '/tmp/telegram_bot.lock'
MESSAGE_DELETE_TIMEFRAME = 15

# ------------------- Logging -------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- Pending group name requests -------------------
# This dict holds user_id -> group_id if that user just did `/group_add` and we’re waiting for the group name.
pending_group_names = {}

# ------------------- File lock to prevent duplicates -------------------

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

lock_file = acquire_lock()
import atexit
atexit.register(release_lock, lock_file)

# ------------------- DB Initialization -------------------

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
        conn.execute("PRAGMA foreign_keys = 1")  
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

# ------------------- DB Helper Functions -------------------

def add_group(group_id):
    """
    Add a group by its chat ID to 'groups' if not present.
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
    Set (or update) the name of a group in the DB.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('UPDATE groups SET group_name = ? WHERE group_id = ?', (group_name, group_id))
        conn.commit()
        conn.close()
        logger.info(f"Group {group_id} name set to '{group_name}' in DB.")
    except Exception as e:
        logger.error(f"Error setting group name for {group_id}: {e}")
        raise

def group_exists(group_id):
    """
    Check if a group is in the 'groups' table.
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
    Check if a user is in the 'bypass_users' table.
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
    Add a user to 'bypass_users'.
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
    Remove a user from 'bypass_users'. Return True if removed, else False.
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
    Enable Arabic message deletion for a group.
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
    Disable Arabic message deletion for a group.
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
    Check if Arabic deletion is enabled for the given group.
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

# (The rest of DB helper functions for removed_users we won’t show here in full to save space, 
# but keep them as in your previous code – add_removed_user, list_removed_users, etc.)

# ------------------- Commands -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - readiness check
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return

    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown("✅ Bot is running and ready.", version=2),
        parse_mode='MarkdownV2'
    )
    logger.info(f"/start used by {user.id}")

async def group_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /group_add <group_id> – register a group, then wait for a non-command message with the name.
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

    # check if group is already known
    if group_exists(g_id):
        msg = "⚠️ This group is already registered."
        await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')
        return

    # insert group
    add_group(g_id)
    # store in pending
    pending_group_names[user.id] = g_id

    msg = f"✅ Group `{g_id}` added.\nNow send me the group name in **any** message (not a command)."
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(msg, version=2), parse_mode='MarkdownV2')

async def handle_any_text_for_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If ALLOWED_USER_ID has a pending group name, take the first non-command text as the group name.
    Then confirm and remove from pending.
    """
    user = update.effective_user
    # Only handle if it's ALLOWED_USER_ID
    if user.id != ALLOWED_USER_ID:
        return

    # Do we have a pending group ID for them?
    if user.id not in pending_group_names:
        return  # no pending name – ignore

    text = (update.message.text or "").strip()
    if not text:
        logger.debug("User typed empty message while pending group name; ignoring.")
        return

    # The user has typed a text message and we have a pending group
    group_id = pending_group_names.pop(user.id)  # remove from dict so we don't overwrite it again

    # set the group name
    set_group_name(group_id, text)

    confirm_msg = f"✅ Group `{group_id}` name set to: *{text}*"
    await context.bot.send_message(chat_id=user.id, text=escape_markdown(confirm_msg, version=2), parse_mode='MarkdownV2')
    logger.info(f"User {user.id} set name of group {group_id} to '{text}'")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help – show commands
    """
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        return
    text = """*Commands*:
• `/start` – Check if the bot is alive
• `/group_add <group_id>` – Add a group; then send the name in a normal text
• `/rmove_group <group_id>` – Remove group from DB
• `/be_sad <group_id>` – Enable Arabic deletion
• `/be_happy <group_id>` – Disable Arabic deletion
• `/show` – Show all groups
• `/help` – This help
(... plus your other commands)"""
    await context.bot.send_message(
        chat_id=user.id,
        text=escape_markdown(text, version=2),
        parse_mode='MarkdownV2'
    )

# (Below, keep your other commands like /bypass, /unbypass, /rmove_group, etc., same as your code.)

# ------------------- Deletion / Filtering Handlers -------------------

delete_all_messages_after_removal = {}

def has_arabic(text):
    return bool(re.search(r'[\u0600-\u06FF]', text))

async def delete_arabic_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If the group is set to delete Arabic, remove any message (text/PDF/image) containing Arabic.
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    chat_id = msg.chat.id

    # check if that group is in deletion mode
    if not is_deletion_enabled(chat_id):
        return
    # check bypass
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

    # If PDF
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
                            logger.error(f"PyPDF2 error reading PDF: {e}")
                except Exception as e:
                    logger.error(f"Failed to parse PDF: {e}")
                finally:
                    try:
                        os.remove(tmp_pdf.name)
                    except:
                        pass

    # If photo
    if msg.photo:
        if pytesseract_available and pillow_available:
            photo_obj = msg.photo[-1]  # highest res
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
                    logger.error(f"OCR error on image: {e}")
                finally:
                    try:
                        os.remove(tmp_img.name)
                    except:
                        pass

async def delete_any_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    For a short time after forcibly removing a user from a group, we delete all messages there.
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

    # still within window
    try:
        await msg.delete()
        logger.info(f"Deleted a message in group {chat_id} due to short-term deletion flag.")
    except Exception as e:
        logger.error(f"Failed to delete message in group {chat_id}: {e}")

# (Keep your “/rmove_user”, “/check”, “/link”, etc. commands as in your code.)

# ------------------- Error Handler -------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error in the bot:", exc_info=context.error)

# ------------------- remove_deletion_flag_after_timeout -------------------

async def remove_deletion_flag_after_timeout(group_id):
    await asyncio.sleep(MESSAGE_DELETE_TIMEFRAME)
    if group_id in delete_all_messages_after_removal:
        delete_all_messages_after_removal.pop(group_id, None)
        logger.info(f"Removal-based deletion flag cleared for group {group_id}")

# ------------------- main() -------------------

def main():
    """
    Initialize DB, create the app, run in polling mode.
    """
    # DB init
    init_db()

    # get token from environment
    TOKEN = os.getenv('BOT_TOKEN')
    if not TOKEN:
        logger.error("BOT_TOKEN not set in environment.")
        sys.exit(1)
    TOKEN = TOKEN.strip()
    if TOKEN.lower().startswith('bot='):
        TOKEN = TOKEN[4:].strip()

    # build app
    app = ApplicationBuilder().token(TOKEN).build()

    # register command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("group_add", group_add_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # ... (Add your other commands: /rmove_group, /be_sad, /be_happy, /bypass, etc.)

    # message handlers
    # 1) Arabic deletion check
    app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO,
        delete_arabic_messages
    ))

    # 2) short-term group deletion after forcibly removing a user
    app.add_handler(MessageHandler(
        filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_any_messages
    ))

    # 3) capture the group name from ALLOWED_USER_ID (non-command text)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_any_text_for_group_name
    ))

    # error handler
    app.add_error_handler(error_handler)

    logger.info("Bot starting up...only one instance will run (file lock).")
    app.run_polling()

if __name__ == "__main__":
    main()
