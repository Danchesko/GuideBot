"""Parse 2GIS search pages and reviews API responses."""
import re
import json


def parse_search_page(html):
    """Parse restaurants from 2GIS search page HTML.

    Args:
        html: Raw HTML string from search page

    Returns:
        list[dict]: List of restaurant dicts with fields:
            id, name, address, lat, lon, rating, reviews_count,
            category, cuisine, avg_price_som, schedule

    Raises:
        ValueError if initialState not found in HTML
    """
    # 1. Extract initialState JSON
    match = re.search(r"var initialState = JSON\.parse\('(.+?)'\);", html)
    if not match:
        raise ValueError("initialState not found in HTML")

    json_escaped = match.group(1)
    # The JS string contains UTF-8 directly + escaped backslashes/quotes (\\ \")
    # unicode_escape interprets \\, \", but corrupts UTF-8 bytes
    # Fix: decode unicode_escape, then fix the UTF-8 (latin1→utf-8 re-decode)
    json_str = json_escaped.encode('raw_unicode_escape').decode('unicode_escape')
    data = json.loads(json_str)

    # 2. Navigate to restaurant entities
    entities = data['data']['entity']['profile']

    # 3. Parse each restaurant
    restaurants = []
    for rest_id, entity in entities.items():
        r = entity['data']

        # Extract cuisine from attribute_groups
        cuisine_tags = []
        for group in r.get('attribute_groups', []):
            for attr in group.get('attributes', []):
                tag = attr.get('tag', '')
                if 'food_service_food_' in tag:  # Cuisine tags
                    cuisine_tags.append(attr['name'])

        # Extract average price
        avg_price = None
        for group in r.get('attribute_groups', []):
            for attr in group.get('attributes', []):
                if attr.get('tag') == 'food_service_avg_price':
                    # Parse "Чек 800 сом" → 800
                    price_match = re.search(r'\d+', attr['name'])
                    if price_match:
                        avg_price = int(price_match.group())

        # Extract coordinates (some entries may not have point field)
        point = r.get('point', {})
        lat = point.get('lat') if point else None
        lon = point.get('lon') if point else None

        restaurants.append({
            'id': r['id'],
            'name': r['name'],
            'address': r.get('address_name'),
            'lat': lat,
            'lon': lon,
            'rating': r.get('reviews', {}).get('general_rating', 0),
            'reviews_count': r.get('reviews', {}).get('general_review_count', 0),
            'category': r['rubrics'][0]['name'] if r.get('rubrics') else None,
            'cuisine': json.dumps(cuisine_tags, ensure_ascii=False),
            'avg_price_som': avg_price,
            'schedule': json.dumps(r.get('schedule'), ensure_ascii=False)
        })

    return restaurants


def parse_reviews_page(data):
    """Parse reviews from 2GIS reviews API JSON response.

    Args:
        data: Parsed JSON dict from reviews API

    Returns:
        tuple: (list[dict] reviews, str next_link or None)
            Each review dict has fields:
                id, restaurant_id, rating, text, date_created, date_edited,
                likes_count, comments_count, photos_count,
                user_public_id, user_name, user_reviews_count,
                is_verified, is_hidden, has_official_answer
    """
    reviews = []
    for r in data.get('reviews', []):
        reviews.append({
            'id': r['id'],
            'restaurant_id': r['object']['id'],
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
