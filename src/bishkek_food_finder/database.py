"""SQLAlchemy database engine and session management."""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

from bishkek_food_finder.models import Base, UserSession

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

# Fix for Railway/Heroku: they use postgres:// but SQLAlchemy requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Single engine with connection pooling
engine = create_engine(
    DATABASE_URL,
    pool_size=10,  # Match MAX_CONCURRENT_RESTAURANTS in reviews.py
    max_overflow=10,
    pool_pre_ping=True  # Auto-reconnect on stale connections
)

SessionLocal = sessionmaker(bind=engine)


@contextmanager
def get_session(schema: str = None) -> Session:
    """Get SQLAlchemy session with optional schema.

    Args:
        schema: City name (e.g., 'bishkek') to set as search_path.
                If None, uses default public schema.

    Example:
        with get_session('bishkek') as session:
            restaurants = session.query(Restaurant).all()
    """
    session = SessionLocal()
    try:
        if schema:
            session.execute(text(f"SET search_path TO {schema}, public"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_raw_connection(schema: str = None):
    """Get raw psycopg2 connection for bulk operations.

    Use for performance-critical batch operations (trust.py).
    Caller is responsible for closing.

    Args:
        schema: City name to set as search_path.

    Returns:
        Raw psycopg2 connection. Must call conn.close() when done.
    """
    conn = engine.raw_connection()
    if schema:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}, public")
    return conn


def ensure_schema():
    """Create user_sessions table in public schema. Call once at startup."""
    with get_session() as session:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id BIGINT PRIMARY KEY,
                city TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))


def get_user_city(user_id: int) -> str | None:
    """Get user's city preference from database."""
    with get_session() as session:
        user = session.query(UserSession).filter_by(user_id=user_id).first()
        return user.city if user else None


def set_user_city(user_id: int, city: str):
    """Save user's city preference to database."""
    with get_session() as session:
        session.execute(text("""
            INSERT INTO user_sessions (user_id, city) VALUES (:user_id, :city)
            ON CONFLICT (user_id) DO UPDATE SET city = :city, updated_at = NOW()
        """), {"user_id": user_id, "city": city})
