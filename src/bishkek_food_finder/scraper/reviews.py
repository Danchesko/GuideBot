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


async def fetch_all_reviews_for_restaurant(client, restaurant_id, restaurant_name, existing_ids):
    """Fetch reviews for a single restaurant with pagination and incremental update support.

    ID-based approach:
    - Stream pages and filter per page (skip IDs we already have)
    - Early stop when entire page already exists
    - Return all new reviews for batch save

    Args:
        client: httpx.AsyncClient instance
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name (for logging and storing)
        existing_ids: Set of review IDs we already have for this restaurant

    Returns:
        list[dict]: All NEW reviews (filtered by existing IDs)
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

            # Filter: only keep reviews we don't have yet
            new_reviews = [r for r in page_reviews if r['id'] not in existing_ids]

            all_new_reviews.extend(new_reviews)

            logger.debug(f"{restaurant_name}: Page {page_num}, got {len(page_reviews)} reviews, {len(new_reviews)} new")

            # EARLY STOP: If ALL reviews in page already exist, stop pagination
            if existing_ids and len(new_reviews) == 0:
                logger.debug(f"{restaurant_name}: All reviews in page already exist, stopping")
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


async def process_restaurant(client, db, restaurant_id, restaurant_name, semaphore, stats_only=False):
    """Process one restaurant: fetch reviews, optionally save to DB, return stats.

    Args:
        client: httpx.AsyncClient instance
        db: sqlite3.Connection (used synchronously within async context)
        restaurant_id: Restaurant ID
        restaurant_name: Restaurant name
        semaphore: asyncio.Semaphore for concurrency control
        stats_only: If True, fetch but don't save to database

    Returns:
        dict: {'new_reviews': int, 'had_new': bool, 'error': bool (optional)}
    """
    logger = logging.getLogger(__name__)

    async with semaphore:
        try:
            # Get existing review IDs for this restaurant
            cursor = db.execute(
                "SELECT id FROM reviews WHERE restaurant_id = ?", (restaurant_id,)
            )
            existing_ids = {row[0] for row in cursor.fetchall()}

            # Fetch reviews (with incremental update using existing IDs)
            reviews = await fetch_all_reviews_for_restaurant(
                client, restaurant_id, restaurant_name, existing_ids
            )

            # If no new reviews and we already had some
            if len(reviews) == 0 and existing_ids:
                if not stats_only:
                    db.execute("""
                        UPDATE restaurants
                        SET reviews_fetched_at = CURRENT_TIMESTAMP,
                            reviews_fetch_error = NULL
                        WHERE id = ?
                    """, (restaurant_id,))
                    db.commit()
                logger.info(f"{restaurant_name}: ✓ No new reviews (up to date)")
                return {'new_reviews': 0, 'had_new': False}

            # Only save to DB if not stats_only
            if not stats_only:
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

                # Mark success
                db.execute("""
                    UPDATE restaurants
                    SET reviews_fetched_at = CURRENT_TIMESTAMP,
                        reviews_fetch_error = NULL
                    WHERE id = ?
                """, (restaurant_id,))
                db.commit()

            logger.info(f"{restaurant_name}: ✓ {'Found' if stats_only else 'Saved'} {len(reviews)} reviews")
            return {'new_reviews': len(reviews), 'had_new': len(reviews) > 0}

        except Exception as e:
            if not stats_only:
                # Mark failure in DB
                db.execute("""
                    UPDATE restaurants
                    SET reviews_fetch_attempts = reviews_fetch_attempts + 1,
                        reviews_fetch_error = ?
                    WHERE id = ?
                """, (str(e), restaurant_id))
                db.commit()

            logger.error(f"{restaurant_name}: ✗ Failed: {e}")
            return {'new_reviews': 0, 'had_new': False, 'error': True}


async def main_async(args, db, restaurants):
    """Async main: process all restaurants concurrently.

    Returns:
        dict: Aggregated stats for this run
    """
    logger = logging.getLogger(__name__)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESTAURANTS)

    # Stats tracking
    stats = {
        'processed': 0,
        'with_new_reviews': 0,
        'up_to_date': 0,
        'new_reviews_total': 0,
        'errors': 0
    }

    async with httpx.AsyncClient() as client:
        tasks = [
            process_restaurant(
                client, db, rest_id, name, semaphore,
                stats_only=args.stats_only
            )
            for rest_id, name in restaurants
        ]

        # Run with progress bar and collect results
        for coro in tqdm.as_completed(tasks, total=len(tasks), desc="Scraping reviews"):
            result = await coro
            if result:
                stats['processed'] += 1
                if result.get('error'):
                    stats['errors'] += 1
                elif result.get('had_new'):
                    stats['with_new_reviews'] += 1
                    stats['new_reviews_total'] += result['new_reviews']
                else:
                    stats['up_to_date'] += 1

    return stats


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
    parser.add_argument(
        '--stats-only',
        action='store_true',
        help="Fetch and show stats without saving to database"
    )
    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(script_name="reviews")

    # Print to console (not logged)
    print(f"\n{'='*60}")
    print(f"Starting reviews scraper (stats_only={args.stats_only})")
    print(f"{'='*60}\n")

    # Initialize database
    db = init_database(DB_PATH)

    # Get restaurants that need reviews
    # Either: never fetched OR not checked in N days
    cursor = db.execute("""
        SELECT id, name
        FROM restaurants
        WHERE (
            reviews_fetched_at IS NULL
            OR reviews_fetched_at < datetime('now', '-' || ? || ' days')
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
        for rest_id, name in restaurants[:10]:  # Show first 10
            print(f"  - {name} ({rest_id})")
        if len(restaurants) > 10:
            print(f"  ... and {len(restaurants) - 10} more")
        db.close()
        return

    # Run async scraping
    start_time = datetime.now()
    stats = asyncio.run(main_async(args, db, restaurants))
    elapsed = (datetime.now() - start_time).total_seconds()

    # Get DB total
    cursor = db.execute("SELECT COUNT(*) FROM reviews")
    total_reviews = cursor.fetchone()[0]

    # Summary
    print(f"\n{'='*60}")
    print(f"Scraping complete in {elapsed:.1f}s!")
    print()
    print(f"This run:")
    print(f"  Restaurants processed: {stats['processed']}")
    print(f"  With new reviews: {stats['with_new_reviews']}")
    print(f"  Already up-to-date: {stats['up_to_date']}")
    print(f"  New reviews fetched: {stats['new_reviews_total']}")
    print(f"  Errors: {stats['errors']}")
    print()
    if args.stats_only:
        print(f"Database total: {total_reviews} reviews (unchanged - stats_only mode)")
    else:
        print(f"Database total: {total_reviews} reviews")
    print(f"{'='*60}\n")

    db.close()


if __name__ == "__main__":
    main()
