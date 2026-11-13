"""Build Chroma index from reviews.

Supports incremental updates — only embeds new reviews.

Run: uv run python -m bishkek_food_finder.indexer.embeddings
     uv run python -m bishkek_food_finder.indexer.embeddings --city almaty
"""

import argparse
import sqlite3
from contextlib import closing

from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.errors import NotFoundError
from tqdm import tqdm

from bishkek_food_finder.log import setup_logging
from bishkek_food_finder.config import CITIES, get_city_config

MODEL_NAME = "cointegrated/rubert-tiny2"
BATCH_SIZE = 128
COLLECTION_NAME = "reviews"  # Will be used as-is (one collection per city chroma path)
MIN_TRUST_DEFAULT = 0.3


def iter_review_batches(conn, min_trust=MIN_TRUST_DEFAULT, batch_size=BATCH_SIZE):
    """Read eligible reviews without loading the whole database into memory."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    cursor = conn.execute("""
        SELECT r.id, r.restaurant_id, r.text
        FROM reviews r
        JOIN review_trust rt ON r.id = rt.review_id
        WHERE r.text IS NOT NULL AND r.text != ''
          AND (rt.base_trust * rt.burst * rt.recency) >= ?
    """, (min_trust,))
    try:
        while rows := cursor.fetchmany(batch_size):
            yield [dict(row) for row in rows]
    finally:
        cursor.close()


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


def index_reviews(
    conn, collection, min_trust=MIN_TRUST_DEFAULT, batch_size=BATCH_SIZE,
    model_factory=SentenceTransformer,
):
    """Embed and save missing reviews one batch at a time."""
    model = None
    indexed = 0
    with closing(iter_review_batches(conn, min_trust, batch_size)) as batches:
        for reviews in tqdm(batches, desc="Indexing", unit="batch"):
            existing = set(collection.get(
                ids=[review['id'] for review in reviews], include=[],
            )['ids'])
            new_reviews = [review for review in reviews if review['id'] not in existing]
            if not new_reviews:
                continue

            if model is None:
                model = model_factory(MODEL_NAME)
            embeddings = model.encode(
                [review['text'] for review in new_reviews], show_progress_bar=False,
            ).tolist()
            collection.add(
                ids=[review['id'] for review in new_reviews],
                embeddings=embeddings,
                metadatas=[{'restaurant_id': review['restaurant_id']} for review in new_reviews],
            )
            indexed += len(new_reviews)
    return indexed


def delete_collection(chroma_path: str):
    """Delete existing collection if it exists."""
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        client.delete_collection(name=COLLECTION_NAME)
        return True
    except NotFoundError:
        return False


def build_fts_index(conn, logger):
    """Build FTS5 full-text search index on reviews table."""
    logger.info("Building FTS5 index...")
    conn.execute("DROP TABLE IF EXISTS reviews_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE reviews_fts USING fts5(
            text, content='reviews', content_rowid=rowid,
            tokenize='unicode61 remove_diacritics 2'
        )
    """)
    conn.execute("INSERT INTO reviews_fts(reviews_fts) VALUES('rebuild')")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM reviews_fts").fetchone()[0]
    logger.info(f"FTS5: {count:,} reviews indexed")


def main():
    parser = argparse.ArgumentParser(description="Build Chroma index from reviews")
    parser.add_argument(
        '--city',
        default='bishkek',
        choices=list(CITIES.keys()),
        help="City to process (default: bishkek)"
    )
    parser.add_argument(
        '--db',
        default=None,
        help="Explicit DB path (default: data/{city}.db). Use for ad-hoc scans."
    )
    parser.add_argument(
        '--chroma',
        default=None,
        help="Explicit Chroma path (default: data/chroma_{city})."
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

    city_config = get_city_config(args.city, db_path=args.db, chroma_path=args.chroma)

    logger = setup_logging(script_name=f"embeddings_{args.city}")
    logger.info(f"Processing {city_config['name']}...")
    logger.info(f"Database: {city_config['db_path']}")
    logger.info(f"Chroma: {city_config['chroma_path']}")
    logger.info(f"Min trust: {args.min_trust}")

    conn = sqlite3.connect(city_config['db_path'])
    conn.row_factory = sqlite3.Row

    try:
        if args.rebuild:
            logger.info("Deleting existing collection...")
            delete_collection(city_config['chroma_path'])

        collection, _ = get_or_create_collection(city_config['chroma_path'])
        indexed = index_reviews(conn, collection, min_trust=args.min_trust)
        logger.info(f"Added {indexed:,} reviews; collection has {collection.count():,} vectors")
        build_fts_index(conn, logger)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
