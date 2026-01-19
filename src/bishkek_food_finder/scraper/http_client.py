"""HTTP client with retry logic."""
import time
import requests
from .config import USER_AGENT, MAX_RETRIES, RETRY_BACKOFF_BASE


def fetch_with_retry(url, session=None, headers=None, max_retries=MAX_RETRIES):
    """Fetch URL with exponential backoff retry.

    Args:
        url: URL to fetch
        session: Optional requests.Session object (maintains cookies/state)
        headers: Optional headers dict
        max_retries: Maximum retry attempts

    Returns:
        requests.Response object

    Raises:
        Exception if all retries fail
    """
    if headers is None:
        headers = {'User-Agent': USER_AGENT}

    # Use session if provided, otherwise create one-off request
    if session is None:
        session = requests.Session()
        session.headers.update(headers)

    for attempt in range(max_retries):
        try:
            response = session.get(url, timeout=10)

            if response.status_code == 200:
                return response
            elif response.status_code in [429, 503]:  # Rate limit or server error
                time.sleep(RETRY_BACKOFF_BASE ** attempt)  # Exponential: 1s, 2s, 4s
            else:
                raise Exception(f"HTTP {response.status_code}")

        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(RETRY_BACKOFF_BASE ** attempt)

    raise Exception(f"Failed after {max_retries} attempts")
