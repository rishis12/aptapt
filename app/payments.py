"""Simulated, test-mode payments. No real money moves and real card numbers are refused.

Only the test cards below are accepted. We store brand + last 4, never the full number.
"""

import re
import secrets
from datetime import date

from . import clock
from . import groups as svc

TEST_CARDS = {
    "4242424242424242": ("Visa", "succeed"),
    "5555555555554444": ("Mastercard", "succeed"),
    "4000002760003184": ("Visa", "verify"),
    "4000000000000002": ("Visa", "decline"),
}
HOLDING_CENTS = 25000


class PaymentError(Exception):
    """Shown to the user on the checkout form."""


def cents(amount_cents):
    return f"${amount_cents / 100:,.2f}"


# --- ledger ----------------------------------------------------------------

def _deposit(db, user_id, group_id, kind, amount_cents, brand, last4):
    return db.execute(
        """INSERT INTO deposits (user_id, group_id, kind, amount_cents, status, card_brand, card_last4)
           VALUES (?, ?, ?, ?, 'held', ?, ?)""",
        (user_id, group_id, kind, amount_cents, brand, last4),
    ).lastrowid


def resolve(db, deposit_id, status, reason):
    db.execute(
        "UPDATE deposits SET status = ?, reason = ?, resolved_at = ? WHERE id = ? AND status = 'held'",
        (status, reason, clock.ts(clock.now()), deposit_id),
    )


def held_deposit(db, group_id, user_id, kind):
    return db.execute(
        "SELECT * FROM deposits WHERE group_id = ? AND user_id = ? AND kind = ? AND status = 'held'",
        (group_id, user_id, kind),
    ).fetchone()


def resolve_commitment(db, group_id, user_id, status, reason):
    dep = held_deposit(db, group_id, user_id, "commitment")
    if dep:
        resolve(db, dep["id"], status, reason)


def ledger(db, user_id):
    return db.execute(
        """SELECT d.*, g.name AS group_name, a.name AS apartment_name FROM deposits d
           JOIN groups g ON g.id = d.group_id JOIN apartments a ON a.id = g.apartment_id
           WHERE d.user_id = ? ORDER BY d.id DESC""",
        (user_id,),
    ).fetchall()


def record_commitment(db, group_id, user_id, brand, last4):
    """Record a successful $100 commitment and add the member. Returns the next group if a deal unlocked."""
    svc.can_commit(db, group_id, user_id)
    _deposit(db, user_id, group_id, "commitment", svc.COMMITMENT_CENTS, brand, last4)
    return svc.add_committed_member(db, group_id, user_id)


# --- checkout sessions -----------------------------------------------------

def get_checkout(db, token):
    return db.execute("SELECT * FROM checkouts WHERE token = ?", (token,)).fetchone()


def start_commitment(db, group_id, user_id):
    svc.can_commit(db, group_id, user_id)
    return _start(db, user_id, group_id, "commitment", svc.COMMITMENT_CENTS, "pay")


def start_holding(db, group_id, user_id):
    from . import deals
    deal = deals.get_deal(db, group_id)
    member = deals.deal_member(db, deal["id"], user_id) if deal else None
    if not member or member["status"] != "pending" or deal["status"] != "reserving":
        raise PaymentError("There's nothing to reserve here right now.")
    return _start(db, user_id, group_id, "holding", HOLDING_CENTS, "plan")


def _start(db, user_id, group_id, purpose, amount_cents, step):
    open_row = db.execute(
        "SELECT token FROM checkouts WHERE user_id = ? AND group_id = ? AND purpose = ? AND status != 'succeeded'",
        (user_id, group_id, purpose),
    ).fetchone()
    if open_row:
        return open_row["token"]
    token = secrets.token_urlsafe(12)
    db.execute(
        """INSERT INTO checkouts (token, user_id, group_id, purpose, amount_cents, status, step)
           VALUES (?, ?, ?, ?, ?, 'open', ?)""",
        (token, user_id, group_id, purpose, amount_cents, step),
    )
    return token


