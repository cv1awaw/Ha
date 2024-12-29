# main.py

import logging
import os
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

# Bot Token
BOT_TOKEN = "YOUR_TOKEN_HERE"  # Replace with your actual bot token

# Allowed user ID for banning
ALLOWED_USER_ID = 6177929931  # Replace with the actual user ID

# Command handler for /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        'Hello! I am your bot. Use /ban <user_id> to ban a user.'
    )

# Command handler for /ban
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    # Check if the user ID is allowed to ban
    if user.id != ALLOWED_USER_ID:
        await update.message.reply_text('You do not have permission to ban users.')
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            'Please specify a user ID to ban. Usage: /ban <user_id>'
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text('Invalid user ID. Please provide a numerical user ID.')
        return

    try:
        # Attempt to ban the user by user ID
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user_id)
        await update.message.reply_text(f'User with ID {user_id} has been banned.')
    except Exception as e:
        logger.error(f'Error banning user: {e}')
        await update.message.reply_text('An error occurred while trying to ban the user.')

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ban", ban))

    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()
