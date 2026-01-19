"""Scrape restaurants from 2GIS using Selenium with API interception.

This scraper:
- Opens visible Chrome browser (user can watch progress)
- Clicks through pages 1→2→3→...→299 sequentially
- Intercepts API calls using Chrome DevTools Protocol (CDP)
- Extracts FULL restaurant data from API JSON response
- Saves to SQLite database with INSERT OR REPLACE (idempotent)
- Has --dry-run mode for testing
- Has --pages N flag to limit pages scraped

Usage:
    # Test on 5 pages
    uv run python -m bishkek_food_finder.scraper.restaurants --pages 5

    # Dry run (no DB writes)
    uv run python -m bishkek_food_finder.scraper.restaurants --dry-run --pages 3

    # Full scrape (299 pages, ~15-20 minutes)
    uv run python -m bishkek_food_finder.scraper.restaurants
"""
import argparse
import logging
import time
import json
import re
from tqdm import tqdm

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .config import setup_logging, SEARCH_URL, TOTAL_PAGES, DB_PATH
from .db import init_database


def extract_api_response(driver, logger):
    """Extract restaurant data from intercepted API call.

    When clicking to next page, the browser makes an API call to:
    https://catalog.api.2gis.ru/3.0/items?key=...&q=еда&page=N&sort=name

    We intercept this call using CDP and extract the full JSON response.

    Returns:
        list[dict]: List of restaurants with ALL fields from API
    """
    # Get network logs
    logs = driver.get_log('performance')

    # Find API calls to catalog.api.2gis.ru
    for entry in logs:
        try:
            log = json.loads(entry['message'])['message']

            # Look for Network.responseReceived events
            if log['method'] == 'Network.responseReceived':
                response = log['params']['response']
                url = response.get('url', '')

                # Check if this is the 2GIS catalog API
                if 'catalog.api.2gis.ru/3.0/items' in url and response.get('status') == 200:
                    request_id = log['params']['requestId']

                    # Get response body
                    try:
                        body_response = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
                        data = json.loads(body_response['body'])

                        # Extract items from response
                        if 'result' in data and 'items' in data['result']:
                            items = data['result']['items']
                            logger.debug(f"Intercepted API response: {len(items)} restaurants")
                            return parse_api_items(items, logger)
                    except Exception as e:
                        logger.debug(f"Could not get response body for request {request_id}: {e}")
                        continue
        except:
            continue

    logger.warning("No API response found in network logs")
    return []


def parse_api_items(items, logger):
    """Parse restaurant data from API items.

    Maps API fields to database schema:
    - id: Direct from API
    - name: Direct from API
    - address: address_name from API
    - lat/lon: From point object
    - rating: From reviews.general_rating
    - reviews_count: From reviews.general_review_count
    - category: From rubrics[0].name
    - cuisine: From attribute_groups (food_service_food_* tags)
    - avg_price_som: From attribute_groups (food_service_avg_price tag)
    - schedule: Direct from API

    Returns:
        list[dict]: Parsed restaurants ready for database insertion
    """
    restaurants = []

    for item in items:
        try:
            # Extract cuisine from attribute_groups
            cuisine_tags = []
            avg_price = None

            for group in item.get('attribute_groups', []):
                for attr in group.get('attributes', []):
                    tag = attr.get('tag', '')

                    # Cuisine tags
                    if 'food_service_food_' in tag:
                        cuisine_tags.append(attr.get('name', ''))

                    # Average price
                    if tag == 'food_service_avg_price':
                        # Parse "Чек 800 сом" → 800
                        price_match = re.search(r'\d+', attr.get('name', ''))
                        if price_match:
                            avg_price = int(price_match.group())

            # Extract coordinates
            point = item.get('point', {})
            lat = point.get('lat')
            lon = point.get('lon')

            # Extract category (first rubric)
            rubrics = item.get('rubrics', [])
            category = rubrics[0].get('name') if rubrics else None

            # Extract reviews
            reviews = item.get('reviews', {})
            rating = reviews.get('general_rating', 0)
            reviews_count = reviews.get('general_review_count', 0)

            # Get simple ID (strip the long hash suffix)
            # API ID format: 70000001080782201_dh6Aktx4dBdB9A825JCH6J2J1GIIGHG3...
            # We want just: 70000001080782201
            full_id = item.get('id', '')
            simple_id = full_id.split('_')[0] if '_' in full_id else full_id

            restaurant = {
                'id': simple_id,
                'name': item.get('name', ''),
                'address': item.get('address_name'),
                'lat': lat,
                'lon': lon,
                'rating': rating,
                'reviews_count': reviews_count,
                'category': category,
                'cuisine': json.dumps(cuisine_tags, ensure_ascii=False),
                'avg_price_som': avg_price,
                'schedule': json.dumps(item.get('schedule'), ensure_ascii=False) if item.get('schedule') else None
            }

            restaurants.append(restaurant)

        except Exception as e:
            logger.error(f"Failed to parse restaurant: {e}")
            continue

    return restaurants


def click_next_page(driver, next_page_num, logger):
    """Click to next page and wait for API call to complete."""
    try:
        # Clear network logs before clicking
        driver.get_log('performance')

        # Find and click next page link
        next_link = driver.find_element(By.XPATH, f"//a[contains(@href, '/page/{next_page_num}')]")
        driver.execute_script("arguments[0].scrollIntoView(); arguments[0].click();", next_link)

        # Wait for API call to happen (give it a moment)
        time.sleep(2)

        logger.debug(f"Clicked to page {next_page_num}")

    except Exception as e:
        logger.error(f"Failed to click to page {next_page_num}: {e}")
        raise


