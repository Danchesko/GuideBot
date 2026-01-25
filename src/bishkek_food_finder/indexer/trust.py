"""Compute trust scores and restaurant stats.

Creates two tables:
- review_trust: per-review trust components (base_trust, burst, recency)
- restaurant_stats: per-restaurant aggregates (weighted_rating, confidence_score)

Run: uv run python -m bishkek_food_finder.indexer.trust
"""

import sqlite3
import re
from math import log
from datetime import datetime
from collections import defaultdict

DB_PATH = "data/bishkek.db"

# === TRUST CONFIG ===

# Burst detection: >10x→0.1, >5x→0.3, >3x→0.5, normal→1.0
BURST_THRESHOLDS = [(10, 0.1), (5, 0.3), (3, 0.5)]

# Recency: linear decay 2% per month, floor at 0.5
RECENCY_DECAY = 0.02
RECENCY_FLOOR = 0.5

# === RESTAURANT STATS CONFIG ===

# Bayesian average: minimum reviews for full confidence
BAYESIAN_M = 10

# Trusted review threshold (trust >= this counts as "trusted")
TRUSTED_THRESHOLD = 0.3


# === TRUST FUNCTIONS ===

def get_base_trust(user_reviews_count) -> float:
    """1→0.1, 2-3→0.3, 4-6→0.5, 7-10→0.7, 11+→1.0"""
    count = user_reviews_count or 1
    if count == 1:
        return 0.1
    if count <= 3:
        return 0.3
    if count <= 6:
        return 0.5
    if count <= 10:
        return 0.7
    return 1.0


def get_recency(review_date: datetime, reference_date: datetime) -> float:
    """Linear decay from reference date, floor at 0.5."""
    months_old = (reference_date - review_date).days / 30
    return max(RECENCY_FLOOR, 1.0 - RECENCY_DECAY * months_old)


def get_burst(day_count: int, baseline: float) -> float:
    """Return burst penalty based on how much day_count exceeds baseline."""
    for mult, score in BURST_THRESHOLDS:
        if day_count > mult * baseline:
            return score
    return 1.0


def parse_date(s: str) -> datetime:
    """Parse ISO date string from SQLite."""
    s = s.replace('Z', '+00:00')
    s = re.sub(r'\.(\d+)', lambda m: '.' + m.group(1)[:6].ljust(6, '0'), s)
    return datetime.fromisoformat(s)


# === COMPUTATION ===

def compute_review_trust(reviews: list[dict], reference_date: datetime) -> list[tuple]:
    """Compute trust components for each review. Returns list of (id, base, burst, recency)."""

    # Group by restaurant for burst detection
    by_restaurant = defaultdict(list)
    for r in reviews:
        by_restaurant[r['restaurant_id']].append(r)

    # Compute burst baselines and day counts per restaurant
    burst_scores = {}
    for rest_reviews in by_restaurant.values():
        if len(rest_reviews) < 2:
            for r in rest_reviews:
                burst_scores[r['id']] = 1.0
            continue

        dates = [r['date'] for r in rest_reviews]
        days_span = (max(dates) - min(dates)).days + 1
        baseline = max(len(rest_reviews) / days_span, 1.0)

        daily_counts = defaultdict(int)
        for r in rest_reviews:
            daily_counts[r['date'].date()] += 1

        for r in rest_reviews:
            burst_scores[r['id']] = get_burst(daily_counts[r['date'].date()], baseline)

    # Build results
    return [
        (
            r['id'],
            get_base_trust(r['user_reviews_count']),
            burst_scores[r['id']],
            get_recency(r['date'], reference_date),
        )
        for r in reviews
    ]


