"""Build Chroma index from reviews.

Supports incremental updates — only embeds new reviews.

Run: uv run python -m bishkek_food_finder.indexer.embeddings
     uv run python -m bishkek_food_finder.indexer.embeddings --city almaty
"""

import argparse
import logging
import sqlite3

from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.errors import NotFoundError
from tqdm import tqdm

from bishkek_food_finder.log import setup_logging
from bishkek_food_finder.scraper.config import CITIES, get_city_config

MODEL_NAME = "cointegrated/rubert-tiny2"
BATCH_SIZE = 128
COLLECTION_NAME = "reviews"  # Will be used as-is (one collection per city chroma path)
MIN_TRUST_DEFAULT = 0.3


def load_reviews(conn, min_trust: float = MIN_TRUST_DEFAULT) -> list[dict]:
    """Load reviews with trust score >= min_trust from SQLite."""
    rows = conn.execute("""
        SELECT r.id, r.restaurant_id, r.text
        FROM reviews r
        JOIN review_trust rt ON r.id = rt.review_id
        WHERE r.text IS NOT NULL AND r.text != ''
          AND (rt.base_trust * rt.burst * rt.recency) >= ?
    """, (min_trust,)).fetchall()
    return [dict(r) for r in rows]


def get_or_create_collection(chroma_path: str):
    """Get existing collection or create new one. Returns (collection, is_new)."""
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        collection = client.get_collection(
            name=COLLECTION_NAME,
        )
        return collection, False
    except NotFoundError:
        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        return collection, True


def get_existing_ids(collection) -> set[str]:
    """Get all review IDs already in Chroma."""
    result = collection.get(include=[])
    return set(result['ids'])


def embed_texts(model, texts: list[str], batch_size: int = BATCH_SIZE) -> list[list[float]]:
    """Embed texts in batches, returns list of vectors."""
    embeddings = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch = texts[i:i + batch_size]
        batch_embeddings = model.encode(batch, show_progress_bar=False)
        embeddings.extend(batch_embeddings.tolist())
    return embeddings


def add_to_collection(collection, reviews: list[dict], embeddings: list[list[float]]):
    """Add reviews with embeddings to Chroma collection."""
    batch_size = 5000
    for i in tqdm(range(0, len(reviews), batch_size), desc="Indexing"):
        batch_reviews = reviews[i:i + batch_size]
        batch_embeddings = embeddings[i:i + batch_size]

        collection.add(
            ids=[r['id'] for r in batch_reviews],
            embeddings=batch_embeddings,
            metadatas=[{"restaurant_id": r['restaurant_id']} for r in batch_reviews],
        )


def delete_collection(chroma_path: str):
    """Delete existing collection if it exists."""
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        client.delete_collection(name=COLLECTION_NAME)
        return True
    except NotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Build Chroma index from reviews")
    parser.add_argument(
        '--city',
        default='bishkek',
        choices=list(CITIES.keys()),
        help="City to process (default: bishkek)"
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help="Use test paths (data/{city}_test.db, data/chroma_{city}_test)"
    )
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help="Delete existing collection and rebuild from scratch"
    )
    parser.add_argument(
        '--min-trust',
        type=float,
        default=MIN_TRUST_DEFAULT,
        help=f"Minimum trust score to include (default: {MIN_TRUST_DEFAULT})"
    )
    args = parser.parse_args()

    city_config = get_city_config(args.city, test=args.test)

    logger = setup_logging(script_name=f"embeddings_{args.city}")
    logger.info(f"Processing {city_config['name']}...")
    logger.info(f"Database: {city_config['db_path']}")
    logger.info(f"Chroma: {city_config['chroma_path']}")
    logger.info(f"Min trust: {args.min_trust}")

    conn = sqlite3.connect(city_config['db_path'])
    conn.row_factory = sqlite3.Row

    logger.info(f"Loading reviews with trust >= {args.min_trust}...")
    reviews = load_reviews(conn, min_trust=args.min_trust)
    logger.info(f"Loaded {len(reviews):,} trusted reviews")

    # Handle rebuild
    if args.rebuild:
        logger.info("Deleting existing collection...")
        if delete_collection(city_config['chroma_path']):
            logger.info("Deleted existing collection")
        else:
            logger.info("No existing collection to delete")

    logger.info(f"Checking Chroma collection at {city_config['chroma_path']}...")
    collection, is_new = get_or_create_collection(city_config['chroma_path'])

    if is_new:
        logger.info("Creating new collection")
        new_reviews = reviews
    else:
        logger.info(f"Found existing collection with {collection.count():,} vectors")
        existing_ids = get_existing_ids(collection)
        new_reviews = [r for r in reviews if r['id'] not in existing_ids]
        logger.info(f"Found {len(new_reviews):,} new reviews to index")

    if not new_reviews:
        logger.info("No new reviews to index. Done!")
        conn.close()
        return

    logger.info(f"Loading model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    logger.info(f"Embedding {len(new_reviews):,} reviews (batch_size={BATCH_SIZE})...")
    texts = [r['text'] for r in new_reviews]
    embeddings = embed_texts(model, texts)

    logger.info("Adding to Chroma...")
    add_to_collection(collection, new_reviews, embeddings)

    logger.info(f"Done! Collection '{COLLECTION_NAME}' now has {collection.count():,} vectors")
    conn.close()


if __name__ == "__main__":
    main()
