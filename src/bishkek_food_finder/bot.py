"""Telegram bot for restaurant recommendations.

Run: uv run python -m bishkek_food_finder.bot
"""

import asyncio
import json
import os
import tempfile
import time
from functools import wraps

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, PicklePersistence

from bishkek_food_finder.agent import run as agent_run
from bishkek_food_finder.log import setup_service_logging
from bishkek_food_finder.config import CITIES, get_city_config

load_dotenv()

# === CONFIG ===

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = [u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()]
HISTORY_TIMEOUT = 30 * 60  # 30 minutes — auto-reset stale sessions
PERSISTENCE_PATH = "data/bot_persistence.pickle"

# City selection keyboard
CITY_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🇰🇬 Бишкек"), KeyboardButton("🇰🇿 Алматы")]
], resize_keyboard=True, one_time_keyboard=True)

# Map button text to city code
CITY_BUTTON_MAP = {
    "🇰🇬 Бишкек": "bishkek",
    "🇰🇿 Алматы": "almaty",
}


def get_main_keyboard(city: str) -> ReplyKeyboardMarkup:
    """Get main keyboard with location button and city change option."""
    city_config = get_city_config(city)
    return ReplyKeyboardMarkup([
        [KeyboardButton("📍 Отправить локацию", request_location=True)],
        [KeyboardButton(f"🏙 {city_config['name']} → сменить")]
    ], resize_keyboard=True)

# === LOGGING ===

logger = setup_service_logging("bot")


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


async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process a user query with the agent. Shared by message handler and pending message processing."""
    user = context.user_data
    city = user.get("city")

    # Auto-reset stale sessions
    now = time.time()
    last_msg_time = user.get("last_message_time", 0)
    if user.get("history") and (now - last_msg_time) > HISTORY_TIMEOUT:
        user["history"] = []
        logger.info(f"AUTO_RESET: user={update.effective_user.id} idle={now - last_msg_time:.0f}s")
    user["last_message_time"] = now

    # Build message with location context
    if user.get("location"):
        lat, lon = user["location"]
        message = f"[Локация: {lat}, {lon}]\n{text}"
    else:
        message = text

    # Build tool call notification callback
    loop = asyncio.get_running_loop()

    def on_tool_call(tool_name: str, params: dict):
        """Send status message when agent calls a tool."""
        if tool_name == "search_restaurants":
            msg = f"🔍 Ищу: {params.get('query', '...')}"
        elif tool_name == "get_restaurant":
            name = params.get("name") or params.get("id", "...")
            msg = f"📋 Смотрю: {name}"
        else:
            return
        future = asyncio.run_coroutine_threadsafe(
            update.message.reply_text(msg), loop
        )
        future.result(timeout=5)

    # Run agent with typing indicator
    typing_task = asyncio.create_task(keep_typing(update))
    try:
        response, user["history"], last_results = await asyncio.to_thread(
            agent_run, message, user.get("history", []), city,
            on_tool_call=on_tool_call, user_id=update.effective_user.id
        )
        if last_results:
            user["last_results"] = last_results
            user["last_query"] = text
    except Exception as e:
        error_str = str(e)
        logger.error(f"ERROR: user={update.effective_user.id} error={e}")

        if "tool_use_id" in error_str or "tool_result" in error_str:
            user["history"] = []
            logger.info(f"AUTO_RESET_ERROR: user={update.effective_user.id} history cleared")
            await update.message.reply_text("Что-то пошло не так. Начнём заново!")
        else:
            await update.message.reply_text("Ошибка. Попробуй /start")
        return
    finally:
        typing_task.cancel()

    await send_response(update, response)
    logger.info(f"RESPONSE: user={update.effective_user.id} city={city} len={len(response)}")


# === HANDLERS ===

CITY_SELECT_MSG = "Привет! Выбери город:"


def get_welcome_msg(city_name: str) -> str:
    return f"""
