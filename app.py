import os, re, time, sqlite3, json, base64, hmac, hashlib, datetime as dt
import requests
import google.generativeai as genai
from bs4 import BeautifulSoup
from readability import Document
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ======================
# CONFIG
# ======================
BOT_NAME = "Quick Summary Bot"
FREE_LIMIT_PER_DAY = int(os.getenv("FREE_LIMIT_PER_DAY", "5"))
PREMIUM_DAYS_ON_PAYMENT = int(os.getenv("PREMIUM_DAYS_ON_PAYMENT", "30"))
PREMIUM_PRICE_INR = int(os.getenv("PREMIUM_PRICE_INR", "149"))  # change anytime

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Razorpay (recommended for India)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")            # required to create payment links
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")    # required to create payment links
RAZORPAY_NOTIFY_URL = os.getenv("RAZORPAY_NOTIFY_URL", "")  # optional: if you later add a webhook endpoint

# Safety checks
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY missing")

# Gemini setup
genai.configure(api_key=GEMINI_API_KEY)
MODEL = genai.GenerativeModel("gemini-1.5-flash")

# ======================
# DB (SQLite) – stored in container filesystem (Render persists across restarts); you can later switch to a hosted DB.
# ======================
DB_PATH = os.getenv("DB_PATH", "bot.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  premium_until INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS usage(
  user_id INTEGER,
  day TEXT,
  count INTEGER,
  PRIMARY KEY(user_id, day)
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  plink_id TEXT,
  status TEXT,
  amount INTEGER,
  created_at INTEGER
)
""")
conn.commit()

def now_ts():
    return int(time.time())

def today_str():
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def is_premium(user_id: int) -> bool:
    cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row: return False
    return row[0] > now_ts()

def ensure_user(user_id: int):
    cur.execute("INSERT OR IGNORE INTO users(user_id, premium_until) VALUES(?, ?)", (user_id, 0))
    conn.commit()

def get_usage(user_id: int) -> int:
    cur.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, today_str()))
    row = cur.fetchone()
    return row[0] if row else 0

def inc_usage(user_id: int):
    day = today_str()
    cur.execute("INSERT OR IGNORE INTO usage(user_id, day, count) VALUES(?, ?, 0)", (user_id, day))
    cur.execute("UPDATE usage SET count = count + 1 WHERE user_id=? AND day=?", (user_id, day))
    conn.commit()

def grant_premium(user_id: int, days: int):
    ensure_user(user_id)
    cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    base = row[0] if row and row[0] > now_ts() else now_ts()
    new_until = base + days * 86400
    cur.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, user_id))
    conn.commit()
    return new_until

# ======================
# Helpers: URL detection, fetching, summarization
# ======================
YOUTUBE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_\-]+)")

def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

def extract_youtube_id(text: str):
    m = YOUTUBE_RE.search(text)
    return m.group(1) if m else None

def fetch_youtube_transcript(video_id: str) -> str | None:
    try:
        tr = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'en-US', 'hi'])
        transcript_text = " ".join([x["text"] for x in tr])
        return transcript_text.strip()
    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript):
        return None
    except Exception:
        return None

def fetch_article_text(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        doc = Document(resp.text)
        html = doc.summary()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n")
        # crude cleanup
        text = re.sub(r"\n{2,}", "\n\n", text).strip()
        return text[:30000]  # cap to be safe
    except Exception:
        return None

def gemini_summarize(text: str, style: str = "auto") -> str:
    # style: "auto", "bullets", "short", "detailed"
    instructions = {
        "auto": "Summarize the following content clearly. Keep it concise and accurate. If the text is long, use bullet points followed by a short paragraph of key insights.",
        "bullets": "Summarize in 5-8 bullet points with clear, punchy statements. End with 2 key takeaways.",
        "short": "Summarize in 3-4 sentences, plain language, no fluff.",
        "detailed": "Create a detailed summary with headings: Overview, Key Points, Insights, Actionable Notes. Keep factual and structured."
    }
    prompt = f"""{instructions.get(style,'auto')}
