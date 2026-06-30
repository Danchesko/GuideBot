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
import subprocess
from tqdm import tqdm

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from bishkek_food_finder.log import setup_logging
from .config import CITIES, get_city_config
from .db import init_database


def detect_chrome_major() -> int | None:
    """Return the installed Chrome major version, or None to let the driver auto-detect.

    Keeps the chromedriver matched to whatever Chrome is installed, so a Chrome
    auto-update never breaks the scraper. Checks macOS and Linux locations.
    """
    candidates = (
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],  # macOS
        ["google-chrome", "--version"],          # Linux (server runs this)
        ["google-chrome-stable", "--version"],
        ["chromium-browser", "--version"],
        ["chromium", "--version"],
    )
    for cmd in candidates:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        match = re.search(r"\b(\d+)\.", out)
        if match:
            return int(match.group(1))
    return None


def extract_api_response(driver, logger, max_retries=3, retry_delay=2):
    """Extract restaurant data from intercepted API call with retry logic.

    When clicking to next page, the browser makes an API call to:
    https://catalog.api.2gis.ru/3.0/items?key=...&q=еда&page=N&sort=name

    We intercept this call using CDP and extract the full JSON response.
    Accumulates logs across retries since get_log() drains the buffer.

    Returns:
        list[dict]: List of restaurants with ALL fields from API
    """
    all_logs = []

    for attempt in range(max_retries):
        # Accumulate logs (get_log drains the buffer each call)
        all_logs.extend(driver.get_log('performance'))

        # Find API calls to catalog.api.2gis.ru
        for entry in all_logs:
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

        # No response found yet, retry if attempts remaining
        if attempt < max_retries - 1:
            logger.debug(f"No API response yet, retrying ({attempt + 1}/{max_retries})...")
            time.sleep(retry_delay)

    logger.warning("No API response found after retries")
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
    """Click to next page and wait for API call to complete.

    Returns True if successful, False if no next page exists (end of results).
    """
    try:
        # Clear network logs before clicking
        driver.get_log('performance')

        # Find and click next page link
        next_link = driver.find_element(By.XPATH, f"//a[contains(@href, '/page/{next_page_num}')]")
        driver.execute_script("arguments[0].scrollIntoView(); arguments[0].click();", next_link)

        # Wait for API call to happen (give it a moment)
        time.sleep(1)

        logger.debug(f"Clicked to page {next_page_num}")
        return True

    except Exception as e:
        logger.info(f"No page {next_page_num} found - likely end of results")
        return False


def _capture_page(driver, logger, page_num, max_retries=3):
    """Extract one page's items, retrying the API read. Returns items or None (never raises)."""
    for attempt in range(max_retries):
        items = extract_api_response(driver, logger)
        if items:
            return items
        logger.warning(f"Page {page_num}: no API response (try {attempt + 1}/{max_retries})")
    logger.error(f"Page {page_num}: gave up after {max_retries} tries")
    return None


def iter_pages(driver, logger, pages):
    """Yield (page_num, items) for pages 2..pages+1 in order.

    items is None when a page never responded, so the caller can record it without
    losing the rest of the run. Never raises, never silently skips.

    Page 1 is NOT captured: 2GIS serves it from initialState with no API call and
    exposes no page-1 link in the pagination to click, so there is no interception
    point. The ~12 alphabetically-first results are a known, documented gap.
    """
    for target in range(2, pages + 2):
        if not click_next_page(driver, target, logger):
            logger.info(f"Reached end of results before page {target}")
            break
        yield target, _capture_page(driver, logger, target)


def save_page(items, db, dry_run, seen_ids, logger):
    """Insert a page of restaurants with INSERT OR REPLACE; track seen ids."""
    for r in items:
        seen_ids.add(r['id'])
        if dry_run:
            logger.debug(f"  [DRY RUN] Would save: {r['name']} ({r['id']})")
            continue
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
    if not dry_run:
        db.commit()


