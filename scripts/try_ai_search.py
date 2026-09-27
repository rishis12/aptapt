#!/usr/bin/env python
"""CLI for trying the AI search pipeline by hand.

Examples:
    python scripts/try_ai_search.py "2 bedroom near the Merchandise Mart, under $1800 each, 2 of us"
    python scripts/try_ai_search.py --city madison "studio near state street under 1200"
    python scripts/try_ai_search.py --offline "party of 5 near the riverwalk, separate units"
    python scripts/try_ai_search.py --suite --offline
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai import data, match  # noqa: E402
from app.ai.intent import parse_intent  # noqa: E402

# Canned River North queries for --suite. Expected values are the ground-truth top slug from
# match() against tests/fixtures/river_north (None means "expect no fits/near_miss -- fallback only").
SUITE = [
    ("2BR near the Merchandise Mart, under $1800 each, 2 of us", "amli-river-north"),
    ("studio near clark and kinzie for me, max 1500", "marina-city"),
    ("two bedroom near navy pier, 4 of us, living together", "optima-signature"),
    ("$1200 each, 3 of us, pool and gym required", None),
    ("me and 3 friends want a 3 bedroom under $5000 total near the mart", "wolf-point-east"),
    ("student housing near the mart, pet friendly", None),
    ("party of 5 near the riverwalk, separate units", "marina-city"),
    ("1BR under $2,000 near the magnificent mile", "marina-city"),
    ("four of us want a 2 bedroom near optima signature area, in unit laundry, rooftop", "amli-river-north"),
    ("no more than $1600 each, studio, near clark and kinzie", "hubbard-place"),
]


def _run(query, city, offline, use_cache):
    intent, parser_used = parse_intent(query, city=city, offline=offline, use_cache=use_cache)
    buildings = data.load_buildings(city)
    gazetteer = data.load_gazetteer(city)
    results = match.match(intent, buildings, gazetteer)
    return intent, parser_used, results


def _print_table(results):
    rows = [dict(r, tier=r["tier"]) for r in results["fits"] + results["near_miss"]]
    if rows:
        print(f"{'#':<3} {'Name':<22} {'Tier':<10} {'Plan':<8} {'$/person':<10} {'Distance':<10} Reasons")
        for i, r in enumerate(rows, 1):
            cost = f"${r['per_person_cost']:,.0f}" if r["per_person_cost"] is not None else "-"
            dist = f"{r['distance']:.1f} mi" if r["distance"] is not None else "-"
            print(f"{i:<3} {r['name']:<22} {r['tier']:<10} {(r['plan'] or '-'):<8} {cost:<10} {dist:<10} {'; '.join(r['reasons'])}")
    else:
        print("No matches.")

    if results["fallback"]:
        print("\nClosest near misses:")
        for r in results["fallback"]:
            dist = f"{r['distance']:.1f} mi" if r["distance"] is not None else "-"
            print(f"  {r['name']:<22} {dist:<10} knocked out by: {r['knocked_out_by']}")


def cmd_search(args):
    intent, parser_used, results = _run(args.query, args.city, args.offline, not args.no_cache)

    if args.json:
        print(json.dumps({
            "intent": asdict(intent), "parser": parser_used, "results": results,
        }, indent=2, default=str))
        return 0

    print(f"Parsed by: {parser_used}")
    print(json.dumps(asdict(intent), indent=2))
    print()
    _print_table(results)
    return 0


def cmd_suite(args):
    failures = 0
    key_present = False
    try:
        import os
        key_present = bool(os.environ.get("GEMINI_API_KEY")) and not args.offline
    except Exception:
        pass

    for query, expected in SUITE:
        intent, parser_used, results = _run(query, "river_north", args.offline, not args.no_cache)
        actual = (results["fits"] + results["near_miss"])
        actual_top = actual[0]["slug"] if actual else None
        ok = actual_top == expected
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {query!r} -> top={actual_top!r} (expected {expected!r})")
        if not ok:
            failures += 1

        if key_present:
            live_intent, live_parser, _ = _run(query, "river_north", False, not args.no_cache)
            # "explanation" is Gemini-only display text, not a decision field -- exclude it from the diff.
            offline_fields = {k: v for k, v in asdict(intent).items() if k != "explanation"}
            live_fields = {k: v for k, v in asdict(live_intent).items() if k != "explanation"}
            if live_parser == "gemini" and live_fields != offline_fields:
                print(f"         differs from offline parse: offline={offline_fields} gemini={live_fields}")

    print(f"\n{len(SUITE) - failures}/{len(SUITE)} passed")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Try the AI search pipeline from the command line.")
    parser.add_argument("query", nargs="?", help="Plain-English search query")
    parser.add_argument("--city", choices=data.CITIES, default="river_north")
    parser.add_argument("--offline", action="store_true", help="Force the rule-based parser")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the intent cache")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--suite", action="store_true", help="Run the canned River North query suite")
    args = parser.parse_args()

    if args.suite:
        return cmd_suite(args)
    if not args.query:
        parser.error("a query is required unless --suite is given")
    return cmd_search(args)


if __name__ == "__main__":
    sys.exit(main())
