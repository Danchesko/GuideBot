"""SQLAlchemy ORM models for the database schema."""
from sqlalchemy import Column, String, Float, Integer, Text, DateTime, JSON, BigInteger
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Restaurant(Base):
    """Restaurant from 2GIS."""
    __tablename__ = 'restaurants'

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    address = Column(String)
    lat = Column(Float)
    lon = Column(Float)
    rating = Column(Float)
    reviews_count = Column(Integer)
    category = Column(String)
    cuisine = Column(JSON)
    avg_price_som = Column(Integer)
    schedule = Column(JSON)
    scraped_at = Column(DateTime, server_default=func.now())
    reviews_fetched_at = Column(DateTime)
    reviews_fetch_attempts = Column(Integer, default=0)
    reviews_fetch_error = Column(Text)


class Review(Base):
    """Review from 2GIS."""
    __tablename__ = 'reviews'

    id = Column(String, primary_key=True)
    restaurant_id = Column(String, index=True)
    restaurant_name = Column(String)
    rating = Column(Integer)
    text = Column(Text)
    date_created = Column(DateTime)
    date_edited = Column(DateTime)
    likes_count = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    photos_count = Column(Integer, default=0)
    user_public_id = Column(String)
    user_name = Column(String)
    user_reviews_count = Column(Integer)
    is_verified = Column(Integer, default=0)
    is_hidden = Column(Integer, default=0)
    has_official_answer = Column(Integer, default=0)
    scraped_at = Column(DateTime, server_default=func.now())


class ReviewTrust(Base):
    """Computed trust scores for reviews."""
    __tablename__ = 'review_trust'

    review_id = Column(String, primary_key=True)
    base_trust = Column(Float, nullable=False)
    burst = Column(Float, nullable=False)
    recency = Column(Float, nullable=False)


class RestaurantStats(Base):
    """Aggregated restaurant statistics."""
    __tablename__ = 'restaurant_stats'

    restaurant_id = Column(String, primary_key=True)
    weighted_rating = Column(Float, nullable=False)
    trusted_review_count = Column(Integer, nullable=False)
    confidence_score = Column(Float, nullable=False)


class UserSession(Base):
    """User city preferences (public schema)."""
    __tablename__ = 'user_sessions'
    __table_args__ = {'schema': 'public'}

    user_id = Column(BigInteger, primary_key=True)
    city = Column(String, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
