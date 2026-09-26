"""ZIP lookup and radius filtering. Madison only, so a few dozen rows filtered in Python is plenty."""

import math

RADIUS_CHOICES = (0.5, 1, 2, 3, 5, 10)
EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def zip_center(db, zip_code):
    """Return the zip_centroids row, or None when the ZIP isn't a supported Madison ZIP."""
    zip_code = (zip_code or "").strip()
    return db.execute("SELECT * FROM zip_centroids WHERE zip = ?", (zip_code,)).fetchone()


def supported_zips(db):
    return db.execute("SELECT * FROM zip_centroids ORDER BY zip").fetchall()


def parse_radius(value, default=2):
    try:
        radius = float(value)
    except (TypeError, ValueError):
        return default
    return radius if radius in RADIUS_CHOICES else default


def apartments_within(db, center, radius_miles):
    """Apartments within radius_miles of center, nearest first, as (row, distance) pairs."""
    results = []
    for apt in db.execute("SELECT * FROM apartments"):
        distance = haversine_miles(center["lat"], center["lng"], apt["lat"], apt["lng"])
        if distance <= radius_miles:
            results.append((apt, distance))
    results.sort(key=lambda pair: pair[1])
    return results
