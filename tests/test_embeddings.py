import logging
import sqlite3
import unittest
from unittest.mock import Mock, patch

import numpy as np

from bishkek_food_finder.indexer.embeddings import (
    build_fts_index, index_reviews, iter_review_batches,
)


class Collection:
    def __init__(self, ids=()):
        self.ids = set(ids)
        self.lookups = []
        self.writes = []

    def get(self, ids, include):
        self.lookups.append(list(ids))
        return {"ids": [item for item in ids if item in self.ids]}

    def add(self, ids, embeddings, metadatas):
        self.writes.append((ids, embeddings, metadatas))
        self.ids.update(ids)


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE reviews (id TEXT PRIMARY KEY, restaurant_id TEXT, text TEXT);
            CREATE TABLE review_trust (
                review_id TEXT, base_trust REAL, burst REAL, recency REAL
            );
        """)
        for i in range(5):
            self.db.execute("INSERT INTO reviews VALUES (?, ?, ?)", (str(i), "r", f"food {i}"))
            self.db.execute("INSERT INTO review_trust VALUES (?, 1, 1, 1)", (str(i),))
        self.db.commit()
        self.model = Mock()
        self.model.encode.side_effect = lambda texts, **kw: np.ones((len(texts), 2))
        self.factory = Mock(return_value=self.model)
        self.addCleanup(self.db.close)

    def test_batches_are_bounded(self):
        batches = list(iter_review_batches(self.db, batch_size=2))
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_filter_excludes_empty_and_low_trust_reviews(self):
        self.db.execute("UPDATE reviews SET text = '' WHERE id = '0'")
        self.db.execute("UPDATE reviews SET text = NULL WHERE id = '1'")
        self.db.execute("UPDATE review_trust SET base_trust = 0.1 WHERE review_id = '2'")
        reviews = [r for batch in iter_review_batches(self.db) for r in batch]
        self.assertEqual([r["id"] for r in reviews], ["3", "4"])

    def test_invalid_batch_size(self):
        with self.assertRaises(ValueError):
            list(iter_review_batches(self.db, batch_size=0))

    def test_incremental_write_and_rerun(self):
        collection = Collection(["1", "3"])
        count = index_reviews(self.db, collection, batch_size=2, model_factory=self.factory)
        self.assertEqual(count, 3)
        self.assertTrue(all(len(ids) <= 2 for ids in collection.lookups))
        self.assertEqual([ids for ids, _, _ in collection.writes], [["0"], ["2"], ["4"]])
        self.factory.assert_called_once()
        self.assertEqual(collection.writes[0][2], [{"restaurant_id": "r"}])
        self.factory.reset_mock()
        self.assertEqual(index_reviews(self.db, collection, model_factory=self.factory), 0)
        self.factory.assert_not_called()

    def test_failed_batch_can_be_retried(self):
        collection = Collection()
        self.model.encode.side_effect = [np.ones((2, 2)), RuntimeError("model failed")]
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            index_reviews(self.db, collection, batch_size=2, model_factory=self.factory)
        self.assertEqual(collection.ids, {"0", "1"})
        self.model.encode.side_effect = lambda texts, **kw: np.ones((len(texts), 2))
        self.assertEqual(index_reviews(self.db, collection, batch_size=2, model_factory=self.factory), 3)
        self.assertEqual(collection.ids, {"0", "1", "2", "3", "4"})

    def test_interruption_closes_batch_cursor_before_connection(self):
        batches = iter_review_batches(self.db, batch_size=2)
        self.model.encode.side_effect = KeyboardInterrupt()
        with patch('bishkek_food_finder.indexer.embeddings.iter_review_batches', return_value=batches):
            with self.assertRaises(KeyboardInterrupt):
                index_reviews(self.db, Collection(), model_factory=self.factory)
        self.assertIsNone(batches.gi_frame)

    def test_real_chroma_collection(self):
        import uuid
        import chromadb
        from chromadb.config import Settings

        client = chromadb.EphemeralClient(Settings(anonymized_telemetry=False))
        collection = client.create_collection(
            "guidebot-test-" + uuid.uuid4().hex,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        try:
            self.assertEqual(
                index_reviews(self.db, collection, batch_size=2, model_factory=self.factory), 5,
            )
            self.assertEqual(collection.count(), 5)
            self.assertEqual(
                index_reviews(self.db, collection, batch_size=2, model_factory=self.factory), 0,
            )
            result = collection.query(query_embeddings=[[1.0, 1.0]], n_results=1)
            self.assertEqual(len(result["ids"][0]), 1)
        finally:
            client.delete_collection(collection.name)

    def test_fts_rebuild(self):
        for _ in range(2):
            build_fts_index(self.db, logging.getLogger("test"))
        count = self.db.execute(
            "SELECT COUNT(*) FROM reviews_fts WHERE reviews_fts MATCH 'food'"
        ).fetchone()[0]
        self.assertEqual(count, 5)


if __name__ == "__main__":
    unittest.main()
