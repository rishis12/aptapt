"""Fixed enums and synonym maps shared by the intent parser and the matcher.

Keeping these in one place means the LLM tool schema, the offline parser, and the
matcher can never quietly drift apart on what a valid value looks like.
"""

FLOOR_PLAN_ENUM = ["Studio", "1BR", "2BR", "3BR", "4BR", "5BR"]

KIND_ENUM = ["student", "market"]

BUDGET_BASIS_ENUM = ["per_person", "total"]

AMENITY_ENUM = [
    "pool",
    "gym",
    "parking",
    "pet_friendly",
    "in_unit_laundry",
    "rooftop",
    "study_space",
]

# Keyword hits against a building's free-text amenities list (case-insensitive substring match).
AMENITY_SYNONYMS = {
    "pool": ["pool"],
    "gym": ["fitness", "gym", "yoga", "sauna", "cold plunge", "wellness"],
    "parking": ["parking", "garage"],
    "pet_friendly": ["pet"],
    "in_unit_laundry": ["washer", "dryer", "laundry", "in-unit"],
    "rooftop": ["rooftop", "roof"],
    "study_space": ["study", "co-working", "coworking"],
}

# Bedroom count used both for per-plan rent interpolation and for "does this plan fit"
# checks. Floor plans beyond the enum (e.g. a building's own "Townhome" listing) still
# need a count so they interpolate/rank sensibly -- Townhome is treated as ~4BR-sized.
BEDROOM_COUNTS = {
    "Studio": 0,
    "1BR": 1,
    "2BR": 2,
    "3BR": 3,
    "4BR": 4,
    "5BR": 5,
    "Townhome": 4,
}

NEAR_MISS_TOLERANCE = 0.10  # up to 10% over budget_max still counts as a near miss
DEFAULT_RADIUS_MILES = 1.0
