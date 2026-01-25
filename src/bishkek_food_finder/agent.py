"""Agent for restaurant recommendations.

Handles natural language queries, clarification, and follow-ups.

Run: uv run python -m bishkek_food_finder.agent "где вкусный плов"
     uv run python -m bishkek_food_finder.agent -i  # interactive mode
"""

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

# Load .env file
load_dotenv()

from bishkek_food_finder.search.pipeline import search

# === CONFIG ===

MODEL = "claude-opus-4-5-20251101"
MAX_ITERATIONS = 5
MAX_RESTAURANTS = 15
MAX_REVIEWS = 5

# === LOGGING ===

LOG_PATH = Path("logs/agent.log")
LOG_PATH.parent.mkdir(exist_ok=True)

# Configure our logger only (not root logger which captures HTTP noise)
logger = logging.getLogger("bishkek_food_finder.agent")
logger.setLevel(logging.DEBUG)

# File handler - all levels
file_handler = logging.FileHandler(LOG_PATH)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(file_handler)

# Console handler - INFO only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(console_handler)

# === SYSTEM PROMPT ===

SYSTEM_PROMPT = """Ты — бот для поиска ресторанов в Бишкеке.

## Возможности
- Поиск по кухне, атмосфере, блюдам, цене, локации
- Доступ к 294,000 реальных отзывов (с фильтрацией фейков)
- Понимание контекста: "уютное место для свидания"

## Как искать
1. Используй search_restaurants для любых запросов о ресторанах
2. Формулируй query конкретно на русском
3. Используй geo_preset когда user говорит "рядом" (walking), "недалеко" (nearby)
4. Используй price_max когда user говорит "недорого" (~500), "средний бюджет" (~1500)
5. ПРОВЕРЯЙ отзывы — поиск семантический, может найти ложные совпадения
6. Если user говорит "используй мой точный запрос" или "exact prompt" — передай его query БЕЗ изменений

## Когда уточнять
- "хочу поесть" → спроси кухню, бюджет, повод
- "рядом" без локации → спроси где находится
- Несколько интерпретаций → уточни

## Когда НЕ уточнять
- Конкретный запрос: "лучший плов" — сразу ищи
- User уже дал контекст

## Формат ответа
1. Рекомендация (1-3 места)
2. Почему подходит (на основе отзывов)
3. Цитаты из проверенных отзывов
4. Предложи ещё варианты

## Пример
User: где вкусный плов?

[вызов search_restaurants(query="вкусный плов")]

Ответ:
**Навват** ⭐ 4.2 (наш рейтинг) • $$
📍 ул. Чуй, 123

Почему: Многие отмечают аутентичный плов.

💬 «Лучший плов в городе, готовят по-узбекски» (проверенный отзыв)
💬 «Порции огромные, плов рассыпчатый»

🔗 2gis.kg/bishkek/firm/...

Хотите ещё варианты?

## Важно
- Отвечай ТОЛЬКО на русском
- Не придумывай отзывы — используй только из результатов поиска
- Если ничего не найдено — честно скажи, предложи расширить критерии
- Вопросы не про рестораны — вежливо откажи
"""

# === TOOL DEFINITION ===

