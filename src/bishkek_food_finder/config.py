"""Shared configuration for all modules."""

CITIES = {
    "bishkek": {
        "name": "Бишкек",
        "country": "kg",
        "search_url": "https://2gis.kg/bishkek/search/еда/filters/sort=name/page/{page}",
        "link_template": "https://2gis.kg/bishkek/firm/{id}",
        "max_pages": 300,
    },
    "almaty": {
        "name": "Алматы",
        "country": "kz",
        "search_url": "https://2gis.kz/almaty/search/еда/filters/sort=name/page/{page}",
        "link_template": "https://2gis.kz/almaty/firm/{id}",
        "max_pages": 500,
    },
}


def get_city_config(city: str, test: bool = False) -> dict:
    """Get full configuration for a city."""
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}. Available: {list(CITIES.keys())}")

    suffix = "_test" if test else ""

    # Backward compatibility: existing Bishkek data uses data/chroma
    if city == "bishkek" and not test:
        chroma_path = "data/chroma"
    else:
        chroma_path = f"data/chroma_{city}{suffix}"

    return {
        **CITIES[city],
        "city": city,
        "db_path": f"data/{city}{suffix}.db",
        "chroma_path": chroma_path,
    }
