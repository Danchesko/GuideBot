"""Build Chroma index from reviews.

Supports incremental updates — only embeds new reviews.

Run: uv run python -m bishkek_food_finder.indexer.embeddings
"""

import sqlite3
from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.errors import NotFoundError
from tqdm import tqdm

DB_PATH = "data/bishkek.db"
CHROMA_PATH = "data/chroma"
COLLECTION_NAME = "reviews"

MODEL_NAME = "cointegrated/rubert-tiny2"
BATCH_SIZE = 128


def load_reviews(conn) -> list[dict]:
    """Load reviews from SQLite."""
    rows = conn.execute("""
        SELECT id, restaurant_id, text
        FROM reviews
        WHERE text IS NOT NULL AND text != ''
    """).fetchall()
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


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"Loading reviews from {DB_PATH}...")
    reviews = load_reviews(conn)
    print(f"Loaded {len(reviews):,} reviews with text")

    print(f"\nChecking Chroma collection at {CHROMA_PATH}...")
    collection, is_new = get_or_create_collection(CHROMA_PATH)

    if is_new:
        print("Creating new collection")
        new_reviews = reviews
    else:
        print(f"Found existing collection with {collection.count():,} vectors")
        existing_ids = get_existing_ids(collection)
        new_reviews = [r for r in reviews if r['id'] not in existing_ids]
        print(f"Found {len(new_reviews):,} new reviews to index")

    if not new_reviews:
        print("\nNo new reviews to index. Done!")
        conn.close()
        return

    print(f"\nLoading model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    print(f"\nEmbedding {len(new_reviews):,} reviews (batch_size={BATCH_SIZE})...")
    texts = [r['text'] for r in new_reviews]
    embeddings = embed_texts(model, texts)

    print(f"\nAdding to Chroma...")
    add_to_collection(collection, new_reviews, embeddings)

    print(f"\nDone! Collection '{COLLECTION_NAME}' now has {collection.count():,} vectors")
    conn.close()


if __name__ == "__main__":
    main()
