# main.py

import logging
import os
import json
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Ensure BOT_TOKEN is set in Railway's environment variables

# Allowed user ID for muting
ALLOWED_USER_ID = 6177929931  # Replace with your actual Telegram user ID

# File to store muted user IDs
MUTED_USERS_FILE = "muted_users.json"

def load_muted_users() -> set:
    """Load muted user IDs from a JSON file."""
    if not os.path.exists(MUTED_USERS_FILE):
        return set()
    try:
        with open(MUTED_USERS_FILE, "r") as file:
            data = json.load(file)
            return set(data)
    except Exception as e:
        logger.error(f"Failed to load muted users: {e}")
        return set()

def save_muted_users(muted_users: set) -> None:
    """Save muted user IDs to a JSON file."""
    try:
        with open(MUTED_USERS_FILE, "w") as file:
            json.dump(list(muted_users), file)
    except Exception as e:
        logger.error(f"Failed to save muted users: {e}")

# Initialize muted users set
muted_users = load_muted_users()

async def is_user_muted(update: Update) -> bool:
    """Check if the user is muted."""
    user_id = update.effective_user.id
    return user_id in muted_users

# Command handler for /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await is_user_muted(update):
        await update.message.reply_text("You are muted and cannot use this bot.")
        return
    await update.message.reply_text(
        'Hello! I am your bot. Use /mute <user_id> to mute a user.'
    )

# Command handler for /mute
async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Check if the user is allowed to mute others
    if user.id != ALLOWED_USER_ID:
        await update.message.reply_text('You do not have permission to mute users.')
        return

    if len(context.args) != 1:
        await update.message.reply_text('Please specify a user ID to mute. Usage: /mute <user_id>')
        return

    try:
        user_id_to_mute = int(context.args[0])
    except ValueError:
        await update.message.reply_text('Invalid user ID. Please provide a numerical user ID.')
        return

    if user_id_to_mute in muted_users:
        await update.message.reply_text(f'User with ID {user_id_to_mute} is already muted.')
        return

    muted_users.add(user_id_to_mute)
    save_muted_users(muted_users)

    await update.message.reply_text(f'User with ID {user_id_to_mute} has been muted.')

# Optional: Command handler for /unmute
async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Check if the user is allowed to unmute others
    if user.id != ALLOWED_USER_ID:
        await update.message.reply_text('You do not have permission to unmute users.')
        return

    if len(context.args) != 1:
        await update.message.reply_text('Please specify a user ID to unmute. Usage: /unmute <user_id>')
        return

    try:
        user_id_to_unmute = int(context.args[0])
    except ValueError:
        await update.message.reply_text('Invalid user ID. Please provide a numerical user ID.')
        return

    if user_id_to_unmute not in muted_users:
        await update.message.reply_text(f'User with ID {user_id_to_unmute} is not muted.')
        return

    muted_users.remove(user_id_to_unmute)
    save_muted_users(muted_users)

    await update.message.reply_text(f'User with ID {user_id_to_unmute} has been unmuted.')

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set. Please set it in Railway.com.")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("unmute", unmute))  # Optional: Enable unmute functionality

    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()
