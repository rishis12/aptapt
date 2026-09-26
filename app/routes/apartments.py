import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import geo, payments
from .. import groups as svc
from ..db import get_db
from .auth import renter_required

bp = Blueprint("apartments", __name__)


def get_building(db, slug):
    apt = db.execute("SELECT * FROM apartments WHERE slug = ?", (slug,)).fetchone()
    if apt is None:
        abort(404)
    return apt


def cover(apt):
    photos = json.loads(apt["photos"] or "[]")
    return photos[0] if photos else None


def building_card(db, apt, distance=None):
    return {"apt": apt, "distance": distance, "stats": svc.apartment_stats(db, apt["id"]), "cover": cover(apt)}


@bp.get("/")
def index():
    db = get_db()
    buildings = [building_card(db, a) for a in db.execute("SELECT * FROM apartments ORDER BY name")]
    # Lead with the building that has the most momentum: a live deal first, then committed renters.
    buildings.sort(key=lambda b: (b["stats"]["offer"] is None, -b["stats"]["committed_count"], -b["stats"]["interested"]))
    return render_template("index.html", buildings=buildings, zips=geo.supported_zips(db),
                           radius_choices=geo.RADIUS_CHOICES, zip=current_app.config["DEFAULT_ZIP"], radius=2)


@bp.get("/search")
def search():
    db = get_db()
    zip_code = request.args.get("zip", "").strip() or current_app.config["DEFAULT_ZIP"]
    radius = geo.parse_radius(request.args.get("radius"))
    center = geo.zip_center(db, zip_code)
    if center is None:
        flash(f"aptapt only covers Madison, WI for now, and {zip_code} isn't a Madison ZIP code.", "error")
        center = geo.zip_center(db, current_app.config["DEFAULT_ZIP"])
        zip_code = center["zip"]
    results = [building_card(db, apt, d) for apt, d in geo.apartments_within(db, center, radius)]
    markers = [{
        "slug": r["apt"]["slug"], "name": r["apt"]["name"], "lat": r["apt"]["lat"], "lng": r["apt"]["lng"],
        "url": url_for("apartments.detail", slug=r["apt"]["slug"]),
        "committed": r["stats"]["committed_count"], "target": r["stats"]["offer"]["required_size"] if r["stats"]["offer"] else None,
    } for r in results]
    return render_template("search.html", results=results, markers=markers, center=dict(center), zip=zip_code,
                           radius=radius, zips=geo.supported_zips(db), radius_choices=geo.RADIUS_CHOICES)


@bp.get("/buildings/<slug>")
def detail(slug):
    db = get_db()
    apt = get_building(db, slug)
    user_id = g.user["id"] if g.user else None
    stats = svc.apartment_stats(db, apt["id"])
    committed = stats["committed_group"]
    exploratory = [
        {"group": grp, "count": grp["size"], "is_member": bool(user_id and svc.is_member(db, grp["id"], user_id))}
        for grp in svc.groups_considering(db, apt["id"])
    ]
    considering_ids = {e["group"]["id"] for e in exploratory}
    my_other_groups = [grp for grp in svc.user_groups(db, user_id) if grp["id"] not in considering_ids] if user_id else []
    history = [
        {"group": grp, "count": svc.member_count(db, grp["id"]), "offer": svc.current_offer(db, grp["id"])}
        for grp in db.execute(
            """SELECT * FROM groups WHERE apartment_id = ? AND kind = 'committed' AND status IN ('unlocked', 'expired')
               ORDER BY seq DESC""", (apt["id"],))
    ]
    return render_template(
        "building.html", apt=apt, stats=stats, committed=committed, exploratory=exploratory, history=history,
        photos=json.loads(apt["photos"] or "[]"), floor_plans=json.loads(apt["floor_plans"] or "[]"),
        amenities=json.loads(apt["amenities"] or "[]"),
        committed_is_member=bool(committed and user_id and svc.is_member(db, committed["id"], user_id)),
        my_other_groups=my_other_groups,
    )


@bp.post("/buildings/<slug>/commit")
@renter_required
def commit(slug):
    """Start the $100 commitment checkout for the building's open committed group."""
    db = get_db()
    apt = get_building(db, slug)
    group = svc.get_or_create_open_committed(db, apt["id"])
    try:
        token = payments.start_commitment(db, group["id"], g.user["id"])
    except svc.GroupError as err:
        flash(str(err), "error")
        return redirect(url_for("groups.show", group_id=group["id"]))
    db.commit()
    return redirect(url_for("checkout.show", token=token))


@bp.post("/buildings/<slug>/groups")
@renter_required
def create_group(slug):
    db = get_db()
    apt = get_building(db, slug)
    group_id = svc.create_exploratory(db, g.user["id"], request.form.get("name"), apt["id"])
    db.commit()
    flash(f"Group started with {apt['name']} on its shortlist. Share the invite link so friends can join.", "success")
    return redirect(url_for("groups.show", group_id=group_id))
