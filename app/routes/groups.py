import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import deals, payments
from .. import groups as svc
from ..db import get_db
from .auth import login_required, renter_required

bp = Blueprint("groups", __name__)


def group_context(db, group_id):
    """Everything the group page, its live partials, and the agent view need."""
    try:
        group = svc.get_group(db, group_id)
    except svc.GroupError:
        abort(404)
    apt = svc.get_apartment(db, group["apartment_id"]) if group["apartment_id"] else None
    user = g.user
    user_id = user["id"] if user else None
    count = svc.member_count(db, group_id)
    offer = svc.current_offer(db, group_id)
    is_member = user_id is not None and svc.is_member(db, group_id, user_id)
    is_agent = bool(apt and user and user["role"] == "agent" and apt["agent_user_id"] == user_id)
    threshold = current_app.config["AGENT_INVITE_THRESHOLD"]
    ctx = {
        "group": group, "apt": apt, "count": count, "members": svc.members(db, group_id), "offer": offer,
        "is_member": is_member, "is_agent": is_agent, "can_chat": is_member or is_agent, "threshold": threshold,
        "manager_on_aptapt": bool(apt and apt["agent_user_id"]),
        "floor_plans": json.loads(apt["floor_plans"]) if apt else [],
        "photos": json.loads(apt["photos"]) if apt else [],
        "shortlist": None, "addable": None,
        "agent_invite": None, "friend_invite": None, "pitch": None, "counter": None, "tally": None, "my_vote": None,
        "deal": None, "deal_counts": None, "my_deal": None, "roster": None, "next_group": None, "committed_group": None,
        "my_deposit": None, "commit_open": False,
    }
    # The window meter's size: the offer/ask target, else the agent-invite threshold.
    ctx["target"] = offer["required_size"] if offer else threshold
    ctx["needed"] = max(0, ctx["target"] - count)

    if group["kind"] == "committed":
        if is_member or is_agent:
            ctx["agent_invite"] = svc.agent_invite_token(db, group_id)
        pitch = deals.latest_pitch(db, group_id)
        if pitch and pitch["status"] in ("sent", "countered"):
            ctx["pitch"] = pitch
            if pitch["status"] == "countered":
                ctx["counter"] = db.execute("SELECT * FROM offers WHERE id = ?", (pitch["counter_offer_id"],)).fetchone()
                ctx["tally"] = deals.vote_tally(db, pitch["id"])
                ctx["my_vote"] = deals.user_vote(db, pitch["id"], user_id) if user_id else None
        deal = deals.get_deal(db, group_id)
        if deal:
            ctx["deal"] = deal
            ctx["deal_counts"] = deals.deal_counts(db, deal["id"])
            ctx["roster"] = deals.roster(db, deal["id"])
            if user_id:
                ctx["my_deal"] = deals.deal_member(db, deal["id"], user_id)
        if group["status"] in ("unlocked", "expired"):
            ctx["next_group"] = svc.open_committed_group(db, group["apartment_id"])
        if is_member:
            ctx["my_deposit"] = payments.held_deposit(db, group_id, user_id, "commitment")
        try:
            if user and user["role"] == "renter" and not is_member:
                svc.can_commit(db, group_id, user_id)
                ctx["commit_open"] = True
        except svc.GroupError:
            pass
    else:
        ctx["shortlist"] = svc.shortlist(db, group_id)
        listed = {i["apt"]["id"] for i in ctx["shortlist"]}
        ctx["addable"] = [a for a in db.execute("SELECT id, name FROM apartments ORDER BY name") if a["id"] not in listed]

    if is_member and group["status"] not in ("unlocked", "expired"):
        ctx["friend_invite"] = svc.get_or_create_invite(db, group_id, user_id, "friend")
        db.commit()
    return ctx


@bp.get("/groups/<int:group_id>")
def show(group_id):
    db = get_db()
    ctx = group_context(db, group_id)
    return render_template("group.html", feed=svc.feed(db, group_id), **ctx)


@bp.get("/groups/<int:group_id>/status")
def status(group_id):
    return render_template("partials/group_status.html", **group_context(get_db(), group_id))


@bp.get("/groups/<int:group_id>/feed")
def feed(group_id):
    db = get_db()
    try:
        svc.get_group(db, group_id)
    except svc.GroupError:
        abort(404)
    return render_template("partials/feed.html", feed=svc.feed(db, group_id), group_id=group_id)


@bp.post("/groups/<int:group_id>/messages")
@login_required
def post_message(group_id):
    db = get_db()
    if not group_context(db, group_id)["can_chat"]:
        abort(403)
    try:
        svc.post_chat(db, group_id, g.user["id"], request.form.get("body"))
        db.commit()
    except svc.GroupError as err:
        if not request.headers.get("HX-Request"):
            flash(str(err), "error")
    if request.headers.get("HX-Request"):
        return render_template("partials/feed.html", feed=svc.feed(db, group_id), group_id=group_id)
    return redirect(request.form.get("back") or url_for("groups.show", group_id=group_id))


def act(group_id, action, success, back=None):
    db = get_db()
    try:
        action(db)
        db.commit()
        if success:
            flash(success, "success")
    except (svc.GroupError, payments.PaymentError) as err:
        db.rollback()
        flash(str(err), "error")
    return redirect(back or url_for("groups.show", group_id=group_id))


