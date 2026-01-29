"""Search pipeline for restaurant discovery.

Handles semantic search, geo filtering, trust-weighted scoring.

Run: uv run python -m bishkek_food_finder.search.pipeline "уютное место"
     uv run python -m bishkek_food_finder.search.pipeline "вкусный плов" --city almaty
"""

import argparse
import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2

from sentence_transformers import SentenceTransformer
import chromadb

from bishkek_food_finder.config import CITIES, get_city_config

logger = logging.getLogger(__name__)

COLLECTION_NAME = "reviews"
MODEL_NAME = "cointegrated/rubert-tiny2"

# === CONFIG ===

SENTIMENT = {1: -1.0, 2: -0.5, 3: 0.0, 4: 0.5, 5: 1.0}
MIN_SIMILARITY = 0.7

# === LAZY LOADING ===

_model = None
_collections = {}  # city -> collection


def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def get_collection(city: str = "bishkek"):
    global _collections
    if city not in _collections:
        city_config = get_city_config(city)
        client = chromadb.PersistentClient(path=city_config['chroma_path'])
        _collections[city] = client.get_collection(COLLECTION_NAME)
    return _collections[city]


# === GEO HELPERS ===

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in km between two points."""
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def is_open_now(schedule_json: str) -> bool:
    """Check if restaurant is open based on schedule JSON."""
    if not schedule_json:
        return True  # Assume open if no schedule

    try:
        schedule = json.loads(schedule_json)
        now = datetime.now()
        day_name = now.strftime("%a")  # Mon, Tue, etc.

        day_schedule = schedule.get(day_name, {})
        hours = day_schedule.get("working_hours", [])

        if not hours:
            return False

        current_time = now.strftime("%H:%M")
        for period in hours:
            if period.get("from", "00:00") <= current_time <= period.get("to", "23:59"):
                return True
        return False
    except (json.JSONDecodeError, KeyError):
        return True  # Assume open on parse error


def simplify_schedule(schedule_json: str) -> dict:
    """Convert schedule JSON to simple format for display."""
    if not schedule_json:
        return {}

    try:
        schedule = json.loads(schedule_json)
        result = {}
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for day in day_names:
            data = schedule.get(day)
            if isinstance(data, dict):
                hours = data.get("working_hours", [])
                if hours:
                    result[day] = f"{hours[0].get('from', '?')}-{hours[0].get('to', '?')}"
        return result
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


# === SQL FILTERS ===

def get_filtered_restaurants(
    conn,
    location: tuple = None,
    max_km: float = None,
    price_max: int = None,
    open_now: bool = False,
) -> set[str] | None:
    """Get restaurant IDs matching filters. Returns None if no filters."""
    conditions = []
    params = []

    if price_max:
        conditions.append("avg_price_som <= ?")
        params.append(price_max)

    if location and max_km:
        lat, lon = location
        delta = max_km / 111.0  # Approximate degrees
        conditions.append("lat BETWEEN ? AND ?")
        conditions.append("lon BETWEEN ? AND ?")
        params.extend([lat - delta, lat + delta, lon - delta, lon + delta])

    if not conditions and not open_now:
        return None

    query = "SELECT id, schedule FROM restaurants"
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"

    rows = conn.execute(query, params).fetchall()

    if open_now:
        return {row['id'] for row in rows if is_open_now(row['schedule'])}
    return {row['id'] for row in rows}


# === CHROMA SEARCH ===

def search_chroma(
    query: str,
    city: str = "bishkek",
    n_results: int = 500,
    restaurant_ids: set = None,
) -> list[dict]:
    """Search Chroma for similar reviews."""
    model = get_model()
    collection = get_collection(city)

    query_embedding = model.encode(query).tolist()

    where = None
    if restaurant_ids:
        where = {"restaurant_id": {"$in": list(restaurant_ids)}}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        where=where,
        include=["metadatas", "distances"]
    )

    output = []
    for id_, meta, dist in zip(results["ids"][0], results["metadatas"][0], results["distances"][0]):
        similarity = 1 - dist
        if similarity >= MIN_SIMILARITY:
            output.append({
                "id": id_,
                "restaurant_id": meta["restaurant_id"],
                "similarity": similarity,
            })

    return output


# === FTS5 KEYWORD SEARCH ===

def build_fts_query(query: str) -> str | None:
    """Convert user query to FTS5 query with prefix matching.

    Strips Russian endings for broader morphological coverage:
    - >= 7 chars: trim 2 ("круассаны" → "круассан*")
    - >= 5 chars: trim 1 ("бургер" → "бурге*")
    - 3-4 chars: keep as-is ("плов" → "плов*")
    - < 3 chars: skip (prepositions)
    """
    words = query.strip().split()
    terms = []
    for w in words:
        w = re.sub(r'[^\w]', '', w)
        if len(w) < 3:
            continue
        if len(w) >= 7:
            stem = w[:len(w) - 2]
        elif len(w) >= 5:
            stem = w[:len(w) - 1]
        else:
            stem = w
        terms.append(f"{stem}*")
    return " OR ".join(terms) if terms else None


def search_fts(
    query: str,
    conn,
    n_results: int = 500,
    restaurant_ids: set = None,
) -> dict[str, float]:
    """Search FTS5 for keyword matches. Returns {review_id: bm25_rank}."""
    fts_query = build_fts_query(query)
    if not fts_query:
        return {}

    sql = """
        SELECT r.id, fts.rank as bm25_rank
        FROM reviews_fts fts
        JOIN reviews r ON r.rowid = fts.rowid
        WHERE reviews_fts MATCH ?
    """
    params = [fts_query]

    if restaurant_ids:
        placeholders = ",".join("?" * len(restaurant_ids))
        sql += f" AND r.restaurant_id IN ({placeholders})"
        params.extend(list(restaurant_ids))

    sql += " ORDER BY fts.rank LIMIT ?"
    params.append(n_results)

    rows = conn.execute(sql, params).fetchall()
    return {row[0]: row[1] for row in rows}


# === SCORING ===

def score_reviews(conn, chroma_results: list[dict], bm25_by_id: dict[str, float] = None) -> list[dict]:
    """Score reviews. Each source uses its own relevance metric."""
    sim_by_id = {r["id"]: r["similarity"] for r in chroma_results}

    fts_rel = {}
    if bm25_by_id:
        max_bm25 = max(abs(v) for v in bm25_by_id.values())
        if max_bm25 > 0:
            fts_rel = {rid: abs(rank) / max_bm25 for rid, rank in bm25_by_id.items()}

    all_ids = list(set(sim_by_id) | set(fts_rel))
    if not all_ids:
        return []

    logger.debug(f"Scoring: {len(sim_by_id)} semantic, {len(fts_rel)} keyword")

    placeholders = ",".join("?" * len(all_ids))
    rows = conn.execute(f"""
        SELECT
            r.id, r.restaurant_id, r.text, r.rating,
            rest.name, rest.address, rest.lat, rest.lon,
            rest.rating as rating_2gis, rest.reviews_count,
            rest.category, rest.cuisine, rest.avg_price_som, rest.schedule,
            rt.base_trust * rt.burst * rt.recency as trust,
            rs.weighted_rating as rating_trusted, rs.trusted_review_count
        FROM reviews r
        JOIN restaurants rest ON r.restaurant_id = rest.id
        JOIN review_trust rt ON r.id = rt.review_id
        LEFT JOIN restaurant_stats rs ON r.restaurant_id = rs.restaurant_id
        WHERE r.id IN ({placeholders})
    """, all_ids).fetchall()

    by_id = {row["id"]: dict(row) for row in rows}

    scored = []
    for review_id in all_ids:
        review = by_id.get(review_id)
        if not review:
            continue

        similarity = sim_by_id.get(review_id, fts_rel.get(review_id, 0))
        trust = review["trust"] or 0.0
        sentiment = SENTIMENT.get(review["rating"], 0.0)

        scored.append({
            **review,
            "similarity": similarity,
            "score": similarity * trust * sentiment,
        })

    return scored


def aggregate_by_restaurant(scored_reviews: list[dict], city: str = "bishkek") -> list[dict]:
    """Group by restaurant, sum scores, collect reviews."""
    city_config = get_city_config(city)
    link_template = city_config['link_template']

    by_restaurant = defaultdict(lambda: {
        "score": 0.0,
        "reviews": [],
        "meta": None,
    })

    for r in scored_reviews:
        rest_id = r["restaurant_id"]
        by_restaurant[rest_id]["score"] += r["score"]
        by_restaurant[rest_id]["reviews"].append({
            "text": r["text"],
            "rating": r["rating"],
            "similarity": r["similarity"],
            "trust": r["trust"],
            "score": r["score"],
        })
        if by_restaurant[rest_id]["meta"] is None:
            by_restaurant[rest_id]["meta"] = {
                "restaurant_id": rest_id,
                "name": r["name"],
                "address": r["address"],
                "link": link_template.format(id=rest_id),
                "rating_2gis": r["rating_2gis"],
                "rating_trusted": r["rating_trusted"],
                "trusted_review_count": r["trusted_review_count"],
                "category": r["category"],
                "cuisine": json.loads(r["cuisine"]) if r["cuisine"] else [],
                "avg_price_som": r["avg_price_som"],
                "reviews_count_2gis": r["reviews_count"],
                "schedule": simplify_schedule(r["schedule"]),
                "lat": r["lat"],
                "lon": r["lon"],
            }

    result = []
    for rest_id, data in by_restaurant.items():
        # Sort reviews by similarity desc
        data["reviews"].sort(key=lambda x: x["similarity"], reverse=True)
        result.append({
            **data["meta"],
            "score": data["score"],
            "reviews": data["reviews"],
        })

    return sorted(result, key=lambda x: x["score"], reverse=True)


# === GEO DECAY ===

def apply_geo_decay(
    restaurants: list[dict],
    location: tuple,
    radius_km: float = None,
) -> list[dict]:
    """Apply geo decay and add distance. Auto-decay for small radii (<=3km)."""
    # Auto-decay: prefer closer places when walking
    decay = 0.4 if radius_km and radius_km <= 3 else 0.0

    lat, lon = location

    result = []
    for r in restaurants:
        if r["lat"] is None or r["lon"] is None:
            continue

        dist = haversine_km(lat, lon, r["lat"], r["lon"])

        if radius_km and dist > radius_km:
            continue

        r["distance_km"] = round(dist, 2)

        if decay > 0 and radius_km:
            geo_factor = max(0, 1 - decay * dist / radius_km)
            r["score"] *= geo_factor

        result.append(r)

    return sorted(result, key=lambda x: x["score"], reverse=True)


# === MAIN PIPELINE ===

def search(
    query: str,
    city: str = "bishkek",
    location: tuple = None,
    radius_km: float = None,
    price_max: int = None,
    open_now: bool = False,
    n_reviews: int = 500,
    top_k: int = 10,
    keyword_only: bool = False,
    semantic_only: bool = False,
) -> list[dict]:
    """Main search pipeline."""
    logger.debug(f"Search: query='{query}', city={city}, location={location}, radius={radius_km}, price_max={price_max}, keyword_only={keyword_only}, semantic_only={semantic_only}")

    city_config = get_city_config(city)
    conn = sqlite3.connect(city_config['db_path'])
    conn.row_factory = sqlite3.Row

    # 1. Get filtered restaurant IDs
    restaurant_ids = get_filtered_restaurants(
        conn,
        location=location,
        max_km=radius_km,
        price_max=price_max,
        open_now=open_now,
    )
    logger.debug(f"Filter: {len(restaurant_ids) if restaurant_ids else 'all'} restaurants")

    # 2. Chroma search (skip if keyword_only)
    if keyword_only:
        chroma_results = []
        logger.debug("Chroma: skipped (keyword_only)")
    else:
        chroma_results = search_chroma(query, city=city, n_results=n_reviews, restaurant_ids=restaurant_ids)
        logger.debug(f"Chroma: {len(chroma_results)} results above {MIN_SIMILARITY} similarity")

    # 2b. FTS5 keyword search (skip if semantic_only)
    if semantic_only:
        bm25_by_id = {}
        logger.debug("FTS5: skipped (semantic_only)")
    else:
        try:
            bm25_by_id = search_fts(query, conn, n_results=n_reviews, restaurant_ids=restaurant_ids)
        except Exception:
            bm25_by_id = {}  # FTS5 table might not exist yet
        logger.debug(f"FTS5: {len(bm25_by_id)} keyword matches")

    # 3. Score reviews (hybrid: semantic + keyword boost)
    scored = score_reviews(conn, chroma_results, bm25_by_id)
    logger.debug(f"Scored: {len(scored)} reviews")

    # 4. Aggregate by restaurant
    restaurants = aggregate_by_restaurant(scored, city=city)
    logger.debug(f"Aggregated: {len(restaurants)} restaurants")

    # 5. Apply geo decay
    if location:
        restaurants = apply_geo_decay(restaurants, location, radius_km)
        logger.debug(f"Geo filtered: {len(restaurants)} restaurants")

    conn.close()

    return restaurants[:top_k]


# === TRANSLITERATION ===

# Cyrillic ↔ Latin lookalikes (visually similar characters)
CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'a',
    'В': 'B', 'в': 'b',  # В looks like B
    'Е': 'E', 'е': 'e',
    'К': 'K', 'к': 'k',
    'М': 'M', 'м': 'm',
    'Н': 'H', 'н': 'h',  # Н looks like H
    'О': 'O', 'о': 'o',
    'Р': 'P', 'р': 'p',  # Р looks like P
    'С': 'C', 'с': 'c',
    'Т': 'T', 'т': 't',
    'У': 'Y', 'у': 'y',  # У looks like Y
    'Х': 'X', 'х': 'x',
}

# Full Cyrillic → Latin transliteration for phonetic matching
CYRILLIC_TRANSLIT = {
    'А': 'A', 'а': 'a', 'Б': 'B', 'б': 'b', 'В': 'V', 'в': 'v',
    'Г': 'G', 'г': 'g', 'Д': 'D', 'д': 'd', 'Е': 'E', 'е': 'e',
    'Ё': 'E', 'ё': 'e', 'Ж': 'Zh', 'ж': 'zh', 'З': 'Z', 'з': 'z',
    'И': 'I', 'и': 'i', 'Й': 'Y', 'й': 'y', 'К': 'K', 'к': 'k',
    'Л': 'L', 'л': 'l', 'М': 'M', 'м': 'm', 'Н': 'N', 'н': 'n',
    'О': 'O', 'о': 'o', 'П': 'P', 'п': 'p', 'Р': 'R', 'р': 'r',
    'С': 'S', 'с': 's', 'Т': 'T', 'т': 't', 'У': 'U', 'у': 'u',
    'Ф': 'F', 'ф': 'f', 'Х': 'Kh', 'х': 'kh', 'Ц': 'Ts', 'ц': 'ts',
    'Ч': 'Ch', 'ч': 'ch', 'Ш': 'Sh', 'ш': 'sh', 'Щ': 'Shch', 'щ': 'shch',
    'Ъ': '', 'ъ': '', 'Ы': 'Y', 'ы': 'y', 'Ь': '', 'ь': '',
    'Э': 'E', 'э': 'e', 'Ю': 'Yu', 'ю': 'yu', 'Я': 'Ya', 'я': 'ya',
}

LATIN_TO_CYRILLIC = {v: k for k, v in CYRILLIC_TO_LATIN.items()}


def transliterate_to_latin(text: str) -> str:
    """Transliterate Cyrillic text to Latin (phonetic)."""
    return ''.join(CYRILLIC_TRANSLIT.get(c, c) for c in text)


def get_search_variants(name: str) -> list[str]:
    """Generate search variants for a name (original + transliterated)."""
    variants = [name]

    # Try Cyrillic → Latin transliteration
    latin = transliterate_to_latin(name)
    if latin != name:
        variants.append(latin)

    # Capitalize first letter variants
    variants = [v[0].upper() + v[1:] if v else v for v in variants]

    return list(set(variants))


# === RESTAURANT LOOKUP ===

def get_restaurant_details(
    city: str = "bishkek",
    name: str = None,
    id: str = None,
    address_hint: str = None,
    max_reviews: int = 100,
    min_trust: float = 0.3,
) -> dict:
    """Look up restaurant by name, ID, or name+address."""
    city_config = get_city_config(city)
    conn = sqlite3.connect(city_config['db_path'])
    conn.row_factory = sqlite3.Row

    # Priority 1: Exact ID lookup
    if id:
        restaurants = conn.execute(
            "SELECT * FROM restaurants WHERE id = ?", (id,)
        ).fetchall()
    # Priority 2: Name + optional address hint
    # Generate search variants (original + transliterated) to handle Latin/Cyrillic mismatches
    elif name:
        name_variants = get_search_variants(name)

        if address_hint:
            # Generate address variants (original, title-cased, uppercase)
            address_variants = [
                address_hint,
                address_hint.title(),  # "сухэ-батора" → "Сухэ-Батора"
                address_hint.capitalize(),  # "сухэ-батора" → "Сухэ-батора"
            ]
            placeholders = " OR ".join(["name LIKE ?" for _ in name_variants])
            address_placeholders = " OR ".join(["address LIKE ?" for _ in address_variants])
            params = [f"%{v}%" for v in name_variants] + [f"%{v}%" for v in address_variants]
            restaurants = conn.execute(f"""
                SELECT * FROM restaurants
                WHERE ({placeholders})
                  AND ({address_placeholders})
            """, params).fetchall()
        else:
            placeholders = " OR ".join(["name LIKE ?" for _ in name_variants])
            params = [f"%{v}%" for v in name_variants]
            restaurants = conn.execute(f"""
                SELECT * FROM restaurants
                WHERE {placeholders}
            """, params).fetchall()
    else:
        conn.close()
        return {"found": False, "message": "Provide name or id"}

    if not restaurants:
        # If address_hint was provided but no match, try without address to show alternatives
        if address_hint and name:
            name_variants = get_search_variants(name)
            placeholders = " OR ".join(["name LIKE ?" for _ in name_variants])
            params = [f"%{v}%" for v in name_variants]
            alternatives = conn.execute(f"""
                SELECT id, name, address FROM restaurants
                WHERE {placeholders}
            """, params).fetchall()

            if alternatives:
                conn.close()
                return {
                    "found": False,
                    "address_not_found": True,
                    "searched_address": address_hint,
                    "alternatives": [
                        {"id": r["id"], "name": r["name"], "address": r["address"]}
                        for r in alternatives
                    ],
                    "message": f"No '{name}' at '{address_hint}', but found {len(alternatives)} other locations. Show them to user with numbers (1, 2, 3...) so they can pick one. When user picks, use get_restaurant with that ID."
                }

        conn.close()
        search_term = id or f"{name} {address_hint or ''}".strip()
        return {
            "found": False,
            "message": f"Restaurant '{search_term}' not found in database. Ask user to send their location or name a known restaurant nearby."
        }

    # Multiple matches by name → return lightweight list, ask user to pick
    # Single match OR lookup by ID → return full details
    if len(restaurants) > 1 and not id:
        conn.close()
        return {
            "found": True,
            "multiple": True,
            "count": len(restaurants),
            "locations": [
                {"id": r["id"], "name": r["name"], "address": r["address"]}
                for r in restaurants
            ],
            "message": f"Found {len(restaurants)} locations. Show numbered list to user and ask which one. When user picks, call get_restaurant with that ID."
        }

    # Single match or ID lookup → return full details
    restaurant = dict(restaurants[0])

    # Get trusted reviews
    reviews = conn.execute("""
        SELECT r.text, r.rating, r.user_name,
               rt.base_trust * rt.burst * rt.recency as trust
        FROM reviews r
        JOIN review_trust rt ON r.id = rt.review_id
        WHERE r.restaurant_id = ?
          AND (rt.base_trust * rt.burst * rt.recency) >= ?
        ORDER BY trust DESC
        LIMIT ?
    """, (restaurant["id"], min_trust, max_reviews)).fetchall()

    # Get stats
    stats = conn.execute(
        "SELECT * FROM restaurant_stats WHERE restaurant_id = ?",
        (restaurant["id"],)
    ).fetchone()

    result = {
        "id": restaurant["id"],
        "name": restaurant["name"],
        "address": restaurant["address"],
        "lat": restaurant["lat"],
        "lon": restaurant["lon"],
        "rating_2gis": restaurant["rating"],
        "rating_trusted": round(stats["weighted_rating"], 2) if stats else None,
        "trusted_review_count": stats["trusted_review_count"] if stats else 0,
        "category": restaurant["category"],
        "cuisine": json.loads(restaurant["cuisine"]) if restaurant["cuisine"] else [],
        "avg_price_som": restaurant["avg_price_som"],
        "link": city_config['link_template'].format(id=restaurant['id']),
        "reviews": [
            {"text": r["text"][:500], "rating": r["rating"], "trust": round(r["trust"], 2)}
            for r in reviews
        ]
    }

    conn.close()

    return {
        "found": True,
        "count": 1,
        "restaurant": result
    }


# === CLI ===

def print_results(results: list[dict], json_output: bool = False):
    """Print search results."""
    if json_output:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if not results:
        print("No results found.")
        return

    print(f"\nFound {len(results)} restaurants\n")
    print("=" * 80)

    for i, r in enumerate(results, 1):
        dist_str = f" • {r['distance_km']}km" if r.get('distance_km') is not None else ""
        print(f"\n{i}. {r['name']} (score: {r['score']:.2f}{dist_str})")
        print(f"   {r['address']}")
        print(f"   ⭐ 2GIS: {r['rating_2gis']} | Trusted: {r['rating_trusted']:.2f} ({r['trusted_review_count']} reviews)")
        print(f"   🔗 {r['link']}")

        for rev in r['reviews'][:3]:
            print(f"\n   [{rev['rating']}★] sim={rev['similarity']:.2f} trust={rev['trust']:.2f}")
            print(f"   {rev['text'][:200]}{'...' if len(rev['text']) > 200 else ''}")

        print("-" * 80)


def main():
    from bishkek_food_finder.log import setup_logging

    parser = argparse.ArgumentParser(description="Search restaurants")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--city", default="bishkek", choices=list(CITIES.keys()), help="City to search")
    parser.add_argument("--top", type=int, default=10, help="Number of results")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lon", type=float, help="Longitude")
    parser.add_argument("--radius", type=float, help="Search radius in km")
    parser.add_argument("--price-max", type=int, help="Max price filter")
    parser.add_argument("--open-now", action="store_true", help="Only open restaurants")
    parser.add_argument("--keyword-only", action="store_true", help="FTS5 keyword search only (skip semantic)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    setup_logging(script_name=f"search_{args.city}")

    city_config = get_city_config(args.city)
    print(f"Searching in {city_config['name']}...\n")

    location = (args.lat, args.lon) if args.lat and args.lon else None

    results = search(
        query=args.query,
        city=args.city,
        location=location,
        radius_km=args.radius,
        price_max=args.price_max,
        open_now=args.open_now,
        top_k=args.top,
        keyword_only=args.keyword_only,
    )

    print_results(results, json_output=args.json)


if __name__ == "__main__":
    main()
