"""Central database connection for PostgreSQL.

All modules should import from here instead of using sqlite3 directly.
"""
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_connection():
    """Get PostgreSQL connection. Use with 'with' statement.

    Example:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM restaurants WHERE city = %s", (city,))
                rows = cur.fetchall()
    """
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable not set")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def get_dict_cursor(conn):
    """Get cursor that returns dicts (like sqlite3.Row)."""
    return conn.cursor(cursor_factory=RealDictCursor)


def execute_query(sql: str, params: tuple = None, city: str = None) -> list[dict]:
    """Execute a SELECT query and return results as list of dicts.

    Convenience function for simple queries.
    """
    with get_connection() as conn:
        with get_dict_cursor(conn) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def ensure_schema():
    """Create required tables and indexes. Call once at startup."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    user_id BIGINT PRIMARY KEY,
                    city TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reviews_fts
                ON reviews USING GIN (to_tsvector('russian', text))
            """)
        conn.commit()


def get_user_city(user_id: int) -> str | None:
    """Get user's city preference from database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT city FROM user_sessions WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None


def set_user_city(user_id: int, city: str):
    """Save user's city preference to database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_sessions (user_id, city) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET city = %s, updated_at = NOW()
            """, (user_id, city, city))
        conn.commit()
