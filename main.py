import os
import logging
import threading
import asyncio
from flask import Flask, request, jsonify
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ============================
# 🔑 Configuration Variables
# ============================

BOT_TOKEN = os.getenv('BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')  # e.g., '-1001234567890'
AUTHORIZED_USER_ID = 6177929931  # Replace with your Telegram user ID
WEB_API_KEY = os.getenv('WEB_API_KEY')  # Set this in Railway for security

# ============================
# 📋 Logging Configuration
# ============================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================
# 🛠️ Flask Web Server Setup
# ============================

app = Flask(__name__)

@app.route('/ban', methods=['POST'])
def ban_user_via_web():
    """
    Endpoint to ban a user via a web interface.
    Expects JSON payload with 'user_id' and 'api_key'.
    """
    data = request.get_json()
    api_key = data.get('api_key')
    user_id = data.get('user_id')
    
    # Simple API key authentication
    if api_key != WEB_API_KEY:
        return jsonify({'status': 'error', 'message': 'Unauthorized access'}), 403
    
    if not user_id:
        return jsonify({'status': 'error', 'message': 'user_id is required'}), 400
    
    try:
        user_id = int(user_id)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'user_id must be an integer'}), 400
    
    # Call the helper function to ban the user
    success, message = ban_user_in_chat(user_id)
    
    if success:
        return jsonify({'status': 'success', 'message': message}), 200
    else:
        return jsonify({'status': 'error', 'message': message}), 500

def ban_user_in_chat(user_id):
    """
    Bans a user from the Telegram chat.
    Returns a tuple (success: bool, message: str)
    """
    try:
        # Access the global bot instance
        bot = application.bot
        bot.ban_chat_member(chat_id=TELEGRAM_CHAT_ID, user_id=user_id)
        bot.send_message(chat_id=user_id, text="🚫 You got banned from the bot.")
        logger.info(f"User {user_id} has been banned via web interface.")
        return True, f"User {user_id} has been banned."
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")
        return False, str(e)

# ============================
# 🛠️ Telegram Bot Command Handlers
# ============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    welcome_message = (
        "👋 Hello! I'm the Admin Bot.\n\n"
        "🔧 Use /ban <user_id> to ban a user from this chat.\n"
        "🚫 Only the authorized admin can use this command."
    )
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /ban command via Telegram."""
    issuer_id = update.effective_user.id

    # Authorization check
    if issuer_id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        logger.warning(f"Unauthorized ban attempt by user ID: {issuer_id}")
        return

    # Argument check
    if len(context.args) != 1:
        await update.message.reply_text("ℹ️ Usage: /ban <user_id>")
        return

    try:
        # Parse the target user ID
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user ID. Please provide a numerical ID.")
        return

    # Call the helper function to ban the user
    success, message = ban_user_in_chat(target_user_id)

    if success:
        await update.message.reply_text(f"✅ User {target_user_id} has been banned.")
    else:
        await update.message.reply_text(f"⚠️ An error occurred: {message}")

# ============================
# 🚀 Bot and Web Server Functions
# ============================

async def run_telegram_bot():
    """Function to start the Telegram bot."""
    global application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ban", ban_command))

    # Run the bot
    logger.info("Starting Telegram bot...")
    await application.run_polling()

def run_flask_app():
    """Function to start the Flask web server."""
    logger.info("Starting Flask web server...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

# ============================
# 🏁 Main Execution
# ============================

if __name__ == '__main__':
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables.")
        exit(1)

    # Start the Telegram bot in a separate thread
    bot_thread = threading.Thread(target=lambda: 
                                  asyncio.run(run_telegram_bot()), 
                                  daemon=True)
    bot_thread.start()

    # Start the Flask web server in the main thread
    run_flask_app()