def main():
    """Main scraper entry point."""
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Scrape restaurants from 2GIS Bishkek with API interception"
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Test run without saving to database"
    )
    parser.add_argument(
        '--pages',
        type=int,
        default=TOTAL_PAGES,
        help=f"Number of pages to scrape (default: {TOTAL_PAGES})"
    )
    args = parser.parse_args()

    # Setup logging
    logger = setup_logging()
    logger.info(f"Starting scraper (dry_run={args.dry_run}, pages={args.pages})")

    # Initialize database (unless dry-run)
    db = None
    if not args.dry_run:
        db = init_database(DB_PATH)
        logger.info(f"Database initialized: {DB_PATH}")
    else:
        logger.info("DRY RUN MODE - No database writes")

    # Launch Chrome with performance logging enabled
    logger.info("Launching Chrome browser (visible)...")
    options = uc.ChromeOptions()
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    driver = uc.Chrome(options=options)

    try:
        # Enable CDP network logging
        driver.execute_cdp_cmd('Network.enable', {})
        logger.info("Network logging enabled via CDP")

        # Navigate to page 1
        logger.info("Navigating to page 1...")
        url = SEARCH_URL.format(page=1) if '{page}' in SEARCH_URL else SEARCH_URL
        driver.get(url)
        time.sleep(3)  # Wait for initial page load

        # Clear logs from initial navigation
        driver.get_log('performance')

        # Scrape pages sequentially
        total_restaurants = 0
        all_restaurant_ids = set()

        logger.info(f"Starting sequential scrape (pages 1 to {args.pages})")

        # Start from page 2 (since we need an API call, and page 1 loads via initialState)
        actual_page = 1
        for iteration in tqdm(range(1, args.pages + 1), desc="Scraping pages"):
            try:
                # On first iteration, click to page 2 to trigger API call
                if iteration == 1:
                    logger.debug("Iteration 1: Clicking to page 2 to trigger API call...")
                    click_next_page(driver, 2, logger)
                    actual_page = 2

                logger.debug(f"Scraping page {actual_page}/{args.pages + 1}")

                # Extract restaurants from API response
                current_restaurants = extract_api_response(driver, logger)
                logger.debug(f"Found {len(current_restaurants)} restaurants on page {actual_page}")

                if not current_restaurants:
                    logger.warning(f"Page {actual_page}: No restaurants found! Skipping...")
                    # Try to continue anyway
                    if iteration < args.pages:
                        actual_page += 1
                        click_next_page(driver, actual_page, logger)
                    continue

                # Log first restaurant for debugging
                if current_restaurants:
                    r = current_restaurants[0]
                    logger.debug(f"  First: {r['name']} ({r['id']})")
                    logger.debug(f"    Address: {r['address']}")
                    logger.debug(f"    Lat/Lon: {r['lat']}, {r['lon']}")
                    logger.debug(f"    Rating: {r['rating']} ({r['reviews_count']} reviews)")

                # Save to database (unless dry-run)
                if not args.dry_run:
                    for r in current_restaurants:
                        # Check for duplicates
                        if r['id'] in all_restaurant_ids:
                            logger.debug(f"  Duplicate: {r['name']} ({r['id']}) - updating")
                        else:
                            all_restaurant_ids.add(r['id'])

                        db.execute("""
                            INSERT OR REPLACE INTO restaurants
                            (id, name, address, lat, lon, rating, reviews_count,
                             category, cuisine, avg_price_som, schedule)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            r['id'], r['name'], r['address'], r['lat'], r['lon'],
                            r['rating'], r['reviews_count'], r['category'],
                            r['cuisine'], r['avg_price_som'], r['schedule']
                        ))

                    db.commit()
                    logger.debug(f"  Saved {len(current_restaurants)} restaurants to DB")
                else:
                    # Dry run: just log
                    for r in current_restaurants:
                        logger.debug(f"  [DRY RUN] Would save: {r['name']} ({r['id']})")
                        all_restaurant_ids.add(r['id'])

                total_restaurants += len(current_restaurants)

                # Click to next page (if not last iteration)
                if iteration < args.pages:
                    actual_page += 1
                    logger.debug(f"  Clicking to page {actual_page}...")
                    click_next_page(driver, actual_page, logger)

            except Exception as e:
                logger.error(f"Page {actual_page} failed: {e}", exc_info=True)
                # Try to continue anyway
                if iteration < args.pages:
                    try:
                        actual_page += 1
                        click_next_page(driver, actual_page, logger)
                    except:
                        break
                continue

        # Summary
        logger.info("=" * 80)
        logger.info(f"Scraping complete!")
        logger.info(f"  Total restaurants scraped: {total_restaurants}")
        logger.info(f"  Unique restaurants: {len(all_restaurant_ids)}")

        if args.dry_run:
            logger.info("  DRY RUN - No data was saved to database")
        else:
            logger.info(f"  Data saved to: {DB_PATH}")

    finally:
        # Always close browser
        logger.info("Closing browser...")
        driver.quit()

        if db:
            db.close()


if __name__ == "__main__":
    main()