def compute_restaurant_stats(conn, global_avg: float) -> list[tuple]:
    """Compute restaurant-level stats from review_trust + reviews tables."""

    rows = conn.execute("""
        SELECT
            r.restaurant_id,
            r.rating,
            rt.base_trust * rt.burst * rt.recency as trust
        FROM reviews r
        JOIN review_trust rt ON r.id = rt.review_id
    """).fetchall()

    # Aggregate per restaurant
    by_restaurant = defaultdict(list)
    for row in rows:
        by_restaurant[row['restaurant_id']].append({
            'rating': row['rating'],
            'trust': row['trust'],
        })

    results = []
    for restaurant_id, reviews in by_restaurant.items():
        # Weighted rating = SUM(rating × trust) / SUM(trust)
        sum_weighted = sum(r['rating'] * r['trust'] for r in reviews)
        sum_trust = sum(r['trust'] for r in reviews)
        weighted_rating = sum_weighted / sum_trust if sum_trust > 0 else 0

        # Trusted review count
        trusted_count = sum(1 for r in reviews if r['trust'] >= TRUSTED_THRESHOLD)

        # Bayesian confidence score
        # score = (count × rating + m × global_avg) / (count + m)
        confidence_score = (
            (trusted_count * weighted_rating + BAYESIAN_M * global_avg) /
            (trusted_count + BAYESIAN_M)
        )

        results.append((restaurant_id, weighted_rating, trusted_count, confidence_score))

    return results


# === MAIN ===

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # === PHASE 1: Review Trust ===
    print("=== Review Trust ===")

    conn.execute("DROP TABLE IF EXISTS review_trust")
    conn.execute("""
        CREATE TABLE review_trust (
            review_id TEXT PRIMARY KEY,
            base_trust REAL NOT NULL,
            burst REAL NOT NULL,
            recency REAL NOT NULL
        )
    """)

    rows = conn.execute(
        "SELECT id, restaurant_id, date_created, user_reviews_count FROM reviews"
    ).fetchall()

    reviews = [
        {
            'id': r['id'],
            'restaurant_id': r['restaurant_id'],
            'date': parse_date(r['date_created']),
            'user_reviews_count': r['user_reviews_count'],
        }
        for r in rows
    ]
    print(f"Loaded {len(reviews):,} reviews")

    reference_date = max(r['date'] for r in reviews)
    print(f"Reference date: {reference_date.date()}")

    print("Computing review trust...")
    review_trust = compute_review_trust(reviews, reference_date)

    conn.executemany("INSERT INTO review_trust VALUES (?, ?, ?, ?)", review_trust)
    conn.commit()

    stats = conn.execute("""
        SELECT
            COUNT(*) as n,
            ROUND(AVG(base_trust), 3) as avg_base,
            ROUND(AVG(burst), 3) as avg_burst,
            ROUND(AVG(recency), 3) as avg_recency,
            SUM(burst < 1.0) as burst_flagged
        FROM review_trust
    """).fetchone()

    print(f"  {stats['n']:,} reviews indexed")
    print(f"  base_trust avg: {stats['avg_base']}")
    print(f"  burst avg: {stats['avg_burst']} ({stats['burst_flagged']:,} flagged)")
    print(f"  recency avg: {stats['avg_recency']}")

    # === PHASE 2: Restaurant Stats ===
    print("\n=== Restaurant Stats ===")

    conn.execute("DROP TABLE IF EXISTS restaurant_stats")
    conn.execute("""
        CREATE TABLE restaurant_stats (
            restaurant_id TEXT PRIMARY KEY,
            weighted_rating REAL NOT NULL,
            trusted_review_count INTEGER NOT NULL,
            confidence_score REAL NOT NULL
        )
    """)

    # Global average for Bayesian prior
    global_avg = conn.execute("""
        SELECT AVG(r.rating * rt.base_trust * rt.burst * rt.recency) /
               AVG(rt.base_trust * rt.burst * rt.recency) as avg
        FROM reviews r
        JOIN review_trust rt ON r.id = rt.review_id
    """).fetchone()['avg']
    print(f"Global weighted average: {global_avg:.2f}")

    print("Computing restaurant stats...")
    restaurant_stats = compute_restaurant_stats(conn, global_avg)

    conn.executemany("INSERT INTO restaurant_stats VALUES (?, ?, ?, ?)", restaurant_stats)
    conn.commit()

    stats = conn.execute("""
        SELECT
            COUNT(*) as n,
            ROUND(AVG(weighted_rating), 2) as avg_rating,
            ROUND(AVG(trusted_review_count), 1) as avg_trusted,
            ROUND(AVG(confidence_score), 2) as avg_confidence
        FROM restaurant_stats
    """).fetchone()

    print(f"  {stats['n']:,} restaurants indexed")
    print(f"  weighted_rating avg: {stats['avg_rating']}")
    print(f"  trusted_review_count avg: {stats['avg_trusted']}")
    print(f"  confidence_score avg: {stats['avg_confidence']}")

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
