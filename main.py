import os
import logging
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, Filters

# ============================
# 🔑 Configuration Variables
# ============================

# Retrieve your bot token from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Ensure 'BOT_TOKEN' is set in your environment

# Authorized user ID who can use the /ban command
AUTHORIZED_USER_ID = 6177929931

# ============================
# 📋 Logging Configuration
# ============================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================
# 🛠️ Command Handlers
# ============================

def start(update: Update, context: CallbackContext):
    """Handler for the /start command."""
    welcome_message = (
        "👋 Hello! I'm the Admin Bot.\n\n"
        "🔧 Use /ban <user_id> to ban a user from this chat.\n"
        "🚫 Only the authorized admin can use this command."
    )
    update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

def ban_user(update: Update, context: CallbackContext):
    """Handler for the /ban command."""
    issuer_id = update.effective_user.id

    # Authorization check
    if issuer_id != AUTHORIZED_USER_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        logger.warning(f"Unauthorized ban attempt by user ID: {issuer_id}")
        return

    # Argument check
    if len(context.args) != 1:
        update.message.reply_text("ℹ️ Usage: /ban <user_id>")
        return

    try:
        # Parse the target user ID
        target_user_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("⚠️ Invalid user ID. Please provide a numerical ID.")
        return

    chat_id = update.effective_chat.id

    try:
        # Ban the user
        context.bot.kick_chat_member(chat_id=chat_id, user_id=target_user_id)
        update.message.reply_text(f"✅ User {target_user_id} has been banned.")

        # Notify the banned user
        context.bot.send_message(
            chat_id=target_user_id,
            text="🚫 You got ban from bot."
        )

        logger.info(f"User {target_user_id} banned by admin {issuer_id}.")

    except Exception as e:
        update.message.reply_text(f"⚠️ An error occurred: {e}")
        logger.error(f"Error banning user {target_user_id}: {e}")

def error_handler(update: object, context: CallbackContext):
    """Log errors caused by Updates."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# ============================
# 🚀 Bot Initialization
# ============================

def main():
    """Start the bot."""
    # Ensure BOT_TOKEN is available
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in environment variables.")
        return

    # Initialize the Updater and Dispatcher
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    # Register command handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("ban", ban_user, pass_args=True, filters=Filters.chat_type.groups))

    # Register the error handler
    dispatcher.add_error_handler(error_handler)

    # Start polling for updates
    updater.start_polling()
    logger.info("Bot is up and running...")

    # Run the bot until interrupted
    updater.idle()

if __name__ == '__main__':
    main()
