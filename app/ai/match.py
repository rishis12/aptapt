"""Pure matching/ranking over plain dicts. No DB, no Flask, no Madison hardcoding --
the model only parses (intent.py); this module decides everything about what matches,
how it ranks, and why.

match(intent, buildings, gazetteer, stats=None) -> {
    "fits":      [ {slug, name, tier, plan, per_person_cost, distance, reasons}, ... ],
    "near_miss": [ same shape, tier == "near_miss" ],
    "fallback":  [ {slug, name, distance, reasons, knocked_out_by}, ... ]  # only when fits+near_miss are both empty
}
"""

from ..geo import haversine_miles
from .vocab import AMENITY_SYNONYMS, BEDROOM_COUNTS, DEFAULT_RADIUS_MILES, NEAR_MISS_TOLERANCE

_FAIL_PRIORITY = ["kind", "amenities", "floor_plan", "radius", "budget"]


def _bedroom_count(plan):
    return BEDROOM_COUNTS.get(plan, 0)


def _plan_rents(building):
    """{plan: interpolated_rent}, interpolating rent_min -> rent_max linearly by bedroom count."""
    plans = building.get("floor_plans") or []
    if not plans:
        return {}
    rent_min, rent_max = building.get("rent_min"), building.get("rent_max")
    if rent_min is None and rent_max is None:
        return {p: None for p in plans}
    if rent_min is None:
        rent_min = rent_max
    if rent_max is None:
        rent_max = rent_min

    beds = sorted(_bedroom_count(p) for p in plans)
    lo, hi = beds[0], beds[-1]
    span = hi - lo

    rents = {}
    for plan in plans:
        if span <= 0 or rent_min == rent_max:
            rents[plan] = rent_min
        else:
            frac = (_bedroom_count(plan) - lo) / span
            rents[plan] = round(rent_min + frac * (rent_max - rent_min))
    return rents


def _has_amenity(building, amenity_key):
    synonyms = AMENITY_SYNONYMS.get(amenity_key, [])
    text = " ".join(building.get("amenities") or []).lower()
    return any(s in text for s in synonyms)


def _missing_amenities(building, wanted):
    return [a for a in (wanted or []) if not _has_amenity(building, a)]


def _distance(building, gazetteer, place_key):
    if not place_key or place_key not in gazetteer:
        return None
    p = gazetteer[place_key]
    return haversine_miles(p["lat"], p["lng"], building["lat"], building["lng"])


def _bedroom_requirement(intent):
    """How many bedrooms a qualifying plan needs. bedrooms_min wins when set; otherwise,
    when the group is living together, assume one bedroom per person."""
    if intent.bedrooms_min is not None:
        return intent.bedrooms_min
    living_together = intent.living_together
    group_size = intent.group_size or 1
    if living_together is None:
        living_together = group_size > 1  # documented default assumption
    if living_together:
        return group_size
    return 0


def _qualifying_plans(building, intent):
    plans = building.get("floor_plans") or []
    if intent.floor_plans:
        plans = [p for p in plans if p in intent.floor_plans]
    required_beds = _bedroom_requirement(intent)
    return [p for p in plans if _bedroom_count(p) >= required_beds]


def _cheapest(plans, rents):
    priced = [p for p in plans if rents.get(p) is not None]
    if not priced:
        return plans[0] if plans else None
    return min(priced, key=lambda p: rents[p])


def _best_plan_and_cost(building, intent, rents):
    """(plan, per_person_cost) for the plan that best fits, or (None, None) if nothing does."""
    qualifying = _qualifying_plans(building, intent)
    if not qualifying:
        return None, None

    plan = _cheapest(qualifying, rents)
    cost = rents.get(plan)
    if cost is None or building.get("leasing") == "per_bed":
        return plan, cost  # per_bed rent is already per person

    living_together = intent.living_together
    group_size = intent.group_size or 1
    if living_together is None:
        living_together = group_size > 1
    if living_together:
        cost = cost / group_size
    return plan, cost


def _budget_tier(cost, intent):
    """("fits" | "near_miss" | None, over_fraction) -- None means excluded."""
    if cost is None or intent.budget_max is None:
        return "fits", None
    if cost <= intent.budget_max:
        return "fits", None
    over_fraction = (cost - intent.budget_max) / intent.budget_max
    if over_fraction <= NEAR_MISS_TOLERANCE:
        return "near_miss", over_fraction
    return None, over_fraction