Text:
{text}
If content is not in English, respond in the same language."""
    resp = MODEL.generate_content(prompt)
    return resp.text.strip()

# ======================
# Razorpay – Payment Links (simple flow)
# ======================
def create_payment_link(user_id: int, amount_inr: int) -> str | None:
    """Creates a one-time payment link and stores its id; returns the short URL."""
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        return None
    url = "https://api.razorpay.com/v1/payment_links"
    payload = {
        "amount": amount_inr * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": f"user_{user_id}_{now_ts()}",
        "description": f"{BOT_NAME} Premium ({PREMIUM_DAYS_ON_PAYMENT} days)",
        "notify": {"sms": False, "email": False},
        "reminder_enable": True,
        "callback_url": RAZORPAY_NOTIFY_URL or "https://razorpay.com",
        "callback_method": "get",
        "customer": {"name": f"TG_{user_id}"}
    }
    r = requests.post(url, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json=payload, timeout=15)
    if r.status_code not in (200, 201):
        return None
    data = r.json()
    plink_id = data.get("id")
    short_url = data.get("short_url")
    if plink_id:
        cur.execute("INSERT INTO payments(user_id, plink_id, status, amount, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, plink_id, "created", amount_inr, now_ts()))
        conn.commit()
    return short_url

def refresh_payment_status(user_id: int) -> str:
    """Checks latest payment link status; if paid, grants premium."""
    cur.execute("SELECT plink_id, status, amount FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if not row: return "No payment found. Use /buy to get a payment link."
    plink_id, old_status, amount = row
    url = f"https://api.razorpay.com/v1/payment_links/{plink_id}"
    r = requests.get(url, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=15)
    if r.status_code != 200:
        return "Couldn't verify payment yet. Try again in a minute."
    data = r.json()
    status = data.get("status")
    if status != old_status:
        cur.execute("UPDATE payments SET status=? WHERE plink_id=?", (status, plink_id))
        conn.commit()
    if status == "paid":
        new_until = grant_premium(user_id, PREMIUM_DAYS_ON_PAYMENT)
        until_str = dt.datetime.utcfromtimestamp(new_until).strftime("%Y-%m-%d")
        return f"✅ Payment verified! Premium active until {until_str}. Enjoy unlimited summaries."
    elif status in ("created", "partially_paid"):
        return "Payment not completed yet. Complete the payment and use /verify again."
    else:
        return f"Payment status: {status}. If you completed payment but still see this, wait a minute and /verify."

# ======================
# Telegram Handlers
# ======================
WELCOME = (
    "👋 *Welcome to Quick Summary Bot!*\n\n"
    "Send me:\n"
    "• Plain text → I’ll summarize it\n"
    "• YouTube link → I’ll fetch transcript (if available) and summarize\n"
    "• Article URL → I’ll extract the page and summarize\n\n"
    f"Free tier: {FREE_LIMIT_PER_DAY}/day • Premium: unlimited\n"
    "Commands: /help /limits /buy /verify /premium"
)

HELP = (
    "💡 *How to use*\n"
    "• Paste text, a YouTube URL, or an article URL\n"
    "• Choose summary style with buttons\n\n"
    f"Free tier: {FREE_LIMIT_PER_DAY} requests per day.\n"
    "Upgrade with /buy (Razorpay • UPI/cards) then /verify."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    kb = [
        [InlineKeyboardButton("✨ Auto", callback_data="style:auto"),
         InlineKeyboardButton("• Bullets", callback_data="style:bullets")],
        [InlineKeyboardButton("📝 Short", callback_data="style:short"),
         InlineKeyboardButton("📚 Detailed", callback_data="style:detailed")]
    ]
    await update.message.reply_text(WELCOME, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")

async def limits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usage = get_usage(user_id)
    prem = is_premium(user_id)
    msg = f"📊 Today’s usage: {usage}/{ '∞' if prem else FREE_LIMIT_PER_DAY }\n"
    if prem:
        cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
        until = cur.fetchone()[0]
        until_str = dt.datetime.utcfromtimestamp(until).strftime("%Y-%m-%d")
        msg += f"💎 Premium active until: {until_str}"
    else:
        msg += "Upgrade with /buy for unlimited summaries."
    await update.message.reply_text(msg)

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        await update.message.reply_text(
            "🔒 Payments are not configured yet. Please try again later."
        )
        return
    link = create_payment_link(user_id, PREMIUM_PRICE_INR)
    if not link:
        await update.message.reply_text("⚠️ Couldn't create payment link. Try again in a minute.")
        return
    await update.message.reply_text(
        f"💎 *Premium* — {PREMIUM_DAYS_ON_PAYMENT} days unlimited summaries\n"
        f"Price: ₹{PREMIUM_PRICE_INR}\n\n"
        f"👉 Pay securely via Razorpay:\n{link}\n\n"
        "After payment, tap /verify to activate.",
        parse_mode="Markdown"
    )

async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        await update.message.reply_text("Payments not configured.")
        return
    msg = refresh_payment_status(update.effective_user.id)
    await update.message.reply_text(msg)

# Remember user’s preferred style in-memory (per chat)
STYLE_PREF: dict[int, str] = {}

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("style:"):
        style = data.split(":", 1)[1]
        STYLE_PREF[query.from_user.id] = style
        await query.edit_message_text(f"✅ Summary style set to *{style}*.", parse_mode="Markdown")

def can_use(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    return get_usage(user_id) < FREE_LIMIT_PER_DAY

def record_usage(user_id: int):
    if not is_premium(user_id):
        inc_usage(user_id)

async def summarize_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    text = update.message.text.strip()

    # Enforce limits
    if not can_use(user_id):
        await update.message.reply_text(
            f"🚦 You’ve reached today’s free limit of {FREE_LIMIT_PER_DAY}. Use /buy to unlock unlimited summaries, then /verify."
        )
        return

    # Decide input type
    style = STYLE_PREF.get(user_id, "auto")
    yt_id = extract_youtube_id(text) if is_url(text) else None

    await update.message.chat.send_action(action="typing")

    try:
        if yt_id:
            transcript = fetch_youtube_transcript(yt_id)
            if not transcript:
                await update.message.reply_text(
                    "❗Couldn't fetch transcript for this video (captions may be disabled). "
                    "Try another link or paste text."
                )
                return
            summary = gemini_summarize(transcript, style)
        elif is_url(text):
            page = fetch_article_text(text)
            if not page:
                await update.message.reply_text("❗Couldn't extract that page. Try another URL or paste the text.")
                return
            summary = gemini_summarize(page, style)
        else:
            summary = gemini_summarize(text, style)

        record_usage(user_id)

        footer = "\n\n— Powered by Gemini • Free tier available • /buy for unlimited"
        # Telegram messages have limits; truncate if huge
        final = (summary + footer)[:3900]
        await update.message.reply_text(final, disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💎 *Premium Plan*\n"
        f"• Unlimited summaries\n"
        f"• Priority responses\n"
        f"• {PREMIUM_DAYS_ON_PAYMENT} days access per purchase\n\n"
        f"Price: ₹{PREMIUM_PRICE_INR}\n"
        f"Use /buy to get a Razorpay link, then /verify to activate.",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("limits", limits_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, summarize_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, summarize_message))
    app.add_handler(MessageHandler(filters.ALL, summarize_message))
    app.add_handler(MessageHandler(filters.UpdateType.CALLBACK_QUERY, summarize_message))
    app.add_handler(MessageHandler(filters.ALL, summarize_message))
    app.add_handler(MessageHandler(filters.ALL, summarize_message))
    app.add_handler(MessageHandler(filters.ALL, summarize_message))
    # Proper callback handler
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(button_cb))

    print("🚀 Bot running with premium & limits…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
