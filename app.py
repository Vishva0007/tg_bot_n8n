import os
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load API key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# Create model
model = genai.GenerativeModel("gemini-1.5-flash")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me any text and I'll summarize it for you!")

async def summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    prompt = f"Summarize this text in 3-4 bullet points:\n\n{user_text}"
    response = model.generate_content(prompt)
    await update.message.reply_text(response.text)

def main():
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    app = Application.builder().token(telegram_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
