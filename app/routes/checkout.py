"""Test-mode checkout. Looks and behaves like a real one; only test cards are accepted, nothing is charged."""

import json
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import deals, payments
from .. import groups as svc
from ..db import get_db
from .auth import login_required

bp = Blueprint("checkout", __name__, url_prefix="/checkout")


def load(token):
    db = get_db()
    co = payments.get_checkout(db, token)
    if co is None or co["user_id"] != g.user["id"]:
        abort(404)
    group = svc.get_group(db, co["group_id"])
    apt = svc.get_apartment(db, group["apartment_id"])
    offer = svc.current_offer(db, group["id"])
    return db, co, group, apt, offer


def earliest_move_in(apt):
    """Buildings still under construction can't be moved into before they open (best effort)."""
    opening = (apt["opening"] or "").lower()
    for year in range(date.today().year, date.today().year + 4):
        if str(year) in opening:
            months = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                      "october", "november", "december"]
            month = next((i + 1 for i, m in enumerate(months) if m in opening or m[:3] in opening.split()), 8)
            return max(date(year, month, 1), date.today())
    return date.today()


def render(co, group, apt, offer, **extra):
    return render_template(
        "checkout.html", co=co, group=group, apt=apt, offer=offer, floor_plans=json.loads(apt["floor_plans"] or "[]"),
        photos=json.loads(apt["photos"] or "[]"), earliest=earliest_move_in(apt), test_cards=payments.TEST_CARDS,
        **extra,
    )


@bp.get("/<token>")
@login_required
def show(token):
    db, co, group, apt, offer = load(token)
    if co["status"] == "succeeded":
        return redirect(url_for("checkout.receipt", token=token))
    if co["status"] == "needs_verification":
        return render(co, group, apt, offer, verifying=True)
    return render(co, group, apt, offer)


@bp.post("/<token>/plan")
@login_required
def plan(token):
    db, co, group, apt, offer = load(token)
    try:
        payments.save_plan(db, co, request.form.get("floor_plan"), request.form.get("move_in"),
                           json.loads(apt["floor_plans"] or "[]"), earliest_move_in(apt))
        db.commit()
    except payments.PaymentError as err:
        flash(str(err), "error")
    return redirect(url_for("checkout.show", token=token))


@bp.post("/<token>/application")
@login_required
def application(token):
    db, co, group, apt, offer = load(token)
    try:
        payments.save_application(db, co, request.form)  # validated then discarded
        db.commit()
    except payments.PaymentError as err:
        flash(str(err), "error")
    return redirect(url_for("checkout.show", token=token))


@bp.post("/<token>/back")
@login_required
def back(token):
    db, co, group, apt, offer = load(token)
    prev = {"application": "plan", "pay": "application"}.get(co["step"])
    if co["purpose"] == "holding" and prev and co["status"] != "succeeded":
        db.execute("UPDATE checkouts SET step = ? WHERE id = ?", (prev, co["id"]))
        db.commit()
    return redirect(url_for("checkout.show", token=token))


@bp.post("/<token>/pay")
@login_required
def pay(token):
    db, co, group, apt, offer = load(token)
    f = request.form
    try:
        result = payments.pay(db, co, f.get("number"), f.get("exp"), f.get("cvc"), f.get("postal"))
        db.commit()
    except (payments.PaymentError, svc.GroupError) as err:
        db.commit()  # keep the 'failed' status for a declined card
        return render(payments.get_checkout(db, token), group, apt, offer, error=str(err),
                      card={"exp": f.get("exp", ""), "postal": f.get("postal", "")}), 422
    if result == "needs_verification":
        return redirect(url_for("checkout.show", token=token))
    return redirect(url_for("checkout.receipt", token=token))


@bp.post("/<token>/verify")
@login_required
def verify(token):
    db, co, group, apt, offer = load(token)
    try:
        payments.verify(db, co, request.form.get("decision") == "approve")
        db.commit()
    except (payments.PaymentError, svc.GroupError) as err:
        db.commit()
        flash(str(err), "error")
        return redirect(url_for("checkout.show", token=token))
    return redirect(url_for("checkout.receipt", token=token))


@bp.get("/<token>/receipt")
@login_required
def receipt(token):
    db, co, group, apt, offer = load(token)
    if co["status"] != "succeeded":
        return redirect(url_for("checkout.show", token=token))
    deposit = db.execute("SELECT * FROM deposits WHERE id = ?", (co["deposit_id"],)).fetchone()
    deal = deals.get_deal(db, group["id"])
    return render_template("receipt.html", co=co, group=group, apt=apt, offer=offer, deposit=deposit, deal=deal,
                           count=svc.member_count(db, group["id"]))
