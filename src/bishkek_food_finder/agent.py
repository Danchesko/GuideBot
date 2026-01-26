"""Agent for restaurant recommendations.

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

load_dotenv()

from bishkek_food_finder.search.pipeline import search, get_restaurant_details

# === CONFIG ===

MODEL = "claude-opus-4-5-20251101"
MAX_ITERATIONS = 5
MAX_RESTAURANTS = 30
MAX_REVIEWS = 30

# === LOGGING ===

Path("logs").mkdir(exist_ok=True)
logger = logging.getLogger("bishkek_food_finder.agent")
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.FileHandler("logs/agent.log"))
sh = logging.StreamHandler(); sh.setLevel(logging.WARNING); logger.addHandler(sh)

# === CLIENT ===

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise EnvironmentError("ANTHROPIC_API_KEY not set")

client = Anthropic()


# === HELPERS ===

def compress_results(results: list[dict]) -> list[dict]:
    """Compress search results for LLM consumption."""
    return [{
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
            {"text": rev["text"][:300], "rating": rev["rating"], "trust": round(rev["trust"], 2)}
            for rev in r["reviews"][:MAX_REVIEWS]
        ]
    } for r in results[:MAX_RESTAURANTS]]


def execute_search(params: dict) -> dict:
    """Execute search pipeline and return compressed results."""
    try:
        location = (params["latitude"], params["longitude"]) if params.get("latitude") else None

        results = search(
            query=params["query"],
            location=location,
            radius_km=params.get("radius_km"),
            price_max=params.get("price_max"),
            open_now=params.get("open_now", False),
            top_k=MAX_RESTAURANTS,
        )

        compressed = compress_results(results)
        return {"count": len(compressed), "restaurants": compressed}

    except Exception as e:
        logger.error(f"SEARCH ERROR: {e}")
        return {"error": str(e)}


# === AGENT LOOP ===

def run(message: str, history: list = None) -> tuple[str, list, dict | None]:
    """Run agent. Returns (response, updated_history, last_search_results)."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": message})
    logger.info(f"USER: {message}")
    last_results = None

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # Final response
        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            messages.append({"role": "assistant", "content": response.content})
            logger.info(f"RESPONSE: {text[:200]}...")
            return text, messages, last_results

        # Tool calls
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"TOOL: {block.name}({json.dumps(block.input, ensure_ascii=False)})")

                    if block.name == "search_restaurants":
                        result = execute_search(block.input)
                    elif block.name == "get_restaurant":
                        result = get_restaurant_details(
                            name=block.input["name"],
                            max_reviews=block.input.get("max_reviews", 50)
                        )
                    else:
                        result = {"error": "Unknown tool"}

                    logger.debug(f"TOOL_RESULT: {json.dumps(result, ensure_ascii=False)}")
                    last_results = result
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })

            messages.append({"role": "user", "content": tool_results})

    logger.warning("MAX_ITERATIONS reached")
    return "Не удалось обработать запрос.", messages, None


# === CLI ===

