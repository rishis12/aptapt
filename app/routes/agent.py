from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import deals
from .. import groups as svc
from ..db import get_db
from .auth import agent_required
from .groups import group_context

bp = Blueprint("agent", __name__, url_prefix="/agent")


@bp.get("/")
@agent_required
def dashboard():
    db = get_db()
    buildings = []
    for apt in db.execute("SELECT * FROM apartments WHERE agent_user_id = ? ORDER BY name", (g.user["id"],)):
        committed = []
        for grp in db.execute(
            "SELECT * FROM groups WHERE apartment_id = ? AND kind = 'committed' ORDER BY seq DESC", (apt["id"],)
        ):
            deal = deals.get_deal(db, grp["id"])
            committed.append({"group": grp, "count": svc.member_count(db, grp["id"]),
                              "offer": svc.current_offer(db, grp["id"]), "deal": deal,
                              "deal_counts": deals.deal_counts(db, deal["id"]) if deal else None})
        buildings.append({"apt": apt, "committed": committed, "stats": svc.apartment_stats(db, apt["id"])})
    todo = []
    for b in buildings:
        for c in b["committed"]:
            if c["group"]["status"] == "agent_invited":
                todo.append((b["apt"], c, "Make an offer"))
            elif c["deal"] and c["deal"]["status"] == "short":
                todo.append((b["apt"], c, "Decide on a short deal"))
            elif c["deal"] and c["deal"]["status"] == "reserving":
                todo.append((b["apt"], c, "Track reservations"))
    pitches = db.execute(
        """SELECT p.*, g.name AS group_name, a.name AS apartment_name,
           (SELECT COUNT(*) FROM memberships m WHERE m.group_id = p.group_id) AS size
           FROM pitches p JOIN groups g ON g.id = p.group_id JOIN apartments a ON a.id = g.apartment_id
           WHERE p.status = 'sent' AND p.to_email = ?""",
        (g.user["email"],),
    ).fetchall()
    return render_template("agent/dashboard.html", buildings=buildings, todo=todo, pitches=pitches)


@bp.get("/i/<token>")
@agent_required
def accept_invite(token):
    db = get_db()
    try:
        group_id = svc.claim_agent_invite(db, token, g.user["id"])
    except svc.GroupError as err:
        flash(str(err), "error")
        return redirect(url_for("agent.dashboard"))
    db.commit()
    flash("You're connected to this group. Make them an offer.", "success")
    return redirect(url_for("agent.group", group_id=group_id))


@bp.get("/groups/<int:group_id>")
@agent_required
def group(group_id):
    db = get_db()
    ctx = group_context(db, group_id)
    if not ctx["is_agent"]:
        abort(403)
    return render_template("agent/group.html", feed=svc.feed(db, group_id), **ctx)


def act(group_id, action, success):
    db = get_db()
    try:
        action(db)
        db.commit()
        flash(success, "success")
    except svc.GroupError as err:
        db.rollback()
        flash(str(err), "error")
    return redirect(url_for("agent.group", group_id=group_id))


@bp.post("/groups/<int:group_id>/offer")
@agent_required
def post_offer(group_id):
    f = request.form
    return act(group_id, lambda db: svc.post_offer(db, group_id, g.user["id"], f.get("incentive_type"), f.get("title"),
                                                   f.get("details"), f.get("required_size")),
               "Offer posted. The group sees it now.")


@bp.post("/groups/<int:group_id>/withdraw")
@agent_required
def withdraw_offer(group_id):
    return act(group_id, lambda db: svc.withdraw_offer(db, group_id, g.user["id"]), "Offer withdrawn.")


@bp.post("/groups/<int:group_id>/nudge/<int:user_id>")
@agent_required
def nudge(group_id, user_id):
    def run(db):
        deal = deals.get_deal(db, group_id)
        deals.nudge(db, deal["id"] if deal else 0, g.user["id"], user_id)
    return act(group_id, run, "Reminder posted in the group chat.")


@bp.post("/groups/<int:group_id>/decide")
@agent_required
def decide(group_id):
    action = request.form.get("action")
    labels = {"honor": "Deal honored.", "extend": "Deal extended by 48 hours.", "cancel": "Deal cancelled."}

    def run(db):
        deal = deals.get_deal(db, group_id)
        deals.resolve_short(db, deal["id"] if deal else 0, g.user["id"], action)
    return act(group_id, run, labels.get(action, "Done."))
