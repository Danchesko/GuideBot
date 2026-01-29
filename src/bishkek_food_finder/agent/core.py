"""Agent core: LLM loop, history management, CLI."""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

from bishkek_food_finder.config import CITIES, get_city_config
from bishkek_food_finder.log import setup_service_logging
from .tools import TOOLS, execute_search, execute_get_restaurant, summarize_tool_result

# === CONFIG ===

MODEL = "claude-haiku-4-5-20251001"
MAX_ITERATIONS = 7
MAX_HISTORY_MESSAGES = 30

# === LOGGING ===

# Default logger for CLI usage
_default_logger = setup_service_logging("agent")

# Cache of per-user loggers
_user_loggers = {}


def _get_logger(user_id: int | str = None):
    """Get logger for user. Creates per-user log file if user_id provided."""
    if user_id is None:
        return _default_logger

    if user_id not in _user_loggers:
        _user_loggers[user_id] = setup_service_logging(f"agent_{user_id}")
    return _user_loggers[user_id]

# === CLIENT ===

if not os.environ.get("LLM_API_KEY"):
    raise EnvironmentError("LLM_API_KEY not set")

client = Anthropic(api_key=os.environ["LLM_API_KEY"])

# === PROMPT ===

_prompt_path = Path(__file__).parent / "prompt.txt"
SYSTEM_PROMPT_TEMPLATE = _prompt_path.read_text(encoding="utf-8")


# === HISTORY HELPERS ===

def _trim_tool_result(result_json: str) -> str:
    """Compact tool result for history. Strip reviews, keep identifiers."""
    try:
        result = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json

    if "restaurants" in result:
        trimmed = {
            "count": result.get("count", 0),
            "restaurants": [
                {k: r[k] for k in ("name", "address", "score", "avg_price_som") if k in r}
                for r in result.get("restaurants", [])[:5]
            ],
        }
        kw = result.get("keyword_restaurants", [])
        if kw:
            trimmed["keyword_count"] = result.get("keyword_count", len(kw))
            trimmed["keyword_restaurants"] = [
                {k: r[k] for k in ("name", "address", "score") if k in r}
                for r in kw[:3]
            ]
        return json.dumps(trimmed, ensure_ascii=False)

    if result.get("found") and not result.get("multiple") and "restaurant" in result:
        r = result["restaurant"]
        trimmed = {
            "found": True, "count": 1,
            "restaurant": {k: r.get(k) for k in ("id", "name", "address", "lat", "lon", "rating_trusted", "trusted_review_count", "avg_price_som")}
        }
        return json.dumps(trimmed, ensure_ascii=False)

    return result_json


def _trim_history(messages: list) -> list:
    """Replace tool results with compact summaries for history storage."""
    trimmed = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            new_content = []
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    new_content.append({
                        **block,
                        "content": _trim_tool_result(block.get("content", ""))
                    })
                else:
                    new_content.append(block)
            trimmed.append({**msg, "content": new_content})
        else:
            trimmed.append(msg)
    return trimmed


# === AGENT LOOP ===

def run(message: str, history: list = None, city: str = "bishkek",
        on_tool_call: callable = None, user_id: int | str = None) -> tuple[str, list, dict | None]:
    """Run agent. Returns (response, updated_history, last_search_results)."""
    logger = _get_logger(user_id)
    city_config = get_city_config(city)
    city_name = city_config['name']
    run_start = time.time()

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(city_name=city_name)

    messages = list(history) if history else []

    # Sliding window: cap history to prevent context overflow
    if len(messages) > MAX_HISTORY_MESSAGES:
        messages = messages[-MAX_HISTORY_MESSAGES:]
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

    messages.append({"role": "user", "content": message})
    logger.info(f"USER ({city}): {message}")
    last_results = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"--- Iteration {iteration}/{MAX_ITERATIONS} ---")

        # LLM call
        t0 = time.time()
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages
        )
        llm_time = time.time() - t0
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        logger.info(f"LLM: {llm_time:.1f}s | {tokens_in}in/{tokens_out}out tokens | stop={response.stop_reason}")

        # Final response
        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            messages.append({"role": "assistant", "content": response.content})
            total_time = time.time() - run_start
            logger.info(f"RESPONSE ({len(text)} chars):\n{text}")
            logger.info(f"DONE: {iteration} iteration(s), {total_time:.1f}s total\n")
            return text, _trim_history(messages), last_results

        # Tool calls
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            # Log thinking (text blocks before tool calls)
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    text = block.text.replace('\n', ' ')
                    if len(text) > 500:
                        text = text[:500] + f"... ({len(block.text)} chars total)"
                    logger.info(f"THINKING: {text}")

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"TOOL_CALL: {block.name}({json.dumps(block.input, ensure_ascii=False)})")

                    if on_tool_call:
                        try:
                            on_tool_call(block.name, block.input)
                        except Exception:
                            pass

                    t0 = time.time()
                    if block.name == "search_restaurants":
                        result = execute_search(block.input, city=city)
                    elif block.name == "get_restaurant":
                        result = execute_get_restaurant(block.input, city=city)
                    else:
                        result = {"error": "Unknown tool"}
                    tool_time = time.time() - t0

                    summary = summarize_tool_result(block.name, result)
                    logger.info(f"TOOL_RESULT ({tool_time:.1f}s): {summary}")
                    logger.debug(f"TOOL_RESULT_FULL: {json.dumps(result, ensure_ascii=False)}")

                    last_results = result
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })

            messages.append({"role": "user", "content": tool_results})

    total_time = time.time() - run_start
    logger.warning(f"MAX_ITERATIONS reached after {total_time:.1f}s")
    return "Не удалось обработать запрос.", _trim_history(messages), None


# === CLI ===

def main():
    """Interactive CLI or single query."""
    parser = argparse.ArgumentParser(description="Restaurant recommendation agent")
    parser.add_argument("query", nargs="?", help="Single query")
    parser.add_argument("--city", default="bishkek", choices=list(CITIES.keys()), help="City to search")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    args = parser.parse_args()

    city_config = get_city_config(args.city)

    if args.interactive:
        print(f"Бот для поиска ресторанов в городе {city_config['name']}\nВведите /exit для выхода\n")
        history = []
        while True:
            try:
                user = input("Вы: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user or user == "/exit":
                break
            response, history, _ = run(user, history, city=args.city)
            print(f"\nБот: {response}\n")
    else:
        response, _, _ = run(args.query or "Где вкусный плов?", city=args.city)
        print(response)


if __name__ == "__main__":
    main()
