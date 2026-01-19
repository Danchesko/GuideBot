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


def parse_reviews_page(data, restaurant_name):
    """Parse reviews from 2GIS reviews API JSON response.

    Args:
        data: Parsed JSON dict from reviews API
        restaurant_name: Restaurant name to include in each review

    Returns:
        tuple: (list[dict] reviews, str next_link or None)
            Each review dict has fields:
                id, restaurant_id, restaurant_name, rating, text, date_created, date_edited,
                likes_count, comments_count, photos_count,
                user_public_id, user_name, user_reviews_count,
                is_verified, is_hidden, has_official_answer
    """
    reviews = []
    for r in data.get('reviews', []):
        reviews.append({
            'id': r['id'],
            'restaurant_id': r['object']['id'],
            'restaurant_name': restaurant_name,
            'rating': r['rating'],
            'text': r.get('text'),
            'date_created': r['date_created'],
            'date_edited': r.get('date_edited'),
            'likes_count': r.get('likes_count', 0),
            'comments_count': r.get('comments_count', 0),
            'photos_count': len(r.get('photos', [])),
            'user_public_id': r['user']['public_id'],
            'user_name': r['user']['name'],
            'user_reviews_count': r['user']['reviews_count'],
            'is_verified': 1 if r.get('is_verified') else 0,
            'is_hidden': 1 if r.get('is_hidden') else 0,
            'has_official_answer': 1 if r.get('official_answer') else 0
        })

    next_link = data.get('meta', {}).get('next_link')
    return reviews, next_link


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


async def fetch_all_reviews_for_restaurant(client, restaurant_id, restaurant_name, latest_review_date):
    """Fetch reviews for a single restaurant with pagination and incremental update support.

    Hybrid approach:
    - Stream pages and filter per page (date_created > latest_review_date)
    - Early stop when entire page is old (all reviews <= latest_review_date)
    - Return all new reviews for batch save

    Args:
        client: httpx.AsyncClient instance
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name (for logging and storing)
        latest_review_date: ISO timestamp string of newest review we have (or None for first run)

    Returns:
        list[dict]: All NEW reviews (filtered by date if latest_review_date provided)
    """
    logger = logging.getLogger(__name__)

    url = REVIEWS_API_URL.format(restaurant_id=restaurant_id)
    params = {'limit': REVIEWS_PAGE_LIMIT, 'key': REVIEWS_API_KEY}

    all_new_reviews = []
    page_num = 1

    while url:
        try:
            data = await fetch_reviews_page_with_retry(client, url, params)
            page_reviews, next_link = parse_reviews_page(data, restaurant_name)

            # Filter: only keep reviews newer than what we have
            if latest_review_date:
                new_reviews = [r for r in page_reviews if r['date_created'] > latest_review_date]
            else:
                new_reviews = page_reviews  # First run, all are new

            all_new_reviews.extend(new_reviews)

            logger.debug(f"{restaurant_name}: Page {page_num}, got {len(page_reviews)} reviews, {len(new_reviews)} new")

            # EARLY STOP: If ALL reviews in page are old, stop pagination
            if latest_review_date and len(new_reviews) == 0:
                logger.debug(f"{restaurant_name}: All reviews in page older than {latest_review_date}, stopping")
                break

            # Next page
            url = next_link
            params = None  # next_link already has params
            page_num += 1

            # Rate limit between pages
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"{restaurant_name}: Failed on page {page_num}: {e}")
            raise

    logger.debug(f"{restaurant_name}: Total {len(all_new_reviews)} new reviews fetched")
    return all_new_reviews