def _momentum(building, stats_entry, group_size):
    """(completes_offer: bool, reason: str | None)."""
    if not stats_entry or not stats_entry.get("target"):
        return False, None
    committed = stats_entry.get("committed_count", 0)
    target = stats_entry["target"]
    projected = min(committed + group_size, target)
    completes = (committed + group_size) >= target
    reason = f"Your {group_size} would take {building['name']} from {committed}/{target} to {projected}/{target}"
    if completes and stats_entry.get("offer_title"):
        reason += f" and unlock {stats_entry['offer_title']}"
    reason += "."
    return completes, reason


def _sort_key(c):
    return (
        not c["_completes_offer"],
        c["distance"] if c["distance"] is not None else float("inf"),
        c["per_person_cost"] if c["per_person_cost"] is not None else float("inf"),
        c["slug"],
    )


def _fallback_candidates(excluded, radius, intent):
    """Up to 3 closest excluded buildings, each with the reason that knocked it out."""
    scored = []
    for item in excluded:
        reasons_by_type = dict(item["fail_reasons"])
        primary = next((t for t in _FAIL_PRIORITY if t in reasons_by_type), None)
        score = 0.0
        if "radius" in reasons_by_type and item["distance"] is not None:
            score += max(0.0, item["distance"] - radius)
        if "budget" in reasons_by_type and item["cost"] is not None and intent.budget_max:
            score += max(0.0, (item["cost"] - intent.budget_max) / intent.budget_max)
        for t in ("kind", "amenities", "floor_plan"):
            if t in reasons_by_type:
                score += 1.0
        scored.append((score, item["building"]["slug"], item, primary, reasons_by_type))

    scored.sort(key=lambda t: (t[0], t[1]))
    out = []
    for _, slug, item, primary, reasons_by_type in scored[:3]:
        b = item["building"]
        out.append({
            "slug": slug,
            "name": b["name"],
            "distance": item["distance"],
            "reasons": [reasons_by_type[t] for t in _FAIL_PRIORITY if t in reasons_by_type],
            "knocked_out_by": reasons_by_type.get(primary, "didn't match"),
        })
    return out


def match(intent, buildings, gazetteer, stats=None):
    stats = stats or {}
    place_key = intent.place
    radius = intent.radius_miles if intent.radius_miles is not None else DEFAULT_RADIUS_MILES
    group_size = intent.group_size or 1
    place_label = gazetteer.get(place_key, {}).get("label", place_key) if place_key else None

    candidates = []
    excluded = []

    for b in buildings:
        rents = _plan_rents(b)
        distance = _distance(b, gazetteer, place_key)
        plan, cost = _best_plan_and_cost(b, intent, rents)

        fail_reasons = []
        if intent.kind and b.get("kind") != intent.kind:
            fail_reasons.append(("kind", f"not a {intent.kind} building"))
        missing = _missing_amenities(b, intent.must_have_amenities)
        if missing:
            fail_reasons.append(("amenities", f"missing {', '.join(missing)}"))
        if plan is None:
            fail_reasons.append(("floor_plan", "no floor plan big enough for your group"))
        if place_key and distance is not None and distance > radius:
            fail_reasons.append(("radius", f"{distance:.1f} mi away, outside your {radius:g}-mile radius"))

        tier, over_fraction = (None, None)
        if not fail_reasons:
            tier, over_fraction = _budget_tier(cost, intent)
            if tier is None:
                fail_reasons.append(("budget", f"about {over_fraction * 100:.0f}% over your budget"))

        if fail_reasons:
            excluded.append({"building": b, "plan": plan, "cost": cost, "distance": distance, "fail_reasons": fail_reasons})
            continue

        completes_offer, momentum_reason = _momentum(b, stats.get(b["slug"]), group_size)

        reasons = []
        if cost is not None:
            reasons.append(f"{plan}, about ${cost:,.0f}/person (estimated)")
        elif plan:
            reasons.append(f"{plan} available")
        if distance is not None:
            reasons.append(f"{distance:.1f} mi from {place_label}")
        if tier == "near_miss":
            reasons.append(f"about {over_fraction * 100:.0f}% over your budget")
        if momentum_reason:
            reasons.append(momentum_reason)

        candidates.append({
            "slug": b["slug"], "name": b["name"], "tier": tier, "plan": plan,
            "per_person_cost": cost, "distance": distance, "reasons": reasons,
            "_completes_offer": completes_offer,
        })

    fits = sorted((c for c in candidates if c["tier"] == "fits"), key=_sort_key)
    near_miss = sorted((c for c in candidates if c["tier"] == "near_miss"), key=_sort_key)
    for c in fits + near_miss:
        del c["_completes_offer"]

    fallback = [] if (fits or near_miss) else _fallback_candidates(excluded, radius, intent)
    return {"fits": fits, "near_miss": near_miss, "fallback": fallback}