def main():
    """Main scraper entry point."""
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Scrape restaurants from 2GIS with API interception"
    )
    parser.add_argument(
        '--city',
        default='bishkek',
        choices=list(CITIES.keys()),
        help="City to scrape (default: bishkek)"
    )
    parser.add_argument(
        '--db',
        default=None,
        help="Explicit DB path (default: data/{city}.db). Use for ad-hoc scans."
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Test run without saving to database"
    )
    parser.add_argument(
        '--pages',
        type=int,
        default=None,
        help="Number of pages to scrape (default: per-city config)"
    )
    parser.add_argument(
        '--search-term',
        default='еда',
        help="Search term (default: еда). Use 'кофе' for coffee shops."
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help="Run Chrome in headless mode (for servers without display)"
    )
    args = parser.parse_args()

    # Get city configuration
    city_config = get_city_config(args.city, db_path=args.db)

    # Resolve pages: CLI arg overrides city config
    pages = args.pages or city_config['max_pages']

    # Setup logging
    logger = setup_logging(script_name=f"restaurants_{args.city}", console_level=logging.WARNING)
    logger.info(f"Starting scraper for {city_config['name']} (dry_run={args.dry_run}, pages={pages})")

    # Initialize database (unless dry-run)
    db = None
    if not args.dry_run:
        db = init_database(city_config['db_path'])
        logger.info(f"Database initialized: {city_config['db_path']}")
    else:
        logger.info("DRY RUN MODE - No database writes")

    # Launch Chrome with performance logging enabled
    options = uc.ChromeOptions()
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-blink-features=AutomationControlled')
    if args.headless:
        options.add_argument('--headless=new')
        logger.info("Launching Chrome browser (headless)...")
    else:
        logger.info("Launching Chrome browser (visible)...")
    driver = uc.Chrome(options=options, version_main=detect_chrome_major())
    time.sleep(3)  # Let Chrome stabilize before sending commands

    try:
        # Enable CDP network logging
        driver.execute_cdp_cmd('Network.enable', {})
        logger.info("Network logging enabled via CDP")

        # Navigate to page 1
        search_term = getattr(args, 'search_term', 'еда')
        logger.info(f"Navigating to page 1 (search term: {search_term})...")
        url = city_config['search_url_template'].format(term=search_term, page=1)
        driver.get(url)
        time.sleep(7)  # Wait for initial page load (longer for headless)

        # Clear logs from initial navigation
        driver.get_log('performance')

        # Scrape pages: iter_pages yields each page's items (or None on failure),
        # never silently skipping a page and never aborting the whole run.
        total_restaurants = 0
        all_restaurant_ids = set()
        failed_pages = []

        logger.info(f"Starting sequential scrape (up to {pages} pages, stops when no more results)")

        for page_num, items in tqdm(iter_pages(driver, logger, pages), total=pages, desc="Scraping pages"):
            if not items:
                logger.error(f"Page {page_num}: no data captured - recorded for re-run")
                failed_pages.append(page_num)
                continue
            try:
                save_page(items, db, args.dry_run, all_restaurant_ids, logger)
                total_restaurants += len(items)
            except Exception as e:
                logger.error(f"Page {page_num}: save failed: {e}", exc_info=True)
                failed_pages.append(page_num)

        # Summary
        logger.info("=" * 80)
        logger.info("Scraping complete!")
        logger.info(f"  Total restaurants scraped: {total_restaurants}")
        logger.info(f"  Unique restaurants: {len(all_restaurant_ids)}")
        if failed_pages:
            logger.error(f"  {len(failed_pages)} pages had no data: {sorted(failed_pages)}")
            print(f"⚠ {len(failed_pages)} страниц без данных: {sorted(failed_pages)} — перезапусти для добора")
        logger.info("  Note: page 1 (~12 first results) not captured — known 2GIS limitation")

        if args.dry_run:
            logger.info("  DRY RUN - No data was saved to database")
        else:
            logger.info(f"  Data saved to: {city_config['db_path']}")

    finally:
        # Always close browser
        logger.info("Closing browser...")
        driver.quit()

        if db:
            db.close()


if __name__ == "__main__":
    main()
