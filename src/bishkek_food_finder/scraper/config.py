"""Configuration for the scraper."""

from bishkek_food_finder.config import CITIES, get_city_config  # Re-export from root config

# === LEGACY DEFAULTS (for backward compatibility) ===

LOG_DIR = "logs"
DB_PATH = "data/bishkek.db"  # Default, use get_city_config() for multi-city
SEARCH_URL = CITIES["bishkek"]["search_url"]

# 2GIS APIs
REVIEWS_API_URL = "https://public-api.reviews.2gis.com/2.0/branches/{restaurant_id}/reviews"
REVIEWS_API_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Rate limiting
REQUEST_DELAY = 1  # seconds between requests
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # exponential: 1s, 2s, 4s

# Scraping - max_pages is per-city in CITIES dict

# Reviews scraping
REVIEWS_PAGE_LIMIT = 50  # Max reviews per API request
MAX_CONCURRENT_RESTAURANTS = 10  # Parallel restaurant scraping


