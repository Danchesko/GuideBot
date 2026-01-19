"""Configuration and logging setup for the scraper."""
import logging
import os
from datetime import datetime

# Paths
DB_PATH = "data/bishkek.db"
LOG_DIR = "logs"

# 2GIS URLs
SEARCH_URL = "https://2gis.kg/bishkek/search/еда/filters/sort=name/page/{page}"
REVIEWS_API_URL = "https://public-api.reviews.2gis.com/2.0/branches/{restaurant_id}/reviews"
REVIEWS_API_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Rate limiting
REQUEST_DELAY = 1  # seconds between requests
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # exponential: 1s, 2s, 4s

# Scraping
TOTAL_PAGES = 299  # 3585 results / 12 per page

# Reviews scraping
REVIEWS_PAGE_LIMIT = 50  # Max reviews per API request
MAX_CONCURRENT_RESTAURANTS = 10  # Parallel restaurant scraping


def setup_logging(log_dir=LOG_DIR, level=logging.DEBUG, script_name="restaurants"):
    """Configure dual logging: timestamped file + console.

    File gets DEBUG (everything), console gets only our INFO messages.
    Silences httpx/urllib3 logs from console.
    """
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")

    # File handler: DEBUG level (everything)
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    # Console handler: WARNING level (only errors/warnings, no INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    # Configure root logger
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, console_handler],
        force=True  # Override any existing config
    )

    # Silence third-party loggers
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    # Our logger: file gets DEBUG, console gets WARNING
    logger = logging.getLogger(__name__)

    # Print this to console manually (not via logger)
    print(f"Logging to: {log_file}")

    return logger