*Поиск ресторанов • {city_name}*

Анализирую 300K+ реальных отзывов и фильтрую фейковые.
Ищу по смыслу, а не по ключевым словам.

*Что можно спросить:*

🍽 *Поиск по блюду или кухне*
«вкусный плов»
«топовые самсы»
«лучшие суши»

📍 *Поиск рядом*
Сначала отправь 📍 локацию, потом:
«плов рядом»
«кофейня в 5 км от меня»

Или назови любой ресторан как ориентир:
«суши рядом с Navat»
«что-то рядом с Барашек»

🔍 *Вопрос о конкретном месте*
«что хвалят в Барашке»
«что поесть в Мубарак»
«как тебе Винтаж?»

Если у заведения несколько филиалов — покажу список, ты выберешь нужный.

*Что не работает:*
Поиск по районам: «в Асанбае», «в центре», «на юге»
→ Вместо района отправь 📍 локацию или назови ресторан рядом

*Команды:*
/reset — начать новый диалог
/json — скачать результаты поиска

⭐️ Рейтинг *(real)* = очищен от накруток
""".strip()


def get_help_msg(city_name: str) -> str:
    return get_welcome_msg(city_name)


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start - show city selection."""
    context.user_data.clear()
    await update.message.reply_text(CITY_SELECT_MSG, reply_markup=CITY_KEYBOARD)
    logger.info(f"START: user={update.effective_user.id}")


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help - show detailed instructions."""
    city = context.user_data.get("city", "bishkek")
    city_config = get_city_config(city)
    await update.message.reply_text(get_help_msg(city_config['name']), parse_mode="Markdown")
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
    city = context.user_data.get("city")
    logger.info(f"LOCATION: user={update.effective_user.id} lat={loc.latitude} lon={loc.longitude}")

    if not city:
        # Store location but ask for city first
        await update.message.reply_text(
            f"📍 Запомнил! Теперь выбери город:",
            reply_markup=CITY_KEYBOARD
        )
        return

    await update.message.reply_text(
        f"📍 Запомнил! ({loc.latitude:.4f}, {loc.longitude:.4f})",
        reply_markup=get_main_keyboard(city)
    )


@authorized
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text message - run agent and return response."""
    if not update.message or not update.message.text:
        return
    user = context.user_data
    text = update.message.text
    logger.info(f"MESSAGE: user={update.effective_user.id} text={text[:50]}...")

    # Handle city selection buttons
    if text in CITY_BUTTON_MAP:
        city = CITY_BUTTON_MAP[text]
        user["city"] = city
        user["history"] = []  # Reset history when changing city
        city_config = get_city_config(city)
        logger.info(f"CITY_SELECT: user={update.effective_user.id} city={city}")

        # Check for pending message (user sent query before selecting city)
        pending = user.pop("pending_message", None)
        if pending:
            logger.info(f"PENDING: user={update.effective_user.id} processing '{pending[:50]}...'")
            await update.message.reply_text(
                f"📍 {city_config['name']}",
                reply_markup=get_main_keyboard(city)
            )
            await process_query(update, context, pending)
        else:
            await update.message.reply_text(
                get_welcome_msg(city_config['name']),
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(city)
            )
        return

    # Handle city change button
    if "→ сменить" in text:
        await update.message.reply_text("Выбери новый город:", reply_markup=CITY_KEYBOARD)
        logger.info(f"CITY_CHANGE: user={update.effective_user.id}")
        return

    # Check if city is selected
    city = user.get("city")
    if not city:
        # Store pending message to process after city selection
        user["pending_message"] = text
        logger.info(f"PENDING_STORE: user={update.effective_user.id} text='{text[:50]}...'")
        await update.message.reply_text("Сначала выбери город:", reply_markup=CITY_KEYBOARD)
        return

    await process_query(update, context, text)


# === MAIN ===

def main():
    """Start the bot."""
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
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
