from app import geo
from app.db import connect


def test_haversine_capitol_to_camp_randall_is_about_one_and_a_half_miles():
    d = geo.haversine_miles(43.0747, -89.3842, 43.0700, -89.4127)
    assert 1.3 < d < 1.6


def test_zip_lookup_and_radius(seeded):
    db = connect(seeded.config["DATABASE"])
    assert geo.zip_center(db, "60601") is None
    center = geo.zip_center(db, " 53703 ")
    assert center["label"].startswith("Downtown")
    near = geo.apartments_within(db, center, 2)
    assert len(near) == 5  # every seeded building is downtown
    distances = [d for _, d in near]
    assert distances == sorted(distances)
    assert geo.apartments_within(db, geo.zip_center(db, "53714"), 0.5) == []


def test_parse_radius_only_allows_known_choices():
    assert geo.parse_radius("5") == 5
    assert geo.parse_radius("7") == 2
    assert geo.parse_radius(None) == 2
