"""Async script to scrape reviews from 2GIS API."""
import argparse
import asyncio
import logging
import sqlite3
from datetime import datetime

import httpx
from tqdm.asyncio import tqdm

from .config import (
    setup_logging, DB_PATH, REVIEWS_API_URL, REVIEWS_API_KEY,
    REVIEWS_PAGE_LIMIT, MAX_CONCURRENT_RESTAURANTS, MAX_RETRIES,
    RETRY_BACKOFF_BASE
)
from .db import init_database
from .parsers import parse_reviews_page


async def fetch_reviews_page_with_retry(client, url, params=None, max_retries=MAX_RETRIES):
    """Fetch reviews page with exponential backoff retry.

    Args:
        client: httpx.AsyncClient instance
        url: URL to fetch
        params: Query parameters (if not in URL)
        max_retries: Maximum retry attempts

    Returns:
        dict: Parsed JSON response

    Raises:
        httpx.HTTPError: If all retries fail
    """
    logger = logging.getLogger(__name__)

    for attempt in range(max_retries):
        try:
            response = await client.get(url, params=params, timeout=10.0)
            if response.status_code == 200:
                return response.json()
            elif response.status_code in [429, 503]:  # Rate limit or server error
                wait_time = RETRY_BACKOFF_BASE ** attempt
                logger.warning(f"HTTP {response.status_code}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise httpx.HTTPError(f"HTTP {response.status_code}")
        except (httpx.RequestError, httpx.HTTPError) as e:
            if attempt == max_retries - 1:
                raise
            wait_time = RETRY_BACKOFF_BASE ** attempt
            logger.debug(f"Request failed: {e}, retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)


async def fetch_all_reviews_for_restaurant(client, restaurant_id, restaurant_name):
    """Fetch all reviews for a single restaurant with pagination.

    Args:
        client: httpx.AsyncClient instance
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name (for logging)

    Returns:
        list[dict]: All reviews for this restaurant
    """
    logger = logging.getLogger(__name__)

    url = REVIEWS_API_URL.format(restaurant_id=restaurant_id)
    params = {'limit': REVIEWS_PAGE_LIMIT, 'key': REVIEWS_API_KEY}

    all_reviews = []
    page_num = 1

    while url:
        try:
            data = await fetch_reviews_page_with_retry(client, url, params)
            reviews, next_link = parse_reviews_page(data)
            all_reviews.extend(reviews)

            logger.debug(f"{restaurant_name}: Page {page_num}, got {len(reviews)} reviews")

            # Next page
            url = next_link
            params = None  # next_link already has params
            page_num += 1

            # Rate limit between pages
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"{restaurant_name}: Failed on page {page_num}: {e}")
            raise

    logger.debug(f"{restaurant_name}: Total {len(all_reviews)} reviews fetched")
    return all_reviews


async def process_restaurant(client, db, restaurant_id, restaurant_name, semaphore):
    """Process one restaurant: fetch reviews, save to DB, update metadata.

    Args:
        client: httpx.AsyncClient instance
        db: sqlite3.Connection (used synchronously within async context)
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name
        semaphore: asyncio.Semaphore for concurrency control
    """
    logger = logging.getLogger(__name__)

    async with semaphore:
        try:
            # Fetch all reviews
            reviews = await fetch_all_reviews_for_restaurant(client, restaurant_id, restaurant_name)

            # Save to DB (synchronous SQLite operations)
            for r in reviews:
                db.execute("""
                    INSERT OR REPLACE INTO reviews
                    (id, restaurant_id, rating, text, date_created, date_edited,
                     likes_count, comments_count, photos_count,
                     user_public_id, user_name, user_reviews_count,
                     is_verified, is_hidden, has_official_answer)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r['id'], r['restaurant_id'], r['rating'], r['text'],
                    r['date_created'], r['date_edited'],
                    r['likes_count'], r['comments_count'], r['photos_count'],
                    r['user_public_id'], r['user_name'], r['user_reviews_count'],
                    r['is_verified'], r['is_hidden'], r['has_official_answer']
                ))

            # Mark success
            db.execute("""
                UPDATE restaurants
                SET reviews_fetched_at = CURRENT_TIMESTAMP,
                    reviews_fetch_error = NULL
                WHERE id = ?
            """, (restaurant_id,))
            db.commit()

            logger.info(f"{restaurant_name}: ✓ Saved {len(reviews)} reviews")

        except Exception as e:
            # Mark failure
            db.execute("""
                UPDATE restaurants
                SET reviews_fetch_attempts = reviews_fetch_attempts + 1,
                    reviews_fetch_error = ?
                WHERE id = ?
            """, (str(e), restaurant_id))
            db.commit()

            logger.error(f"{restaurant_name}: ✗ Failed: {e}")


async def main_async(args, db, restaurants):
    """Async main: process all restaurants concurrently."""
    logger = logging.getLogger(__name__)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESTAURANTS)

    async with httpx.AsyncClient() as client:
        tasks = [
            process_restaurant(client, db, rest_id, name, semaphore)
            for rest_id, name in restaurants
        ]

        # Run with progress bar
        for coro in tqdm.as_completed(tasks, total=len(tasks), desc="Scraping reviews"):
            await coro


def main():
    """Scrape reviews for all restaurants that need them."""
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Scrape reviews from 2GIS API"
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Test run without saving to database"
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help="Limit number of restaurants to process (for testing)"
    )
    parser.add_argument(
        '--max-attempts',
        type=int,
        default=3,
        help="Max fetch attempts before giving up (default: 3)"
    )
    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(script_name="reviews")
    logger.info(f"Starting reviews scraper (dry_run={args.dry_run})")

    # Initialize database
    db = init_database(DB_PATH)
    logger.info(f"Database initialized: {DB_PATH}")

    # Get restaurants that need reviews
    cursor = db.execute("""
        SELECT id, name FROM restaurants
        WHERE reviews_fetched_at IS NULL
        AND reviews_fetch_attempts < ?
        ORDER BY id
    """, (args.max_attempts,))
    restaurants = cursor.fetchall()

    if args.limit:
        restaurants = restaurants[:args.limit]

    logger.info(f"Found {len(restaurants)} restaurants pending review fetch")

    if not restaurants:
        logger.info("No restaurants to process. Exiting.")
        db.close()
        return

    if args.dry_run:
        logger.info("DRY RUN - Listing restaurants that would be processed:")
        for rest_id, name in restaurants[:10]:  # Show first 10
            logger.info(f"  - {name} ({rest_id})")
        if len(restaurants) > 10:
            logger.info(f"  ... and {len(restaurants) - 10} more")
        db.close()
        return

    # Run async scraping
    start_time = datetime.now()
    asyncio.run(main_async(args, db, restaurants))
    elapsed = (datetime.now() - start_time).total_seconds()

    # Summary
    cursor = db.execute("""
        SELECT
            COUNT(*) as total_restaurants,
            SUM(CASE WHEN reviews_fetched_at IS NOT NULL THEN 1 ELSE 0 END) as fetched,
            SUM(CASE WHEN reviews_fetch_error IS NOT NULL THEN 1 ELSE 0 END) as errors
        FROM restaurants
    """)
    total, fetched, errors = cursor.fetchone()

    cursor = db.execute("SELECT COUNT(*) FROM reviews")
    total_reviews = cursor.fetchone()[0]

    logger.info(f"Scraping complete in {elapsed:.1f}s!")
    logger.info(f"Restaurants: {fetched}/{total} fetched, {errors} errors")
    logger.info(f"Total reviews: {total_reviews}")

    db.close()


if __name__ == "__main__":
    main()
