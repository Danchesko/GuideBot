"""Shared configuration for all modules."""

CITIES = {
    "bishkek": {
        "name": "Бишкек",
        "country": "kg",
        "search_url_template": "https://2gis.kg/bishkek/search/{term}/filters/sort=name/page/{page}",
        "link_template": "https://2gis.kg/bishkek/firm/{id}",
        "max_pages": 300,
    },
    "almaty": {
        "name": "Алматы",
        "country": "kz",
        "search_url_template": "https://2gis.kz/almaty/search/{term}/filters/sort=name/page/{page}",
        "link_template": "https://2gis.kz/almaty/firm/{id}",
        "max_pages": 500,
    },
}


def get_city_config(
    city: str,
    db_path: str | None = None,
    chroma_path: str | None = None,
) -> dict:
    """Get full configuration for a city. db_path and chroma_path default
    to data/{city}.db and data/chroma_{city} when not provided."""
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}. Available: {list(CITIES.keys())}")

    return {
        **CITIES[city],
        "city": city,
        "db_path": db_path or f"data/{city}.db",
        "chroma_path": chroma_path or f"data/chroma_{city}",
    }
