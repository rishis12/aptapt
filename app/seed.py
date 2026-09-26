"""Seed: five real Madison buildings (app/data/buildings.json) with fictional property managers."""

import json
import re
from pathlib import Path

from werkzeug.security import generate_password_hash

from . import payments
from . import groups as svc
from .db import init_schema

BUILDINGS_JSON = Path(__file__).parent / "data" / "buildings.json"
DEMO_PASSWORD = "demo1234"

ZIPS = [
    ("53703", "Downtown / Capitol", 43.0766, -89.3831),
    ("53706", "UW Campus", 43.0766, -89.4125),
    ("53715", "Regent / Vilas", 43.0650, -89.3990),
    ("53704", "North & East Side", 43.1200, -89.3500),
    ("53705", "Near West / Shorewood", 43.0730, -89.4600),
    ("53711", "Southwest / Monroe St", 43.0450, -89.4400),
    ("53713", "South Park St", 43.0370, -89.3970),
    ("53714", "Eastmorland", 43.0980, -89.3100),
    ("53716", "Monona / Lake Edge", 43.0680, -89.3150),
    ("53726", "Regent / University Heights", 43.0710, -89.4210),
]

# Fictional management per building (matched by a keyword in the slug/name).
# on_aptapt=True means the manager has an agent account and runs the offer path;
# otherwise groups set an ask and aptapt pitches it to the contact.
MANAGERS = [
    {"match": "seven20", "company": "Eastline Residential", "contact": "Morgan Reyes",
     "email": "morgan@eastline.test", "on_aptapt": True},
    {"match": "theory", "company": "Campus Key Living", "contact": "Dana Olsen",
     "email": "dana@campuskey.test", "on_aptapt": True},
    {"match": "bella", "company": "Isthmus Property Group", "contact": "Casey Nguyen",
     "email": "casey@isthmuspg.test", "on_aptapt": False},
    {"match": "axton", "company": "Regent Street Living", "contact": "Jamie Lindqvist",
     "email": "jamie@regentliving.test", "on_aptapt": False},
    {"match": "", "company": "Northgate Student Living", "contact": "Rae Okafor",  # fallback (Johnson & Broome)
     "email": "rae@northgate.test", "on_aptapt": False},
]

RENTERS = [
    ("Sam Park", "sam@example.test"),
    ("Jordan Lee", "jordan@example.test"),
    ("Priya Shah", "priya@example.test"),
    ("Alex Kim", "alex@example.test"),
    ("Taylor Brooks", "taylor@example.test"),
]


def load_buildings():
    if not BUILDINGS_JSON.exists():
        raise RuntimeError(f"Missing {BUILDINGS_JSON}. It holds the real building data.")
    return json.loads(BUILDINGS_JSON.read_text())


PREFERRED_INFO_SOURCES = ("downtownmadison.org", "antunovich.com")


def info_url(building):
    """The official site if there is one, otherwise the most neutral public page about the project."""
    if building.get("website"):
        return building["website"]
    sources = building.get("sources") or []
    return next((u for pref in PREFERRED_INFO_SOURCES for u in sources if pref in u), sources[0] if sources else None)



def sync_building_links(db):
    """Refresh public property links without resetting existing renters or groups."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(apartments)")}
    if "info_url" not in columns:
        db.execute("ALTER TABLE apartments ADD COLUMN info_url TEXT")
    if "sources" not in columns:
        db.execute("ALTER TABLE apartments ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
    for building in load_buildings():
        db.execute(
            "UPDATE apartments SET website = ?, info_url = ?, sources = ? WHERE slug = ?",
            (building.get("website"), info_url(building), json.dumps(building.get("sources") or []), building["slug"]),
        )
    # Older demos include this proposed project instead of Axton. Keep its identity
    # and groups intact, and link to the City's project submission.
    db.execute(
        "UPDATE apartments SET info_url = ? WHERE slug = ? AND website IS NULL AND info_url IS NULL",
        ("https://madison.legistar.com/View.ashx?G=D66739FE-4C3C-468C-A9F0-0198EFAA8EF8&GUID=52F16A72-B9DE-4437-BBB7-8A4702DC386E&ID=13342289&M=F", "atmosphere-on-mifflin"),
    )
    db.commit()


def manager_for(building):
    key = f"{building['slug']} {building['name']}".lower()
    return next(m for m in MANAGERS if m["match"] in key)


def seed(db):
    init_schema(db)
    db.executemany("INSERT INTO zip_centroids (zip, label, lat, lng) VALUES (?, ?, ?, ?)", ZIPS)
    pw = generate_password_hash(DEMO_PASSWORD)

    renter = {
        name.split()[0].lower(): db.execute(
            "INSERT INTO users (name, email, role, password_hash) VALUES (?, ?, 'renter', ?)", (name, email, pw)
        ).lastrowid
        for name, email in RENTERS
    }
    agents = {}
    apt = {}
    for b in load_buildings():
        mgr = manager_for(b)
        agent_id = db.execute(
            "INSERT INTO users (name, email, role, company, password_hash) VALUES (?, ?, 'agent', ?, ?)",
            (mgr["contact"], mgr["email"], mgr["company"], pw),
        ).lastrowid
        agents[mgr["contact"].split()[0].lower()] = agent_id
        apt[mgr["match"] or "broome"] = db.execute(
            """INSERT INTO apartments (slug, name, address, neighborhood, zip, lat, lng, kind, leasing, stories, units,
               beds, developer, opening, status_text, floor_plans, rent_min, rent_max, rent_is_estimate, amenities,
               description, website, info_url, sources, photos, manager_company, contact_name, contact_email,
               agent_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (b["slug"], b["name"], re.sub(r",\s*Madison,?\s*WI.*$", "", b["address"]), b["neighborhood"], b["zip"], b["lat"], b["lng"], b["kind"],
             b["leasing"], b.get("stories"), b.get("units"), b.get("beds"), b.get("developer"), b.get("opening"),
             b.get("status"), json.dumps(b.get("floor_plans") or []), b.get("rent_min"), b.get("rent_max"),
             int(bool(b.get("rent_is_estimate", True))), json.dumps(b.get("amenities") or []),
             b.get("description") or "", b.get("website"), info_url(b), json.dumps(b.get("sources") or []),
             json.dumps(b.get("photos") or []),
             mgr["company"], mgr["contact"], mgr["email"], agent_id if mgr["on_aptapt"] else None),
        ).lastrowid

    _seed_scenarios(db, apt, renter, agents)
    db.commit()