@bp.post("/groups/<int:group_id>/join")
@renter_required
def join(group_id):
    """Exploratory groups are free; committed groups go through the $100 checkout."""
    db = get_db()
    group = svc.get_group(db, group_id)
    if group["kind"] == "committed":
        try:
            token = payments.start_commitment(db, group_id, g.user["id"])
        except svc.GroupError as err:
            flash(str(err), "error")
            return redirect(url_for("groups.show", group_id=group_id))
        db.commit()
        return redirect(url_for("checkout.show", token=token))
    return act(group_id, lambda db: svc.join_exploratory(db, group_id, g.user["id"]), "You joined the group.")


@bp.post("/groups/<int:group_id>/leave")
@renter_required
def leave(group_id):
    db = get_db()
    if svc.get_group(db, group_id)["kind"] == "committed":
        return act(group_id, lambda db: svc.leave_committed(db, group_id, g.user["id"]),
                   "You left the group. Your $100 deposit was forfeited.")
    return act(group_id, lambda db: svc.leave_exploratory(db, group_id, g.user["id"]), "You left the group.")


@bp.post("/groups/new")
@renter_required
def create():
    db = get_db()
    group_id = svc.create_exploratory(db, g.user["id"], request.form.get("name"))
    db.commit()
    flash("Group started. Add buildings to its shortlist and invite friends.", "success")
    return redirect(url_for("groups.show", group_id=group_id))


@bp.post("/groups/<int:group_id>/shortlist")
@renter_required
def shortlist_add(group_id):
    apartment_id = request.form.get("apartment_id", type=int)
    back = request.form.get("back") or None
    return act(group_id, lambda db: svc.add_to_shortlist(db, group_id, apartment_id, g.user["id"]),
               "Added to the shortlist.", back)


@bp.post("/groups/<int:group_id>/shortlist/<int:apartment_id>/remove")
@renter_required
def shortlist_remove(group_id, apartment_id):
    return act(group_id, lambda db: svc.remove_from_shortlist(db, group_id, apartment_id, g.user["id"]),
               "Removed from the shortlist.")


@bp.post("/groups/<int:group_id>/invite-agent")
@renter_required
def invite_agent(group_id):
    threshold = current_app.config["AGENT_INVITE_THRESHOLD"]
    return act(group_id, lambda db: svc.invite_agent(db, group_id, g.user["id"], threshold),
               "Leasing office invited. Send them the link below.")


@bp.post("/groups/<int:group_id>/ask")
@renter_required
def set_ask(group_id):
    f = request.form
    return act(group_id, lambda db: svc.set_ask(db, group_id, g.user["id"], f.get("incentive_type"), f.get("title"),
                                                 f.get("details"), f.get("required_size")),
               "Ask saved. We'll pitch it to the manager when the group reaches its size.")


@bp.post("/groups/<int:group_id>/vote")
@renter_required
def vote(group_id):
    db = get_db()
    pitch = deals.latest_pitch(db, group_id)
    choice = request.form.get("choice")
    msg = "You accepted the counter-offer." if choice == "accept" else \
        "You rejected the counter-offer. If the vote closes that way, you're released and refunded."
    return act(group_id, lambda db: deals.vote(db, pitch["id"] if pitch else 0, g.user["id"], choice), msg)


@bp.post("/groups/<int:group_id>/reserve")
@renter_required
def reserve(group_id):
    db = get_db()
    try:
        token = payments.start_holding(db, group_id, g.user["id"])
    except payments.PaymentError as err:
        flash(str(err), "error")
        return redirect(url_for("groups.show", group_id=group_id))
    db.commit()
    return redirect(url_for("checkout.show", token=token))


@bp.route("/groups/<int:group_id>/sign", methods=["GET", "POST"])
@renter_required
def sign(group_id):
    db = get_db()
    ctx = group_context(db, group_id)
    if not ctx["deal"] or not ctx["my_deal"] or ctx["my_deal"]["status"] != "reserved":
        flash("Reserve your unit before signing the lease.", "error")
        return redirect(url_for("groups.show", group_id=group_id))
    if request.method == "POST":
        try:
            deals.sign(db, ctx["deal"]["id"], g.user["id"], request.form.get("signature"))
            db.commit()
            flash("Lease signed. Your $100 commitment deposit is on its way back.", "success")
            return redirect(url_for("groups.show", group_id=group_id))
        except svc.GroupError as err:
            flash(str(err), "error")
    return render_template("sign.html", **ctx)


@bp.get("/i/<token>")
def invite(token):
    db = get_db()
    inv = svc.find_invite(db, token)
    if inv is None:
        abort(404)
    if inv["kind"] == "agent":
        return redirect(url_for("agent.accept_invite", token=token))
    ctx = group_context(db, inv["group_id"])
    inviter = db.execute("SELECT name FROM users WHERE id = ?", (inv["created_by"],)).fetchone()
    return render_template("invite.html", inviter=inviter, **ctx)


@bp.get("/me")
@login_required
def my_groups():
    db = get_db()
    rows = db.execute(
        """SELECT g.*, a.name AS apartment_name, a.slug FROM memberships m
           JOIN groups g ON g.id = m.group_id LEFT JOIN apartments a ON a.id = g.apartment_id
           WHERE m.user_id = ? ORDER BY g.kind = 'committed' DESC, m.joined_at DESC""",
        (g.user["id"],),
    ).fetchall()
    items = [{"group": r, "count": svc.member_count(db, r["id"]), "offer": svc.current_offer(db, r["id"]),
              "shortlist": [i["apt"]["name"] for i in svc.shortlist(db, r["id"])] if r["kind"] == "exploratory" else None}
             for r in rows]
    return render_template("me.html", items=items, ledger=payments.ledger(db, g.user["id"]))
