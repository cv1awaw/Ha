# main.py

from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
import logging

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Token
BOT_TOKEN = "YOUR TOKEN HERE"

# Allowed user ID for banning
ALLOWED_USER_ID = 6177929931

# Command handler for /start
def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('Hello! I am your bot. Use /ban <user_id> to ban a user.')

# Command handler for /ban
def ban(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat = update.effective_chat

    # Check if the user ID is allowed to ban
    if user.id != ALLOWED_USER_ID:
        update.message.reply_text('You do not have permission to ban users.')
        return

    if len(context.args) == 0:
        update.message.reply_text('Please specify a user ID to ban. Usage: /ban <user_id>')
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        update.message.reply_text('Invalid user ID. Please provide a numerical user ID.')
        return

    try:
        # Attempt to ban the user by user ID
        context.bot.kick_chat_member(chat_id=chat.id, user_id=user_id)
        update.message.reply_text(f'User with ID {user_id} has been banned.')
    except Exception as e:
        logger.error(f'Error banning user: {e}')
        update.message.reply_text('An error occurred while trying to ban the user.')

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("ban", ban, pass_args=True))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