def _commit(db, group_id, user_id):
    payments.record_commitment(db, group_id, user_id, "Visa", "4242")


def _seed_scenarios(db, apt, renter, agents):
    # Seven20 (manager on aptapt): committed group with a live offer at 6/10, plus exploratory chatter.
    s20 = apt.get("seven20")
    if s20:
        grads = svc.create_exploratory(db, renter["sam"], "UW grad students", s20)
        if apt.get("bella"):
            svc.add_to_shortlist(db, grads, apt["bella"], renter["sam"])
        for who in ("jordan", "priya", "alex"):
            svc.join_exploratory(db, grads, renter[who])
        svc.post_chat(db, grads, renter["priya"], "Anyone else starting at UW Health next spring?")
        svc.post_chat(db, grads, renter["sam"], "Me! That pool deck sells it. I just want a better price.")
        group = svc.get_or_create_open_committed(db, s20)["id"]
        _commit(db, group, renter["jordan"])
        _commit(db, group, renter["priya"])
        svc.add_fake_members(db, group, 1)
        svc.invite_agent(db, group, renter["jordan"], threshold=3)
        svc.add_fake_members(db, group, 3)
        svc.post_offer(db, group, agents["morgan"], "rent_discount", "$150 off monthly rent for 12 months",
                       "Any 1BR or 2BR on a 12-month lease. Move-in from May 2027.", 10)
        svc.post_chat(db, group, agents["morgan"], "Happy to see the interest! Hit 10 and the discount is locked for everyone.")
        svc.post_chat(db, group, renter["jordan"], "We're at 6. Four more and we lock in $150/mo off!")

    # The Bella (manager NOT on aptapt): group ask at 4/6. One more renter + Sam triggers the pitch.
    bella = apt.get("bella")
    if bella:
        group = svc.get_or_create_open_committed(db, bella)["id"]
        _commit(db, group, renter["taylor"])
        svc.set_ask(db, group, renter["taylor"], "waived_fees", "Waived application & admin fees + $75 off/mo",
                    "For every renter in the group on a 12-month lease.", 6)
        svc.add_fake_members(db, group, 3)
        svc.post_chat(db, group, renter["taylor"], "Isthmus Property Group isn't on aptapt yet. Once we hit 6, "
                                                   "aptapt pitches our ask to them directly.")

    # Theory Madison (manager on aptapt): small forming group, ready for the invite-the-agent step.
    theory = apt.get("theory")
    if theory:
        group = svc.get_or_create_open_committed(db, theory)["id"]
        _commit(db, group, renter["alex"])
        svc.add_fake_members(db, group, 1)
        svc.post_chat(db, group, renter["alex"], "One more and we can invite the leasing office. Tell your friends!")

    # Johnson & Broome: exploratory interest only.
    # A friend group weighing three student buildings before anyone commits.
    friends = svc.create_exploratory(db, renter["jordan"], "Sellery Hall friends", apt.get("broome"))
    for key in ("theory", "axton"):
        if apt.get(key):
            svc.add_to_shortlist(db, friends, apt[key], renter["jordan"])
    svc.join_exploratory(db, friends, renter["sam"])
    svc.add_fake_members(db, friends, 3)
    svc.post_chat(db, friends, renter["jordan"], "Theory and Axton are open now, ōLiv isn't built yet. "
                                                 "If we want a 5BR together, which one do we commit to?")
    svc.post_chat(db, friends, renter["sam"], "Theory already has a group forming. If we all commit there we'd "
                                              "fill it basically by ourselves.")