def save_plan(db, checkout, floor_plan, move_in, floor_plans, earliest_move_in):
    if floor_plan not in floor_plans:
        raise PaymentError("Choose a floor plan.")
    try:
        move = date.fromisoformat(move_in or "")
    except ValueError:
        raise PaymentError("Choose a move-in date.") from None
    if earliest_move_in and move < earliest_move_in:
        raise PaymentError(f"Move-in can't be before {clock.strftime(earliest_move_in, '%B %-d, %Y')}.")
    db.execute("UPDATE checkouts SET floor_plan = ?, move_in = ?, step = 'application' WHERE id = ?",
               (floor_plan, move.isoformat(), checkout["id"]))


def save_application(db, checkout, form):
    """The application is simulated: we validate shape and discard everything. Nothing personal is stored."""
    required = ("legal_name", "dob", "current_address", "monthly_income")
    if any(not (form.get(k) or "").strip() for k in required):
        raise PaymentError("Fill in every application field. Made-up values are fine, this is a demo.")
    if not re.fullmatch(r"\d{4}", (form.get("ssn_last4") or "").strip()):
        raise PaymentError("Enter any 4 digits for the SSN field (use 0000; never a real SSN).")
    if not form.get("consent"):
        raise PaymentError("Check the box to authorize the (simulated) screening.")
    db.execute("UPDATE checkouts SET step = 'pay' WHERE id = ?", (checkout["id"],))


def _card_digits(number):
    return re.sub(r"[\s-]", "", number or "")


def pay(db, checkout, number, exp, cvc, postal):
    """Returns 'succeeded' or 'needs_verification'. Raises PaymentError on any problem."""
    if checkout["status"] in ("succeeded",):
        return "succeeded"
    digits = _card_digits(number)
    card = TEST_CARDS.get(digits)
    if card is None:
        raise PaymentError("Test mode: this demo only accepts test cards. Use 4242 4242 4242 4242. "
                           "Never enter a real card.")
    m = re.fullmatch(r"\s*(\d{2})\s*/\s*(\d{2})\s*", exp or "")
    if not m or not 1 <= int(m.group(1)) <= 12:
        raise PaymentError("Enter the expiry as MM/YY.")
    expiry = (2000 + int(m.group(2)), int(m.group(1)))
    today = date.today()
    if expiry < (today.year, today.month):
        raise PaymentError("That card has expired.")
    if not re.fullmatch(r"\d{3,4}", (cvc or "").strip()):
        raise PaymentError("Enter the 3-digit security code.")
    if not re.fullmatch(r"\d{5}", (postal or "").strip()):
        raise PaymentError("Enter a 5-digit ZIP code.")

    brand, behavior = card
    if behavior == "decline":
        db.execute("UPDATE checkouts SET status = 'failed', error = ? WHERE id = ?",
                   ("Your card was declined.", checkout["id"]))
        raise PaymentError("Your card was declined. Try 4242 4242 4242 4242.")
    db.execute("UPDATE checkouts SET card_brand = ?, card_last4 = ?, error = NULL WHERE id = ?",
               (brand, digits[-4:], checkout["id"]))
    if behavior == "verify":
        db.execute("UPDATE checkouts SET status = 'needs_verification' WHERE id = ?", (checkout["id"],))
        return "needs_verification"
    _succeed(db, checkout["id"])
    return "succeeded"


def verify(db, checkout, approved):
    if checkout["status"] != "needs_verification":
        raise PaymentError("Nothing to verify.")
    if not approved:
        db.execute("UPDATE checkouts SET status = 'failed', error = 'Verification failed.' WHERE id = ?",
                   (checkout["id"],))
        raise PaymentError("Your bank couldn't verify the payment. Try again.")
    _succeed(db, checkout["id"])


def _succeed(db, checkout_id):
    from . import deals
    co = db.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
    if co["purpose"] == "commitment":
        record_commitment(db, co["group_id"], co["user_id"], co["card_brand"], co["card_last4"])
        dep = held_deposit(db, co["group_id"], co["user_id"], "commitment")
    else:
        deal = deals.get_deal(db, co["group_id"])
        _deposit(db, co["user_id"], co["group_id"], "holding", co["amount_cents"], co["card_brand"], co["card_last4"])
        deals.mark_reserved(db, deal["id"], co["user_id"], co["floor_plan"], co["move_in"])
        dep = held_deposit(db, co["group_id"], co["user_id"], "holding")
    db.execute("UPDATE checkouts SET status = 'succeeded', deposit_id = ? WHERE id = ?",
               (dep["id"] if dep else None, checkout_id))