TOOLS = [{
    "name": "search_restaurants",
    "description": """Search for restaurants in Bishkek.

Use when user asks for recommendations. Semantic search across 294k reviews.

Returns restaurants ranked by: relevance × trust × sentiment.

IMPORTANT: Search is semantic, not keyword. "вкусные завтраки" may match
"вкусный шашлык". YOU must verify reviews actually mention what user wants.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query in Russian"
            },
            "latitude": {
                "type": "number",
                "description": "User's latitude"
            },
            "longitude": {
                "type": "number",
                "description": "User's longitude"
            },
            "geo_preset": {
                "type": "string",
                "enum": ["walking", "nearby", "driving", "city_wide"],
                "description": "walking=3km, nearby=5km, driving=10km, city_wide=no limit"
            },
            "price_max": {
                "type": "integer",
                "description": "Maximum average price in SOM"
            },
            "open_now": {
                "type": "boolean",
                "description": "Only show currently open restaurants"
            }
        },
        "required": ["query"]
    }
}]

# === CLIENT ===

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise EnvironmentError(
        "ANTHROPIC_API_KEY not set. Run:\n"
        "  export ANTHROPIC_API_KEY='your-key-here'"
    )

client = Anthropic()

# === HELPER FUNCTIONS ===


def compress_results(results: list[dict]) -> list[dict]:
    """Compress search results for LLM consumption."""
    compressed = []

    for r in results[:MAX_RESTAURANTS]:
        compressed.append({
            "name": r["name"],
            "address": r["address"],
            "link": r["link"],
            "distance_km": r.get("distance_km"),
            "rating_2gis": r["rating_2gis"],
            "rating_trusted": round(r["rating_trusted"], 2) if r["rating_trusted"] else None,
            "trusted_review_count": r["trusted_review_count"],
            "score": round(r["score"], 2),
            "category": r["category"],
            "cuisine": r["cuisine"],
            "avg_price_som": r["avg_price_som"],
            "reviews": [
                {
                    "text": rev["text"][:300],
                    "rating": rev["rating"],
                    "trust": round(rev["trust"], 2),
                }
                for rev in r["reviews"][:MAX_REVIEWS]
            ]
        })

    return compressed


def execute_search(params: dict) -> dict:
    """Execute search pipeline and return compressed results."""
    try:
        location = None
        if params.get("latitude") and params.get("longitude"):
            location = (params["latitude"], params["longitude"])

        results = search(
            query=params["query"],
            location=location,
            geo_preset=params.get("geo_preset"),
            price_max=params.get("price_max"),
            open_now=params.get("open_now", False),
            top_k=MAX_RESTAURANTS,
        )

        compressed = compress_results(results)

        logger.debug(f"RESULTS: {len(compressed)} restaurants")

        return {
            "count": len(compressed),
            "restaurants": compressed
        }

    except Exception as e:
        logger.error(f"SEARCH ERROR: {e}")
        return {"error": str(e)}


def extract_text(response) -> str:
    """Extract text content from Claude response."""
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


# === AGENT LOOP ===


def run(message: str, history: list = None) -> tuple[str, list]:
    """Run agent. Returns (response, updated_history)."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": message})

    logger.info(f"USER: {message}")

    for iteration in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # No tool calls — return text response
        if response.stop_reason == "end_turn":
            text = extract_text(response)
            messages.append({"role": "assistant", "content": response.content})

            logger.info(f"RESPONSE: {text[:200]}...")
            logger.debug(f"FULL RESPONSE: {text}")

            return text, messages

        # Handle tool calls
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"TOOL: {block.name}({json.dumps(block.input, ensure_ascii=False)})")

                    if block.name == "search_restaurants":
                        result = execute_search(block.input)
                    else:
                        result = {"error": f"Unknown tool: {block.name}"}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })

            messages.append({"role": "user", "content": tool_results})

    logger.warning("MAX_ITERATIONS reached")
    return "Не удалось обработать запрос. Попробуйте переформулировать.", messages


# === CLI ===


def main():
    """Interactive CLI or single query."""
    parser = argparse.ArgumentParser(description="Restaurant recommendation agent")
    parser.add_argument("query", nargs="?", help="Single query")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    args = parser.parse_args()

    if args.interactive:
        print("Бот для поиска ресторанов в Бишкеке")
        print("Введите /exit для выхода\n")

        history = []
        while True:
            try:
                user = input("Вы: ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user or user == "/exit":
                break

            response, history = run(user, history)
            print(f"\nБот: {response}\n")
    else:
        query = args.query or "Где вкусный плов?"
        response, _ = run(query)
        print(response)


if __name__ == "__main__":
    main()
