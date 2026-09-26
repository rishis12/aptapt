"""What a property manager who isn't on aptapt sees when a group pitches them (via the simulated email)."""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import deals
from .. import groups as svc
from ..db import get_db

bp = Blueprint("pitch", __name__, url_prefix="/pitch")


def load(token):
    db = get_db()
    pitch = deals.get_pitch(db, token)
    if pitch is None:
        abort(404)
    group = svc.get_group(db, pitch["group_id"])
    apt = svc.get_apartment(db, group["apartment_id"])
    ask = db.execute("SELECT * FROM offers WHERE id = ?", (pitch["ask_offer_id"],)).fetchone()
    return db, pitch, group, apt, ask


@bp.get("/<token>")
def show(token):
    db, pitch, group, apt, ask = load(token)
    return render_template("pitch.html", pitch=pitch, group=group, apt=apt, ask=ask,
                           count=svc.member_count(db, group["id"]), members=svc.members(db, group["id"]))


@bp.get("/<token>/email")
def email(token):
    """The simulated email aptapt 'sent' to the manager."""
    db, pitch, group, apt, ask = load(token)
    return render_template("pitch_email.html", pitch=pitch, group=group, apt=apt, ask=ask,
                           count=svc.member_count(db, group["id"]))


@bp.post("/<token>")
def respond(token):
    db, pitch, group, apt, ask = load(token)
    if g.user is None or g.user["role"] != "agent":
        flash("Create a leasing agent account (or sign in) to respond to this group.", "info")
        return redirect(url_for("auth.signup", role="agent", next=url_for("pitch.show", token=token)))
    f = request.form
    action = f.get("action")
    try:
        deals.respond_to_pitch(db, token, g.user["id"], action, f.get("incentive_type"), f.get("title"),
                               f.get("details"), f.get("required_size"))
        db.commit()
    except svc.GroupError as err:
        db.rollback()
        flash(str(err), "error")
        return redirect(url_for("pitch.show", token=token))
    flash({"accept": "You accepted. The group's deal is unlocked and reservations are open.",
           "counter": "Counter-offer sent. The group has 48 hours to vote.",
           "decline": "You declined the group's ask."}.get(action, "Response sent."), "success")
    return redirect(url_for("agent.group", group_id=group["id"]) if action != "decline" else url_for("agent.dashboard"))