async def process_restaurant(client, db, restaurant_id, restaurant_name, latest_review_date, semaphore):
    """Process one restaurant: fetch reviews, save to DB, update metadata.

    Args:
        client: httpx.AsyncClient instance
        db: sqlite3.Connection (used synchronously within async context)
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name
        latest_review_date: ISO timestamp of newest review we have (or None)
        semaphore: asyncio.Semaphore for concurrency control
    """
    logger = logging.getLogger(__name__)

    async with semaphore:
        try:
            # Fetch reviews (with incremental update if latest_review_date provided)
            reviews = await fetch_all_reviews_for_restaurant(
                client, restaurant_id, restaurant_name, latest_review_date
            )

            # If no new reviews and we already had some, just update timestamp
            if len(reviews) == 0 and latest_review_date:
                db.execute("""
                    UPDATE restaurants
                    SET reviews_fetched_at = CURRENT_TIMESTAMP,
                        reviews_fetch_error = NULL
                    WHERE id = ?
                """, (restaurant_id,))
                db.commit()
                logger.info(f"{restaurant_name}: ✓ No new reviews (up to date)")
                return

            # Batch save all reviews
            for r in reviews:
                db.execute("""
                    INSERT OR REPLACE INTO reviews
                    (id, restaurant_id, restaurant_name, rating, text, date_created, date_edited,
                     likes_count, comments_count, photos_count,
                     user_public_id, user_name, user_reviews_count,
                     is_verified, is_hidden, has_official_answer)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r['id'], r['restaurant_id'], r['restaurant_name'], r['rating'], r['text'],
                    r['date_created'], r['date_edited'],
                    r['likes_count'], r['comments_count'], r['photos_count'],
                    r['user_public_id'], r['user_name'], r['user_reviews_count'],
                    r['is_verified'], r['is_hidden'], r['has_official_answer']
                ))

            # Calculate latest review date from saved reviews
            if reviews:
                max_date = max(r['date_created'] for r in reviews)
            else:
                # No reviews for this restaurant at all
                max_date = None

            # Mark success and update latest_review_date
            db.execute("""
                UPDATE restaurants
                SET reviews_fetched_at = CURRENT_TIMESTAMP,
                    reviews_fetch_error = NULL,
                    latest_review_date = ?
                WHERE id = ?
            """, (max_date if max_date else None, restaurant_id))
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
            process_restaurant(client, db, rest_id, name, latest_date, semaphore)
            for rest_id, name, latest_date in restaurants
        ]

        # Run with progress bar
        for coro in tqdm.as_completed(tasks, total=len(tasks), desc="Scraping reviews"):
            await coro


def main():
    """Scrape reviews for all restaurants that need them."""
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Scrape reviews from 2GIS API with incremental update support"
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
    parser.add_argument(
        '--days-since-check',
        type=int,
        default=30,
        help="Re-check restaurants not updated in N days (default: 30)"
    )
    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(script_name="reviews")

    # Print to console (not logged)
    print(f"\n{'='*60}")
    print(f"Starting reviews scraper (dry_run={args.dry_run})")
    print(f"{'='*60}\n")

    # Initialize database
    db = init_database(DB_PATH)

    # Get restaurants that need reviews
    # Either: never fetched OR not checked in N days
    cursor = db.execute("""
        SELECT id, name, latest_review_date
        FROM restaurants
        WHERE (
            latest_review_date IS NULL
            OR latest_review_date < datetime('now', '-' || ? || ' days')
        )
        AND reviews_fetch_attempts < ?
        ORDER BY rowid
    """, (args.days_since_check, args.max_attempts))
    restaurants = cursor.fetchall()

    if args.limit:
        restaurants = restaurants[:args.limit]

    print(f"Found {len(restaurants)} restaurants to process")
    print(f"  (never checked OR not checked in {args.days_since_check} days)\n")

    if not restaurants:
        print("No restaurants to process. Exiting.")
        db.close()
        return

    if args.dry_run:
        print("DRY RUN - Listing restaurants that would be processed:\n")
        for rest_id, name, latest in restaurants[:10]:  # Show first 10
            status = "never checked" if not latest else f"last: {latest}"
            print(f"  - {name} ({rest_id}) [{status}]")
        if len(restaurants) > 10:
            print(f"  ... and {len(restaurants) - 10} more")
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

    print(f"\n{'='*60}")
    print(f"Scraping complete in {elapsed:.1f}s!")
    print(f"Restaurants: {fetched}/{total} fetched, {errors} errors")
    print(f"Total reviews: {total_reviews}")
    print(f"{'='*60}\n")

    db.close()


if __name__ == "__main__":
    main()