def main():
    """Interactive CLI or single query."""
    parser = argparse.ArgumentParser(description="Restaurant recommendation agent")
    parser.add_argument("query", nargs="?", help="Single query")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    args = parser.parse_args()

    if args.interactive:
        print("Бот для поиска ресторанов в Бишкеке\nВведите /exit для выхода\n")
        history = []
        while True:
            try:
                user = input("Вы: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user or user == "/exit":
                break
            response, history, _ = run(user, history)
            print(f"\nБот: {response}\n")
    else:
        response, _, _ = run(args.query or "Где вкусный плов?")
        print(response)


# === SYSTEM PROMPT ===

SYSTEM_PROMPT = """Ты — бот для поиска ресторанов в Бишкеке.

## Возможности
- Поиск по кухне, атмосфере, блюдам, цене, локации
- Доступ к 294,000 реальных отзывов (с фильтрацией фейков)
- Понимание контекста: "уютное место для свидания"

## Как искать
1. Используй search_restaurants для поиска ресторанов по критериям
2. Используй get_restaurant для вопросов о КОНКРЕТНОМ месте:
   - "что поесть в Навват" → get_restaurant("Навват")
   - "как тебе Винтаж?" → get_restaurant("Винтаж")
   - "рядом с La Maison" → get_restaurant("La Maison") → использовать lat/lon для search_restaurants
3. Если get_restaurant вернул несколько кандидатов — уточни у user какой именно
4. Формулируй query конкретно на русском
5. Используй radius_km когда user упоминает локацию:
   - "рядом", "близко" → 1
   - "пешком", "5 минут пешком" → 2
   - "на машине", "недалеко" → 5
   - "в радиусе X км" → X
6. Используй price_max когда user говорит "недорого" (~500), "средний бюджет" (~1500)
7. ПРОВЕРЯЙ отзывы — поиск семантический, может найти ложные совпадения

## Когда уточнять
- "хочу поесть" → спроси кухню, бюджет, повод
- "рядом" без локации → спроси где находится
- Несколько интерпретаций → уточни

## Когда НЕ уточнять
- Конкретный запрос: "лучший плов" — сразу ищи
- User уже дал контекст

## Формат ответа
- Сначала параметры поиска (🔍 Ищу, 📍 Радиус, 💰 Бюджет)
- По умолчанию 3 места (или сколько попросит user)
- Топ-3 с медалями 🥇🥈🥉, остальные с номерами (4. 5. ...)
- Рейтинг "(real)" = очищенный от фейков
- После адреса — твоё мнение об этом месте (1 предложение, на основе ВСЕХ отзывов)
- Цитаты из отзывов — показывай как есть
- Ссылка — последняя (ничего после неё!)
- В конце "Ещё хорошие варианты" если есть ещё достойные места
- В самом конце hint про локацию (если user не использовал локацию)

## Параметры поиска (в начале ответа)
Показывай ТОЧНО те параметры, что передал в search_restaurants.
Формат — code block:
```
🔍 Ищу: вкусный плов
📍 Радиус: 3 км
💰 Бюджет: любой
```

## Пример ответа

```
🔍 Ищу: недооцененное место изысканная кухня
📍 Радиус: весь Бишкек
💰 Бюджет: любой
```

Места с уникальной атмосферой и изысканной кухней в Бишкеке

🥇 La Maison du voyageur ⭐️ 4.31 (real) • ~400 сом
   📍 улица Орозбекова, 19

   Французский ресторан с невысоким ценником — редкость. 299 проверенных отзывов, при этом многие не слышали о нём.

   ✍️ «Настоящее место с душой! Много милых и интересных деталей в интерьере»
   ✍️ «Живая музыка скрипача, уютная атмосфера»
   ✍️ «Интересная задумка со вторым уровнем, идеально для свиданий»

   Открыть в 2GIS (https://2gis.kg/bishkek/firm/70000001031466679)

───

🥈 Винтаж ⭐️ 4.72 (real) • ~1500 сом
   📍 проспект Чынгыза Айтматова, 299/7а

   Винное место с европейской кухней. 94 проверенных отзыва, рейтинг почти 4.8 — но мало кто знает.

   ✍️ «Для долгих душевных разговоров — идеальное место»
   ✍️ «Место потрясающей красоты, чувствуется атмосфера уюта»

   Открыть в 2GIS (https://2gis.kg/bishkek/firm/70000001068490439)

───

🥉 Iwa ⭐️ 3.34 (real) • ~3500 сом
   📍 Киевская улица, 148

   Японский ресторан/бар с видами. Рейтинг занижен спорными отзывами, но те, кто понимает — ценят.

   ✍️ «Своя атмосфера, определённый вайб и кайф»
   ✍️ «Шикарная атмосфера, обалденные виды»

   Открыть в 2GIS (https://2gis.kg/bishkek/firm/70000001042571832)

───

Ещё хорошие варианты:
- Красный дом — аутентичная китайская кухня с атмосферой, ~1000 сом ⭐️ 3.76
- Cafe de Paris — тихое французское кафе с пекарней, ~1500 сом ⭐️ 3.72

───

📍 Хочешь найти что-то рядом? Отправь локацию.

## Заголовок
- Описательный, без форматирования
- Примеры: "Места с уникальной атмосферой", "Плов рядом", "Кофейни в центре"

## Стиль
- Отвечай кратко, без воды и клише
- Никаких "Отличный выбор!", "С удовольствием помогу!", "Конечно!"
- Просто результаты — чисто и по делу
- Если ничего не найдено — скажи прямо, предложи расширить критерии

## Важно
- Отвечай ТОЛЬКО на русском
- Не придумывай отзывы — используй только из результатов поиска
- Вопросы не про рестораны — вежливо откажи
"""

# === TOOL DEFINITION ===

TOOLS = [{
    "name": "search_restaurants",
    "description": """Search for restaurants in Bishkek. Semantic search across 294k reviews.
Returns restaurants ranked by: relevance × trust × sentiment.
IMPORTANT: Search is semantic, not keyword. YOU must verify reviews match what user wants.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query in Russian"},
            "latitude": {"type": "number", "description": "User's latitude"},
            "longitude": {"type": "number", "description": "User's longitude"},
            "radius_km": {"type": "number", "description": "Search radius in km"},
            "price_max": {"type": "integer", "description": "Max price in SOM"},
            "open_now": {"type": "boolean", "description": "Only open restaurants"}
        },
        "required": ["query"]
    }
}, {
    "name": "get_restaurant",
    "description": """Look up a specific restaurant by name. Returns details + all trusted reviews.
Use when user asks about a SPECIFIC place: what to eat there, opinion, or to get its location for nearby search.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Restaurant name (partial match OK)"},
            "max_reviews": {"type": "integer", "description": "Max reviews to return. Default: 50"}
        },
        "required": ["name"]
    }
}]


if __name__ == "__main__":
    main()
