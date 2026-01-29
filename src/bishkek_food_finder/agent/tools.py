"""Tool definitions and execution for the agent."""

import json

from bishkek_food_finder.search.pipeline import search, get_restaurant_details
from bishkek_food_finder.log import setup_service_logging

logger = setup_service_logging("agent")

# === CONSTANTS ===

MAX_RESTAURANTS = 10
MAX_REVIEWS = 30
N_REVIEWS = 1000

# === TOOL DEFINITIONS ===

TOOLS = [{
    "name": "search_restaurants",
    "description": """Search for restaurants. Hybrid semantic + keyword search across 294k reviews.
Returns restaurants ranked by: relevance × trust × sentiment.
Also returns keyword_restaurants — additional places found by exact keyword matching (not in main results).
Check keyword_restaurants for specific food/dish queries — they may have more relevant matches.""",
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
    "description": """Look up restaurant by name, ID, or name+address. Returns details + trusted reviews.
Use ID for exact match from previous search results. Use address_hint to narrow down (e.g., name="винтаж", address_hint="токомбаева").""",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Restaurant name (partial match)"},
            "id": {"type": "string", "description": "Exact restaurant ID from previous search results"},
            "address_hint": {"type": "string", "description": "Address fragment to narrow results (e.g., 'токомбаева')"},
            "max_reviews": {"type": "integer", "description": "Max reviews to return. Default: 100"}
        }
    }
}]


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


def summarize_tool_result(name: str, result: dict) -> str:
    """One-line summary of tool result for logging."""
    if "error" in result:
        return f"ERROR: {result['error']}"
    if name == "search_restaurants":
        count = result.get("count", 0)
        if count == 0:
            return "0 restaurants found"
        names = [r["name"] for r in result.get("restaurants", [])[:5]]
        return f"{count} restaurants: {', '.join(names)}{'...' if count > 5 else ''}"
    if name == "get_restaurant":
        if not result.get("found"):
            return f"not found: {result.get('message', '')[:80]}"
        if result.get("multiple"):
            return f"{result['count']} locations found"
        r = result.get("restaurant", {})
        return f"found: {r.get('name', '?')} ({r.get('trusted_review_count', 0)} trusted reviews)"
    return json.dumps(result, ensure_ascii=False)[:100]


# === TOOL EXECUTION ===

def execute_search(params: dict, city: str = "bishkek") -> dict:
    """Execute search pipeline and return compressed results.

    Runs both hybrid (semantic + keyword) and keyword-only searches.
    Returns deduplicated keyword_restaurants alongside main results.
    """
    try:
        location = (params["latitude"], params["longitude"]) if params.get("latitude") else None
        search_kwargs = dict(
            query=params["query"],
            city=city,
            location=location,
            radius_km=params.get("radius_km"),
            price_max=params.get("price_max"),
            open_now=params.get("open_now", False),
            top_k=MAX_RESTAURANTS,
        )

        # Semantic search (Chroma only)
        results = search(**search_kwargs, semantic_only=True, n_reviews=N_REVIEWS)

        # Keyword-only search (FTS5 only, skip Chroma)
        try:
            keyword_results = search(**search_kwargs, keyword_only=True, n_reviews=N_REVIEWS)
        except Exception:
            keyword_results = []

        # Deduplicate: only keep keyword restaurants NOT in hybrid results
        hybrid_ids = {r["restaurant_id"] for r in results}
        keyword_new = [r for r in keyword_results if r["restaurant_id"] not in hybrid_ids]

        compressed = compress_results(results)
        keyword_compressed = compress_results(keyword_new)

        return {
            "count": len(compressed),
            "restaurants": compressed,
            "keyword_count": len(keyword_compressed),
            "keyword_restaurants": keyword_compressed,
        }

    except Exception as e:
        logger.error(f"SEARCH ERROR: {e}")
        return {"error": str(e)}


def execute_get_restaurant(params: dict, city: str = "bishkek") -> dict:
    """Execute get_restaurant_details with params from tool call."""
    return get_restaurant_details(
        city=city,
        name=params.get("name"),
        id=params.get("id"),
        address_hint=params.get("address_hint"),
        max_reviews=params.get("max_reviews", 100)
    )
