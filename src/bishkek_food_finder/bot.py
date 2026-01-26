"""Telegram bot for restaurant recommendations.

Run: uv run python -m bishkek_food_finder.bot
"""

import asyncio
import json
import logging
import os
import tempfile
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from bishkek_food_finder.agent import run as agent_run

load_dotenv()

# === CONFIG ===

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = [u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()]

# === LOGGING ===

Path("logs").mkdir(exist_ok=True)
logger = logging.getLogger("bishkek_food_finder.bot")
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.FileHandler("logs/bot.log"))
logger.addHandler(logging.StreamHandler())


# === HELPERS ===

def authorized(func):
    """Decorator to restrict access to allowed users only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_USERS and update.effective_user.username not in ALLOWED_USERS:
            await update.message.reply_text("Доступ запрещён.")
            return
        return await func(update, context)
    return wrapper


async def send_response(update: Update, text: str):
    """Send response with markdown fallback and message splitting."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)] if len(text) > 4000 else [text]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(chunk, disable_web_page_preview=True)


async def keep_typing(update: Update):
    """Keep typing indicator active while agent processes."""
    while True:
        await update.message.chat.send_action("typing")
        await asyncio.sleep(5)


# === HANDLERS ===

WELCOME_MSG = """
Привет! Я помогу найти ресторан в Бишкеке.

*Как это работает:*
Анализирую 294,000 реальных отзывов и фильтрую фейковые.
Ищу по смыслу, а не по ключевым словам.

*Что я умею:*
• По кухне: «хочу суши», «где плов»
• По атмосфере: «уютное место для свидания»
• По цене: «недорогой обед до 500 сом»
• По расстоянию: «кафе рядом» (нужна локация)
• О конкретном месте: «что поесть в Navat», «как тебе La Maison»

*Поиск рядом:*
📍 Отправь локацию и напиши «рядом» в запросе.

*Команды:*
/help — справка
/reset — начать заново
""".strip()

HELP_MSG = """
*Бот для поиска ресторанов в Бишкеке*

📊 294,000 отзывов • Фильтрация фейков • Умный поиск

━━━━━━━━━━━━━━━━━━━━

*Что умею:*

🍽 *Поиск* — «вкусный плов», «суши», «романтический ужин»

💰 *По бюджету* — «недорого до 500 сом», «средний бюджет»

📍 *Рядом с тобой* — отправь 📍 локацию, потом «кафе рядом»

📍 *Рядом с местом* — «плов рядом с Navat» (назови любой ресторан)

🔍 *О месте* — «что поесть в Navat», «как тебе Винтаж»

💬 *Уточнения* — «ещё варианты», «а подешевле?»

━━━━━━━━━━━━━━━━━━━━

*Не поддерживается:*
Поиск по районам («в центре», «на юге»)
→ Отправь локацию или назови ресторан рядом

━━━━━━━━━━━━━━━━━━━━

*Команды:*
/reset — очистить историю
/json — скачать результаты поиска

*Рейтинг (real)* = без накрученных отзывов
""".strip()


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start - welcome message and reset state."""
    context.user_data.clear()
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Отправить локацию", request_location=True)]],
        resize_keyboard=True
    )
    await update.message.reply_text(WELCOME_MSG, parse_mode="Markdown", reply_markup=keyboard)
    logger.info(f"START: user={update.effective_user.id}")


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help - show detailed instructions."""
    await update.message.reply_text(HELP_MSG, parse_mode="Markdown")
    logger.info(f"HELP: user={update.effective_user.id}")


@authorized
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reset - clear conversation history."""
    context.user_data["history"] = []
    await update.message.reply_text("История очищена!")
    logger.info(f"RESET: user={update.effective_user.id}")


@authorized
async def cmd_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /json - send last search results as JSON file."""
    results = context.user_data.get("last_results")
    if not results:
        await update.message.reply_text("Сначала сделай поиск.")
        return
    query = context.user_data.get("last_query", "search")
    filename = f"search_{query[:30].replace(' ', '_')}.json"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        path = f.name
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename=filename)
    os.unlink(path)
    logger.info(f"JSON: user={update.effective_user.id} file={filename}")


@authorized
async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle location - store for geo-filtered searches."""
    loc = update.message.location
    context.user_data["location"] = (loc.latitude, loc.longitude)
    await update.message.reply_text(f"📍 Запомнил! ({loc.latitude:.4f}, {loc.longitude:.4f})")
    logger.info(f"LOCATION: user={update.effective_user.id} lat={loc.latitude} lon={loc.longitude}")


@authorized
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text message - run agent and return response."""
    if not update.message or not update.message.text:
        return
    user = context.user_data
    text = update.message.text
    logger.info(f"MESSAGE: user={update.effective_user.id} text={text[:50]}...")

    # Build message with location context
    if user.get("location"):
        lat, lon = user["location"]
        message = f"[Локация: {lat}, {lon}]\n{text}"
    else:
        message = text

    # Run agent with typing indicator
    typing_task = asyncio.create_task(keep_typing(update))
    try:
        response, user["history"], last_results = await asyncio.to_thread(
            agent_run, message, user.get("history", [])
        )
        if last_results:
            user["last_results"] = last_results
            user["last_query"] = text
    except Exception as e:
        logger.error(f"ERROR: user={update.effective_user.id} error={e}")
        await update.message.reply_text("Ошибка. Попробуй /start")
        return
    finally:
        typing_task.cancel()

    await send_response(update, response)
    logger.info(f"RESPONSE: user={update.effective_user.id} len={len(response)}")


# === MAIN ===

def main():
    """Start the bot."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("json", cmd_json))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
