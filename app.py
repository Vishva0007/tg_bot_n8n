import os
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler
)
import google.generativeai as genai

# ======================
# Logging
# ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ======================
# API Keys
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_KEY)

# ======================
# Free / Premium Settings
# ======================
FREE_LIMIT = 5
user_usage = {}
premium_users = set()

# ======================
# Bot Handlers
# ======================
async def start(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("☕ Buy me a Coffee", callback_data="coffee")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Welcome to the AI Summary Bot!\n\n"
        "• Summarizes messages using Gemini AI\n"
        f"• Free limit: {FREE_LIMIT} summaries per day\n"
        "• Unlock unlimited access with Premium 🚀\n\n"
        "Type /help to learn commands.",
        reply_markup=reply_markup
    )

async def help_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "📖 Commands:\n"
        "/start - Start bot\n"
        "/help - Show help\n"
        "/limits - Show usage\n"
        "/buy - Buy premium\n"
        "/verify - Verify premium\n"
        "/premium - Premium info"
    )

async def limits_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    usage = user_usage.get(user_id, 0)
    await update.message.reply_text(
        f"📊 You used {usage}/{FREE_LIMIT} free summaries today."
    )

async def buy_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "☕ Support this bot & unlock **unlimited premium**!\n\n"
        "Payment link (BuyMeACoffee, Razorpay, UPI, etc.)\n\n"
        "After payment, type /verify"
    )

async def verify_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    premium_users.add(user_id)
    await update.message.reply_text("✅ Premium activated! Enjoy unlimited summaries 🚀")

async def premium_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "🌟 Premium Features:\n"
        "• Unlimited summaries\n"
        "• Faster responses\n"
        "• Priority support\n"
        "• Help support development"
    )

async def button_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data == "coffee":
        await query.edit_message_text("☕ Buy me a coffee at: https://paypal.me/SHRI0709")

async def summarize_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    text = update.message.text

    # Free limit check
    if user_id not in premium_users:
        usage = user_usage.get(user_id, 0)
        if usage >= FREE_LIMIT:
            await update.message.reply_text("⚠️ Free limit reached. Type /buy to upgrade.")
            return
        user_usage[user_id] = usage + 1

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(f"Summarize this: {text}")
        await update.message.reply_text("📝 Summary:\n" + response.text)
    except Exception as e:
        await update.message.reply_text("❌ Error: " + str(e))

# ======================
# Main
# ======================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("limits", limits_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize_message))

    # Webhook setup
    port = int(os.getenv("PORT", 8443))
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_URL').replace('https://','')}/{TELEGRAM_TOKEN}"

    print(f"🚀 Starting bot in webhook mode at {webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()
