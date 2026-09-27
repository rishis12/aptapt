"""Turns a plain-English query into a strict SearchIntent. Gemini only extracts fields --
it never picks, ranks, or describes buildings (that's match.py, plain Python).

parse_intent() tries Gemini (structured JSON output, temperature 0) when GEMINI_API_KEY is
set, and falls back to a regex/keyword parser otherwise or on any API error, so the app
never breaks in a demo because of the API. Both paths run through the same normalize step.
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .vocab import AMENITY_ENUM, AMENITY_SYNONYMS, BUDGET_BASIS_ENUM, FLOOR_PLAN_ENUM, KIND_ENUM

PROMPT_VERSION = "v3"
# gemini-2.5-flash-lite was retired for new users; running on Gemini 3.5 Flash instead.
DEFAULT_MODEL = "gemini-3.5-flash"
CACHE_PATH = Path(__file__).resolve().parents[2] / "instance" / "intent_cache.json"

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


@dataclass
class SearchIntent:
    city: Optional[str] = None
    place: Optional[str] = None
    radius_miles: Optional[float] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    budget_basis: Optional[str] = None  # "per_person" | "total"
    group_size: Optional[int] = None
    living_together: Optional[bool] = None
    bedrooms_min: Optional[int] = None
    floor_plans: List[str] = field(default_factory=list)
    kind: Optional[str] = None  # "student" | "market"
    must_have_amenities: List[str] = field(default_factory=list)
    unparsed: List[str] = field(default_factory=list)
    explanation: Optional[str] = None  # Gemini's own one-sentence paraphrase, for display only -- never fed to match()


# --- Gemini parser -----------------------------------------------------------
# Gemini's structured-output schema is an OpenAPI 3.0 subset, not JSON Schema: a single
# uppercase `type` (no ["string","null"] unions) plus a separate `nullable` flag, and
# `enum` lists may only contain strings (nullability is `nullable`, never a null in `enum`).

_SYSTEM_INSTRUCTION = (
    "Extract a structured apartment-search intent from a renter's plain-English query. "
    "Only extract what the query actually says -- leave a field null (or empty list) rather than guessing. "
    "Put any leftover fragments that don't map to any field above into unparsed. "
    "Also write a short, casual, one-sentence explanation, in your own voice, of what you understood from the "
    "query -- e.g. \"Got it -- a 2-bedroom near the Merchandise Mart, under $1,800 each, for 2 people.\" "
    "This is shown to the user as proof you actually read their query, so make it specific to what they wrote."
)


def _response_schema(gazetteer):
    place_keys = sorted(gazetteer)
    return {
        "type": "OBJECT",
        "properties": {
            "city": {"type": "STRING", "nullable": True,
                      "description": "The city the user mentions, in free text (e.g. 'madison', 'chicago'). Null if not mentioned."},
            "place": {"type": "STRING", "nullable": True, "enum": place_keys,
                       "description": "A known place from this list, or null if none is mentioned or it doesn't match."},
            "radius_miles": {"type": "NUMBER", "nullable": True, "description": "How far from `place` is acceptable, in miles."},
            "budget_min": {"type": "NUMBER", "nullable": True},
            "budget_max": {"type": "NUMBER", "nullable": True},
            "budget_basis": {"type": "STRING", "nullable": True, "enum": BUDGET_BASIS_ENUM,
                               "description": "Whether the budget is per person or for the whole place."},
            "group_size": {"type": "INTEGER", "nullable": True, "description": "How many renters total, including the user."},
            "living_together": {"type": "BOOLEAN", "nullable": True,
                                  "description": "True if the group wants one shared unit, false if separate units, null if unsaid."},
            "bedrooms_min": {"type": "INTEGER", "nullable": True},
            "floor_plans": {"type": "ARRAY", "items": {"type": "STRING", "enum": FLOOR_PLAN_ENUM}},
            "kind": {"type": "STRING", "nullable": True, "enum": KIND_ENUM},
            "must_have_amenities": {"type": "ARRAY", "items": {"type": "STRING", "enum": AMENITY_ENUM}},
            "unparsed": {"type": "ARRAY", "items": {"type": "STRING"},
                          "description": "Short fragments of the query that don't map to any field above."},
            "explanation": {"type": "STRING",
                             "description": "A short, casual, one-sentence paraphrase of what you understood, in your own voice."},
        },
        "required": ["city", "place", "radius_miles", "budget_min", "budget_max", "budget_basis",
                     "group_size", "living_together", "bedrooms_min", "floor_plans", "kind",
                     "must_have_amenities", "unparsed", "explanation"],
    }


def _parse_with_gemini(query, gazetteer, model):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=model,
        contents=query,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_response_schema(gazetteer),
            system_instruction=_SYSTEM_INSTRUCTION,
        ),
    )
    if not response.text:
        raise ValueError("Gemini didn't return a structured response.")
    return json.loads(response.text)


# --- offline (rule-based) parser ---------------------------------------------

_MONEY = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?"
_RANGE_RE = re.compile(rf"between\s+{_MONEY}\s+(?:and|to)\s+{_MONEY}", re.I)
_DASH_RANGE_RE = re.compile(rf"{_MONEY}\s*-\s*{_MONEY}")
_UNDER_RE = re.compile(rf"(?:under|below|up to|less than|no more than|max(?:imum)?)\s+{_MONEY}", re.I)
_OVER_RE = re.compile(rf"(?:over|above|at least|(?<!no )more than|min(?:imum)?)\s+{_MONEY}", re.I)
# Bare fallback (no under/over/between cue) only counts as money with an explicit $ or k marker --
# otherwise a bare number is more likely a bedroom or people count, not a budget.
_BARE_MONEY_RE = re.compile(rf"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?|\b(\d[\d,]*(?:\.\d+)?)\s*k\b", re.I)
_EACH_RE = re.compile(r"\b(each|per person|per head|apiece|pp)\b", re.I)
_TOTAL_RE = re.compile(r"\b(total|for the place|all together|altogether|combined)\b", re.I)

_OF_US_RE = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+of\s+us\b", re.I)
_ME_AND_RE = re.compile(r"\bme\s+and\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                        r"(?:friends?|roommates?|people)\b", re.I)
_PARTY_RE = re.compile(r"\b(?:party|group)\s+of\s+(\d+)\b", re.I)
_N_PEOPLE_RE = re.compile(r"\b(\d+)\s+(?:people|roommates|friends|renters)\b", re.I)

_BR_RE = re.compile(r"\b(\d)\s*-?\s*br\b", re.I)
_BEDROOM_RE = re.compile(r"\b(\d|one|two|three|four|five)\s*-?\s*bed(?:room)?s?\b", re.I)
_STUDIO_RE = re.compile(r"\bstudio\b", re.I)

# A small denylist so the offline parser can still catch an obviously-not-Madison query
# (e.g. "in Chicago") without an LLM. Not exhaustive -- the live parser has no such limit.
_KNOWN_CITIES = {
    "madison": "madison", "chicago": "chicago", "milwaukee": "milwaukee", "minneapolis": "minneapolis",
    "new york": "new york", "nyc": "new york", "los angeles": "los angeles", "boston": "boston",
    "seattle": "seattle", "austin": "austin", "san francisco": "san francisco", "denver": "denver",
}
_CITY_RE = re.compile(r"\b(" + "|".join(sorted(_KNOWN_CITIES, key=len, reverse=True)) + r")\b", re.I)

_LIVING_TOGETHER_RE = re.compile(r"\b(living together|share a place|one unit|same apartment|same unit)\b", re.I)
_SEPARATE_RE = re.compile(r"\b(separate units|our own places|individually|separate apartments)\b", re.I)
_STUDENT_RE = re.compile(
    r"\b(grad student housing|college student housing|student housing|grad student|college student|students?)\b", re.I
)

_STOPWORDS = {"a", "an", "the", "for", "of", "and", "with", "in", "near", "us", "me", "to", "at",
              "is", "our", "we", "want", "looking", "i", "im", "each", "on", "by"}


def _word_or_digit(s):
    return int(s) if s.isdigit() else _WORD_NUMBERS.get(s.lower())


def _to_number(num_str, is_k):
    value = float(num_str.replace(",", ""))
    return value * 1000 if is_k else value


def _extract_budget(query):
    consumed = []
    budget_min = budget_max = basis = None

    m = _RANGE_RE.search(query) or _DASH_RANGE_RE.search(query)
    if m:
        a, ak, b, bk = m.groups()
        budget_min, budget_max = sorted([_to_number(a, ak), _to_number(b, bk)])
        consumed.append(m.group(0))
    else:
        m = _UNDER_RE.search(query)
        if m:
            budget_max = _to_number(m.group(1), m.group(2))
            consumed.append(m.group(0))
        m2 = _OVER_RE.search(query)
        if m2:
            budget_min = _to_number(m2.group(1), m2.group(2))
            consumed.append(m2.group(0))
        if budget_max is None and budget_min is None:
            m3 = _BARE_MONEY_RE.search(query)
            if m3:
                if m3.group(1) is not None:
                    budget_max = _to_number(m3.group(1), m3.group(2))
                else:
                    budget_max = _to_number(m3.group(3), "k")
                consumed.append(m3.group(0))

    m = _EACH_RE.search(query)
    if m:
        basis = "per_person"
        consumed.append(m.group(0))
    else:
        m = _TOTAL_RE.search(query)
        if m:
            basis = "total"
            consumed.append(m.group(0))

    return budget_min, budget_max, basis, consumed


def _extract_group_size(query):
    m = _OF_US_RE.search(query)
    if m:
        return _word_or_digit(m.group(1)), [m.group(0)]
    m = _ME_AND_RE.search(query)
    if m:
        n = _word_or_digit(m.group(1))
        return (n + 1) if n is not None else None, [m.group(0)]
    m = _PARTY_RE.search(query)
    if m:
        return int(m.group(1)), [m.group(0)]
    m = _N_PEOPLE_RE.search(query)
    if m:
        return int(m.group(1)), [m.group(0)]
    return None, []


def _extract_bedrooms(query):
    m = _BR_RE.search(query)
    if m:
        n = int(m.group(1))
        return n, [f"{n}BR"], [m.group(0)]
    m = _BEDROOM_RE.search(query)
    if m:
        n = _word_or_digit(m.group(1))
        return n, ([f"{n}BR"] if n else []), [m.group(0)]
    m = _STUDIO_RE.search(query)
    if m:
        return 0, ["Studio"], [m.group(0)]
    return None, [], []


def _extract_place(query, gazetteer):
    q_lower = query.lower()
    best = None  # (alias_len, key, alias)
    for key, entry in gazetteer.items():
        for alias in entry.get("aliases", []):
            if alias.lower() in q_lower and (best is None or len(alias) > best[0]):
                best = (len(alias), key, alias)
    return (best[1], best[2]) if best else (None, None)


def _extract_amenities(query):
    q_lower = query.lower()
    found, consumed = [], []
    for key, synonyms in AMENITY_SYNONYMS.items():
        for s in synonyms:
            if s in q_lower:
                found.append(key)
                consumed.append(s)
                break
    return found, consumed


def _extract_living_together(query):
    m = _SEPARATE_RE.search(query)
    if m:
        return False, [m.group(0)]
    m = _LIVING_TOGETHER_RE.search(query)
    if m:
        return True, [m.group(0)]
    return None, []


def _extract_kind(query):
    m = _STUDENT_RE.search(query)
    return ("student", [m.group(0)]) if m else (None, [])


def _extract_city(query):
    m = _CITY_RE.search(query)
    return (_KNOWN_CITIES[m.group(1).lower()], [m.group(0)]) if m else (None, [])


def parse_offline(query, gazetteer):
    """Regex/keyword parser used when ANTHROPIC_API_KEY is missing or the API errors."""
    consumed = []

    budget_min, budget_max, budget_basis, c = _extract_budget(query)
    consumed += c
    group_size, c = _extract_group_size(query)
    consumed += c
    bedrooms_min, floor_plans, c = _extract_bedrooms(query)
    consumed += c
    place_key, alias = _extract_place(query, gazetteer)
    if alias:
        consumed.append(alias)
    amenities, c = _extract_amenities(query)
    consumed += c
    living_together, c = _extract_living_together(query)
    consumed += c
    kind, c = _extract_kind(query)
    consumed += c
    city, c = _extract_city(query)
    consumed += c

    remainder = query
    for span in consumed:
        remainder = re.sub(re.escape(span), " ", remainder, flags=re.I)
    remainder = re.sub(r"[^\w\s]", " ", remainder)
    leftover = [w for w in remainder.split() if w.lower() not in _STOPWORDS and not w.isdigit() and len(w) > 2]
    unparsed = [" ".join(leftover)] if leftover else []

    return {
        "city": city, "place": place_key, "radius_miles": None,
        "budget_min": budget_min, "budget_max": budget_max, "budget_basis": budget_basis,
        "group_size": group_size, "living_together": living_together, "bedrooms_min": bedrooms_min,
        "floor_plans": floor_plans, "kind": kind, "must_have_amenities": amenities, "unparsed": unparsed,
    }


# --- shared normalize/validate ------------------------------------------------

def _clamp(value, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, value))


def normalize_intent(raw, gazetteer):
    """Clamp numbers, drop unknown enum values, fix reversed min/max -- run on output
    from either parser so the rest of the app only ever sees a valid SearchIntent."""
    raw = dict(raw or {})
    unparsed = list(raw.get("unparsed") or [])

    budget_min = raw.get("budget_min")
    budget_max = raw.get("budget_max")
    budget_min = _clamp(budget_min, 0, 20000) if budget_min is not None else None
    budget_max = _clamp(budget_max, 0, 20000) if budget_max is not None else None
    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        budget_min, budget_max = budget_max, budget_min

    group_size = raw.get("group_size")
    if group_size is not None:
        clamped = _clamp(group_size, 1, 20)
        group_size = int(clamped) if clamped is not None else None

    budget_basis = raw.get("budget_basis")
    if budget_basis not in BUDGET_BASIS_ENUM:
        budget_basis = None
    if budget_basis is None and (budget_min is not None or budget_max is not None) and group_size and group_size > 1:
        budget_basis = "per_person"
        unparsed = unparsed + ["assumed a per-person budget since a group size was given"]

    place = raw.get("place")
    if place not in gazetteer:
        place = None

    kind = raw.get("kind")
    if kind not in KIND_ENUM:
        kind = None

    floor_plans = [p for p in (raw.get("floor_plans") or []) if p in FLOOR_PLAN_ENUM]
    amenities = [a for a in (raw.get("must_have_amenities") or []) if a in AMENITY_ENUM]

    bedrooms_min = raw.get("bedrooms_min")
    if bedrooms_min is not None:
        clamped = _clamp(bedrooms_min, 0, 10)
        bedrooms_min = int(clamped) if clamped is not None else None

    radius_miles = raw.get("radius_miles")
    if radius_miles is not None:
        radius_miles = _clamp(radius_miles, 0.1, 15)

    living_together = raw.get("living_together")
    if not isinstance(living_together, bool):
        living_together = None

    city = raw.get("city")
    city = (str(city).strip().lower() or None) if city is not None else None

    explanation = raw.get("explanation")
    explanation = explanation.strip() if isinstance(explanation, str) and explanation.strip() else None

    return SearchIntent(
        city=city, place=place, radius_miles=radius_miles,
        budget_min=int(budget_min) if budget_min is not None else None,
        budget_max=int(budget_max) if budget_max is not None else None,
        budget_basis=budget_basis, group_size=group_size, living_together=living_together,
        bedrooms_min=bedrooms_min, floor_plans=floor_plans, kind=kind,
        must_have_amenities=amenities, unparsed=unparsed, explanation=explanation,
    )


# --- cache --------------------------------------------------------------------

def _cache_key(city, query, model):
    normalized = " ".join(query.strip().lower().split())
    digest = hashlib.sha256(f"{city}|{normalized}|{model}|{PROMPT_VERSION}".encode("utf-8")).hexdigest()
    return digest


def _load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


# --- entry point ----------------------------------------------------------------

def parse_intent(query, city, model=None, use_cache=True, offline=False):
    """Returns (SearchIntent, parser_used) where parser_used is 'gemini' or 'offline'."""
    from . import data

    query = (query or "").strip()
    gazetteer = data.load_gazetteer(city)
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    cache = _load_cache() if use_cache else {}
    key = _cache_key(city, query, model)
    if use_cache and key in cache:
        entry = cache[key]
        return normalize_intent(entry["raw"], gazetteer), entry["parser"]

    raw, parser_used = None, "offline"
    if not offline and os.environ.get("GEMINI_API_KEY"):
        try:
            raw = _parse_with_gemini(query, gazetteer, model)
            parser_used = "gemini"
        except Exception:
            raw = None
    if raw is None:
        raw = parse_offline(query, gazetteer)
        parser_used = "offline"

    if use_cache:
        cache[key] = {"raw": raw, "parser": parser_used}
        _save_cache(cache)

    return normalize_intent(raw, gazetteer), parser_used


def intent_to_dict(intent):
    return asdict(intent)
