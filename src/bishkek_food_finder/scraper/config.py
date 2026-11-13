"""Configuration for the scraper."""

import os

from dotenv import load_dotenv

from bishkek_food_finder.config import CITIES, get_city_config  # Re-export from root config

load_dotenv()

# === LEGACY DEFAULTS (for backward compatibility) ===

LOG_DIR = "logs"
DB_PATH = "data/bishkek.db"  # Default, use get_city_config() for multi-city

# 2GIS APIs
REVIEWS_API_URL = "https://public-api.reviews.2gis.com/2.0/branches/{restaurant_id}/reviews"
REVIEWS_API_KEY = os.environ.get("REVIEWS_API_KEY", "")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Rate limiting
REQUEST_DELAY = 1  # seconds between requests
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # exponential: 1s, 2s, 4s

# Scraping - max_pages is per-city in CITIES dict

# Reviews scraping
REVIEWS_PAGE_LIMIT = 50  # Max reviews per API request
MAX_CONCURRENT_RESTAURANTS = 10  # Parallel restaurant scraping


