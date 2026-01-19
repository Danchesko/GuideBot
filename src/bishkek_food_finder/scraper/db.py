"""Database initialization for the scraper."""
import sqlite3
import os


def init_database(db_path):
    """Initialize database with restaurants table.

    Returns sqlite3.Connection.
    NO CRUD methods - use raw SQL in main script.
    """
    # Create directory if needed
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.text_factory = str  # Ensure proper UTF-8 handling

    # Create restaurants table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            -- P0: Core identification
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT,
            lat REAL,
            lon REAL,
            rating REAL DEFAULT 0,
            reviews_count INTEGER DEFAULT 0,

            -- P1: Bot filtering
            category TEXT,
            cuisine TEXT DEFAULT '[]',
            avg_price_som INTEGER,
            schedule TEXT DEFAULT '{}',

            -- Scraping metadata
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviews_fetched_at TIMESTAMP,
            reviews_fetch_error TEXT,
            reviews_fetch_attempts INTEGER DEFAULT 0,
            latest_review_date TIMESTAMP
        )
    """)

    # Create reviews table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            -- Core review data
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL,
            restaurant_name TEXT,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            text TEXT,
            date_created TIMESTAMP NOT NULL,
            date_edited TIMESTAMP,
            likes_count INTEGER DEFAULT 0,
            comments_count INTEGER DEFAULT 0,
            photos_count INTEGER DEFAULT 0,

            -- User data (denormalized)
            user_public_id TEXT,
            user_name TEXT,
            user_reviews_count INTEGER,

            -- Verification flags (from API)
            is_verified INTEGER DEFAULT 0,
            is_hidden INTEGER DEFAULT 0,
            has_official_answer INTEGER DEFAULT 0,

            -- Metadata
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Index for querying reviews by restaurant
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reviews_restaurant
        ON reviews(restaurant_id)
    """)

    conn.commit()
    return conn
