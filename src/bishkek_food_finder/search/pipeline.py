"""Search pipeline for restaurant discovery.

Handles semantic search, geo filtering, trust-weighted scoring.

Run: uv run python -m bishkek_food_finder.search.pipeline "уютное место"
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2

from sentence_transformers import SentenceTransformer
import chromadb

DB_PATH = "data/bishkek.db"
CHROMA_PATH = "data/chroma"
COLLECTION_NAME = "reviews"
MODEL_NAME = "cointegrated/rubert-tiny2"

# === CONFIG ===

SENTIMENT = {1: -1.0, 2: -0.5, 3: 0.0, 4: 0.5, 5: 1.0}
MIN_SIMILARITY = 0.7

GEO_PRESETS = {
    "walking": {"max_km": 3, "decay": 0.4},
    "nearby": {"max_km": 5, "decay": 0.2},
    "driving": {"max_km": 10, "decay": 0.0},
    "city_wide": {"max_km": None, "decay": 0.0},
}

# === LAZY LOADING ===

_model = None
_collection = None


def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        _collection = client.get_collection(COLLECTION_NAME)
    return _collection


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
    n_results: int = 500,
    restaurant_ids: set = None,
) -> list[dict]:
    """Search Chroma for similar reviews."""
    model = get_model()
    collection = get_collection()

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


# === SCORING ===

def score_reviews(conn, chroma_results: list[dict]) -> list[dict]:
    """Join with SQLite, compute scores."""
    if not chroma_results:
        return []

    ids = [r["id"] for r in chroma_results]
    placeholders = ",".join("?" * len(ids))

    rows = conn.execute(f"""
        SELECT
            r.id,
            r.restaurant_id,
            r.text,
            r.rating,
            rest.name,
            rest.address,
            rest.lat,
            rest.lon,
            rest.rating as rating_2gis,
            rest.reviews_count,
            rest.category,
            rest.cuisine,
            rest.avg_price_som,
            rest.schedule,
            rt.base_trust * rt.burst * rt.recency as trust,
            rs.weighted_rating as rating_trusted,
            rs.trusted_review_count
        FROM reviews r
        JOIN restaurants rest ON r.restaurant_id = rest.id
        JOIN review_trust rt ON r.id = rt.review_id
        LEFT JOIN restaurant_stats rs ON r.restaurant_id = rs.restaurant_id
        WHERE r.id IN ({placeholders})
    """, ids).fetchall()

    by_id = {row["id"]: dict(row) for row in rows}
    sim_by_id = {r["id"]: r["similarity"] for r in chroma_results}

    scored = []
    for review_id, similarity in sim_by_id.items():
        review = by_id.get(review_id)
        if not review:
            continue

        trust = review["trust"] or 0.0
        sentiment = SENTIMENT.get(review["rating"], 0.0)
        score = similarity * trust * sentiment

        scored.append({
            **review,
            "similarity": similarity,
            "score": score,
        })

    return scored


def aggregate_by_restaurant(scored_reviews: list[dict]) -> list[dict]:
    """Group by restaurant, sum scores, collect reviews."""
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
                "link": f"https://2gis.kg/bishkek/firm/{rest_id}",
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
    geo_preset: str,
) -> list[dict]:
    """Apply geo decay and add distance. Decay only for 'walking'."""
    preset = GEO_PRESETS.get(geo_preset, GEO_PRESETS["city_wide"])
    max_km = preset["max_km"]
    decay = preset["decay"]

    lat, lon = location

    result = []
    for r in restaurants:
        if r["lat"] is None or r["lon"] is None:
            continue

        dist = haversine_km(lat, lon, r["lat"], r["lon"])

        if max_km and dist > max_km:
            continue

        r["distance_km"] = round(dist, 2)

        if decay > 0 and max_km:
            geo_factor = max(0, 1 - decay * dist / max_km)
            r["score"] *= geo_factor

        result.append(r)

    return sorted(result, key=lambda x: x["score"], reverse=True)


# === MAIN PIPELINE ===

def search(
    query: str,
    location: tuple = None,
    geo_preset: str = None,
    price_max: int = None,
    open_now: bool = False,
    n_reviews: int = 500,
    top_k: int = 10,
) -> list[dict]:
    """Main search pipeline."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Get filtered restaurant IDs
    max_km = None
    if geo_preset:
        max_km = GEO_PRESETS.get(geo_preset, {}).get("max_km")

    restaurant_ids = get_filtered_restaurants(
        conn,
        location=location,
        max_km=max_km,
        price_max=price_max,
        open_now=open_now,
    )

    # 2. Chroma search
    chroma_results = search_chroma(query, n_reviews, restaurant_ids)

    # 3. Score reviews
    scored = score_reviews(conn, chroma_results)

    # 4. Aggregate by restaurant
    restaurants = aggregate_by_restaurant(scored)

    # 5. Apply geo decay
    if location:
        restaurants = apply_geo_decay(restaurants, location, geo_preset or "city_wide")

    conn.close()

    return restaurants[:top_k]


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
    parser = argparse.ArgumentParser(description="Search restaurants")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--top", type=int, default=10, help="Number of results")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lon", type=float, help="Longitude")
    parser.add_argument("--geo", choices=["walking", "nearby", "driving", "city_wide"], help="Geo preset")
    parser.add_argument("--price-max", type=int, help="Max price filter")
    parser.add_argument("--open-now", action="store_true", help="Only open restaurants")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    location = (args.lat, args.lon) if args.lat and args.lon else None

    results = search(
        query=args.query,
        location=location,
        geo_preset=args.geo,
        price_max=args.price_max,
        open_now=args.open_now,
        top_k=args.top,
    )

    print_results(results, json_output=args.json)


if __name__ == "__main__":
    main()
