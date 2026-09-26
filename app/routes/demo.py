"""Demo-only controls so one person can run the whole story on stage."""

from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for

from .. import clock, deals
from .. import groups as svc
from ..db import get_db
from ..seed import seed

bp = Blueprint("demo", __name__, url_prefix="/demo")


@bp.before_request
def demo_only():
    if not current_app.config["DEMO_MODE"]:
        abort(404)


def back(group_id):
    return redirect(request.form.get("back") or url_for("groups.show", group_id=group_id))


def run(group_id, fn, message):
    db = get_db()
    try:
        fn(db)
        db.commit()
        flash(message, "info")
    except svc.GroupError as err:
        db.rollback()
        flash(str(err), "error")
    return back(group_id)


@bp.post("/groups/<int:group_id>/add-members")
def add_members(group_id):
    n = max(1, min(request.form.get("n", 3, type=int), 20))
    return run(group_id, lambda db: svc.add_fake_members(db, group_id, n), f"Simulated {n} renters joining.")


@bp.post("/groups/<int:group_id>/due-now")
def due_now(group_id):
    def fn(db):
        db.execute("UPDATE groups SET due_at = ? WHERE id = ?", (clock.ts(clock.now()), group_id))
        deals.run_deadlines(db)
    return run(group_id, fn, "Jumped to the group's due date.")


@bp.post("/groups/<int:group_id>/end-window")
def end_window(group_id):
    def fn(db):
        deal = deals.get_deal(db, group_id)
        if deal:
            db.execute("UPDATE deals SET window_ends_at = ? WHERE id = ?", (clock.ts(clock.now()), deal["id"]))
            deals.run_deadlines(db)
    return run(group_id, fn, "Jumped to the end of the reservation window.")


@bp.post("/groups/<int:group_id>/demo-renters-sign")
def demo_renters_sign(group_id):
    def fn(db):
        deal = deals.get_deal(db, group_id)
        if deal:
            deals.sign_all_demo(db, deal["id"])
    return run(group_id, fn, "Simulated renters reserved and signed.")


@bp.post("/groups/<int:group_id>/manager-responds")
def manager_responds(group_id):
    """Answer the group's pitch as the building's fictional contact."""
    action = request.form.get("action")

    def fn(db):
        pitch = deals.latest_pitch(db, group_id)
        if not pitch or pitch["status"] != "sent":
            raise svc.GroupError("There's no pitch waiting for a response.")
        contact = db.execute("SELECT id FROM users WHERE email = ?", (pitch["to_email"],)).fetchone()
        if contact is None:
            raise svc.GroupError("The manager has no demo account.")
        ask = db.execute("SELECT * FROM offers WHERE id = ?", (pitch["ask_offer_id"],)).fetchone()
        counter = {"incentive_type": "waived_fees", "title": "Waived application & admin fees",
                   "details": "No rent discount, but every fee is waived.", "required_size": ask["required_size"]}
        deals.respond_to_pitch(db, pitch["token"], contact["id"], action,
                               **(counter if action == "counter" else {}))
    return run(group_id, fn, f"The manager responded: {action}.")


@bp.post("/reset")
def reset():
    seed(get_db())
    session.pop("user_id", None)
    flash("Demo data reset.", "info")
    return redirect(url_for("apartments.index"))
