"""What happens around an unlock: pitches to off-platform managers, and the deal itself.

Deal: 72h reservation window -> members reserve ($250 holding deposit) and sign -> closed.
At window end, anyone who hasn't reserved is dropped (commitment forfeited). If that leaves the group
under target, the agent decides: honor, extend 48h (new renters may join), or cancel (all refunded).
"""

import secrets

from . import clock, payments
from . import groups as svc

RESERVE_WINDOW_HOURS = 72
EXTENSION_HOURS = 48
PITCH_RESPONSE_DAYS = 5
VOTE_HOURS = 48


# --- pitches ---------------------------------------------------------------

def get_pitch(db, token):
    return db.execute("SELECT * FROM pitches WHERE token = ?", (token,)).fetchone()


def latest_pitch(db, group_id):
    return db.execute("SELECT * FROM pitches WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,)).fetchone()


def send_pitch(db, group_id, ask):
    group = svc.get_group(db, group_id)
    apt = svc.get_apartment(db, group["apartment_id"])
    token = secrets.token_urlsafe(12)
    db.execute(
        """INSERT INTO pitches (group_id, ask_offer_id, token, to_name, to_email, status, sent_at, respond_by)
           VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)""",
        (group_id, ask["id"], token, apt["contact_name"], apt["contact_email"], clock.ts(clock.now()),
         clock.later(days=PITCH_RESPONSE_DAYS)),
    )
    db.execute("UPDATE groups SET status = 'pitch_sent' WHERE id = ?", (group_id,))
    count = svc.member_count(db, group_id)
    svc.post_system(db, group_id, f"Target reached! We sent the group's pitch ({count} committed renters, "
                                  f"{ask['title']}) to {apt['contact_name']} at {apt['manager_company']}. "
                                  f"They have {PITCH_RESPONSE_DAYS} days to respond.")
    return token


def _claim_building(db, group, agent_id):
    db.execute("UPDATE apartments SET agent_user_id = ? WHERE id = ?", (agent_id, group["apartment_id"]))


def respond_to_pitch(db, token, agent_id, action, incentive_type=None, title=None, details=None, required_size=None):
    """The manager's answer: 'accept', 'counter', or 'decline'. Responding puts them on aptapt."""
    pitch = get_pitch(db, token)
    if pitch is None:
        raise svc.GroupError("That pitch link isn't valid.")
    if pitch["status"] != "sent":
        raise svc.GroupError("This pitch has already been answered.")
    group = svc.get_group(db, pitch["group_id"])
    now = clock.ts(clock.now())
    name = svc.user_name(db, agent_id)
    _claim_building(db, group, agent_id)

    if action == "accept":
        db.execute("UPDATE pitches SET status = 'accepted', responded_at = ? WHERE id = ?", (now, pitch["id"]))
        svc.post_system(db, group["id"], f"{name} accepted the group's ask and joined aptapt as the leasing agent.",
                        agent_id)
        return svc.unlock(db, group["id"], pitch["ask_offer_id"])

    if action == "decline":
        db.execute("UPDATE pitches SET status = 'declined', responded_at = ? WHERE id = ?", (now, pitch["id"]))
        _reopen_after_pitch(db, group["id"], pitch, f"{name} declined the group's ask. Set a new ask to keep going.")
        return None

    if action == "counter":
        title, size = svc._validate_terms(incentive_type, title, required_size)
        offer_id = db.execute(
            """INSERT INTO offers (group_id, author_id, source, incentive_type, title, details, required_size, status)
               VALUES (?, ?, 'agent', ?, ?, ?, ?, 'proposed')""",
            (group["id"], agent_id, incentive_type, title, (details or "").strip(), size),
        ).lastrowid
        db.execute(
            "UPDATE pitches SET status = 'countered', counter_offer_id = ?, responded_at = ?, vote_ends_at = ? WHERE id = ?",
            (offer_id, now, clock.later(hours=VOTE_HOURS), pitch["id"]),
        )
        svc.post_system(db, group["id"], f"{name} countered: {title} at {size} committed renters. "
                                         f"Vote within {VOTE_HOURS} hours. If you reject, you're released and refunded.",
                        agent_id)
        return None

    raise svc.GroupError("Unknown response.")


def _reopen_after_pitch(db, group_id, pitch, message):
    db.execute("UPDATE offers SET status = 'declined' WHERE id = ?", (pitch["ask_offer_id"],))
    db.execute("UPDATE groups SET status = 'forming' WHERE id = ?", (group_id,))
    svc.post_system(db, group_id, message)


def expire_pitch(db, pitch):
    db.execute("UPDATE pitches SET status = 'expired' WHERE id = ?", (pitch["id"],))
    _reopen_after_pitch(db, pitch["group_id"], pitch,
                        "The manager didn't respond in time. Adjust the group's ask and we'll pitch again.")


def vote(db, pitch_id, user_id, choice):
    pitch = db.execute("SELECT * FROM pitches WHERE id = ?", (pitch_id,)).fetchone()
    if pitch is None or pitch["status"] != "countered":
        raise svc.GroupError("There's no counter-offer to vote on.")
    if not svc.is_member(db, pitch["group_id"], user_id):
        raise svc.GroupError("Only committed members can vote.")
    if choice not in ("accept", "reject"):
        raise svc.GroupError("Vote accept or reject.")
    db.execute("INSERT OR REPLACE INTO votes (pitch_id, user_id, choice) VALUES (?, ?, ?)", (pitch_id, user_id, choice))
    remaining = db.execute(
        """SELECT COUNT(*) FROM memberships m WHERE m.group_id = ?
           AND NOT EXISTS (SELECT 1 FROM votes v WHERE v.pitch_id = ? AND v.user_id = m.user_id)""",
        (pitch["group_id"], pitch_id),
    ).fetchone()[0]
    if remaining == 0:
        return resolve_vote(db, pitch_id)
    return None


def vote_tally(db, pitch_id):
    rows = db.execute("SELECT choice, COUNT(*) AS n FROM votes WHERE pitch_id = ? GROUP BY choice", (pitch_id,))
    tally = {"accept": 0, "reject": 0}
    tally.update({r["choice"]: r["n"] for r in rows})
    return tally


def user_vote(db, pitch_id, user_id):
    row = db.execute("SELECT choice FROM votes WHERE pitch_id = ? AND user_id = ?", (pitch_id, user_id)).fetchone()
    return row["choice"] if row else None


def resolve_vote(db, pitch_id):
    """Rejecters and non-voters are released and refunded; the counter becomes the group's offer."""
    pitch = db.execute("SELECT * FROM pitches WHERE id = ?", (pitch_id,)).fetchone()
    group_id = pitch["group_id"]
    released = db.execute(
        """SELECT m.user_id FROM memberships m WHERE m.group_id = ? AND NOT EXISTS
           (SELECT 1 FROM votes v WHERE v.pitch_id = ? AND v.user_id = m.user_id AND v.choice = 'accept')""",
        (group_id, pitch_id),
    ).fetchall()
    for row in released:
        db.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, row["user_id"]))
        payments.resolve_commitment(db, group_id, row["user_id"], "refunded", "Rejected the manager's counter-offer")
    db.execute("UPDATE pitches SET status = 'resolved' WHERE id = ?", (pitch_id,))
    db.execute("UPDATE offers SET status = 'rejected' WHERE id = ?", (pitch["ask_offer_id"],))
    db.execute("UPDATE offers SET status = 'active' WHERE id = ?", (pitch["counter_offer_id"],))
    db.execute("UPDATE groups SET status = 'offer_active' WHERE id = ?", (group_id,))
    kept = svc.member_count(db, group_id)
    note = f" {len(released)} released with a refund." if released else ""
    svc.post_system(db, group_id, f"Vote closed: {kept} renters accepted the counter-offer.{note}")
    return svc.check_target(db, group_id)


# --- deals -----------------------------------------------------------------

def get_deal(db, group_id):
    return db.execute("SELECT * FROM deals WHERE group_id = ?", (group_id,)).fetchone()


def get_deal_by_id(db, deal_id):
    return db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()


def deal_member(db, deal_id, user_id):
    return db.execute("SELECT * FROM deal_members WHERE deal_id = ? AND user_id = ?", (deal_id, user_id)).fetchone()


def roster(db, deal_id):
    return db.execute(
        """SELECT dm.*, u.name, u.email, u.is_demo FROM deal_members dm JOIN users u ON u.id = dm.user_id
           WHERE dm.deal_id = ? ORDER BY CASE dm.status WHEN 'signed' THEN 0 WHEN 'reserved' THEN 1
           WHEN 'pending' THEN 2 ELSE 3 END, u.name""",
        (deal_id,),
    ).fetchall()


def deal_counts(db, deal_id):
    rows = db.execute("SELECT status, COUNT(*) AS n FROM deal_members WHERE deal_id = ? GROUP BY status", (deal_id,))
    counts = {"pending": 0, "reserved": 0, "signed": 0, "dropped": 0}
    counts.update({r["status"]: r["n"] for r in rows})
    counts["active"] = counts["pending"] + counts["reserved"] + counts["signed"]
    return counts


def open_deal(db, group_id, offer_id):
    deal_id = db.execute(
        "INSERT INTO deals (group_id, offer_id, status, window_ends_at) VALUES (?, ?, 'reserving', ?)",
        (group_id, offer_id, clock.later(hours=RESERVE_WINDOW_HOURS)),
    ).lastrowid
    for member in svc.members(db, group_id):
        add_member(db, deal_id, member["id"])
    return deal_id


def add_member(db, deal_id, user_id):
    db.execute("INSERT OR IGNORE INTO deal_members (deal_id, user_id, status) VALUES (?, ?, 'pending')",
               (deal_id, user_id))


def mark_reserved(db, deal_id, user_id, floor_plan, move_in):
    deal = get_deal_by_id(db, deal_id)
    db.execute(
        """UPDATE deal_members SET status = 'reserved', floor_plan = ?, move_in = ?, reserved_at = ?
           WHERE deal_id = ? AND user_id = ? AND status = 'pending'""",
        (floor_plan, move_in, clock.ts(clock.now()), deal_id, user_id),
    )
    svc.post_system(db, deal["group_id"], f"{svc.user_name(db, user_id)} reserved a {floor_plan}.", user_id)


def sign(db, deal_id, user_id, typed_name):
    member = deal_member(db, deal_id, user_id)
    if member is None or member["status"] != "reserved":
        raise svc.GroupError("Reserve your unit before signing.")
    expected = svc.user_name(db, user_id)
    if (typed_name or "").strip().lower() != expected.strip().lower():
        raise svc.GroupError(f"Type your full name exactly as “{expected}” to sign.")
    deal = get_deal_by_id(db, deal_id)
    db.execute(
        "UPDATE deal_members SET status = 'signed', signed_at = ?, signature = ? WHERE deal_id = ? AND user_id = ?",
        (clock.ts(clock.now()), typed_name.strip(), deal_id, user_id),
    )
    payments.resolve_commitment(db, deal["group_id"], user_id, "refunded", "Signed your lease. Deal went through!")
    hold = payments.held_deposit(db, deal["group_id"], user_id, "holding")
    if hold:
        payments.resolve(db, hold["id"], "applied", "Applied to your first month's rent")
    svc.post_system(db, deal["group_id"], f"{expected} signed their lease.", user_id)
    check_close(db, deal_id)


def sign_all_demo(db, deal_id):
    """Demo: every generated renter reserves and signs."""
    deal = get_deal_by_id(db, deal_id)
    apt = svc.get_apartment(db, svc.get_group(db, deal["group_id"])["apartment_id"])
    import json
    plan = (json.loads(apt["floor_plans"]) or ["1BR"])[0]
    for m in roster(db, deal_id):
        if not m["is_demo"] or m["status"] == "dropped":
            continue
        if m["status"] == "pending":
            db.execute(
                """INSERT INTO deposits (user_id, group_id, kind, amount_cents, status, card_brand, card_last4)
                   VALUES (?, ?, 'holding', ?, 'held', 'Visa', '4242')""",
                (m["user_id"], deal["group_id"], payments.HOLDING_CENTS),
            )
            mark_reserved(db, deal_id, m["user_id"], plan, None)
        sign(db, deal_id, m["user_id"], m["name"])


def drop_member(db, deal_id, user_id, reason):
    deal = get_deal_by_id(db, deal_id)
    member = deal_member(db, deal_id, user_id)
    if member is None or member["status"] in ("signed", "dropped"):
        raise svc.GroupError("You can't back out at this point.")
    db.execute("UPDATE deal_members SET status = 'dropped' WHERE deal_id = ? AND user_id = ?", (deal_id, user_id))
    payments.resolve_commitment(db, deal["group_id"], user_id, "forfeited", reason)
    hold = payments.held_deposit(db, deal["group_id"], user_id, "holding")
    if hold:
        payments.resolve(db, hold["id"], "refunded", "Holding deposit returned after backing out")
    svc.post_system(db, deal["group_id"], f"{svc.user_name(db, user_id)} backed out of the deal.", user_id)
    if deal["window_closed"]:
        _check_short(db, deal_id)
    check_close(db, deal_id)


def check_close(db, deal_id):
    deal = get_deal_by_id(db, deal_id)
    if deal["status"] != "reserving":
        return
    counts = deal_counts(db, deal_id)
    if counts["pending"] or counts["reserved"] or not counts["signed"]:
        return
    offer = db.execute("SELECT required_size FROM offers WHERE id = ?", (deal["offer_id"],)).fetchone()
    if counts["signed"] < offer["required_size"] and not deal["honored"]:
        _check_short(db, deal_id)  # everyone's done but people dropped: the agent decides
        return
    db.execute("UPDATE deals SET status = 'closed', closed_at = ? WHERE id = ?", (clock.ts(clock.now()), deal_id))
    svc.post_system(db, deal["group_id"], f"Deal closed: {counts['signed']} leases signed. Welcome home!")


def _check_short(db, deal_id):
    deal = get_deal_by_id(db, deal_id)
    offer = db.execute("SELECT * FROM offers WHERE id = ?", (deal["offer_id"],)).fetchone()
    counts = deal_counts(db, deal_id)
    if deal["status"] == "reserving" and not deal["honored"] and counts["active"] < offer["required_size"]:
        db.execute("UPDATE deals SET status = 'short' WHERE id = ?", (deal_id,))
        svc.post_system(db, deal["group_id"],
                        f"The group is short: {counts['active']} of {offer['required_size']} renters are still in. "
                        f"The leasing agent will decide whether to honor, extend, or cancel.")


def end_window(db, deal_id):
    deal = get_deal_by_id(db, deal_id)
    if deal["window_closed"] or deal["status"] not in ("reserving",):
        return
    db.execute("UPDATE deals SET window_closed = 1 WHERE id = ?", (deal_id,))
    for m in roster(db, deal_id):
        if m["status"] == "pending":
            db.execute("UPDATE deal_members SET status = 'dropped' WHERE deal_id = ? AND user_id = ?",
                       (deal_id, m["user_id"]))
            payments.resolve_commitment(db, deal["group_id"], m["user_id"], "forfeited",
                                        "Didn't reserve before the window closed")
            svc.post_system(db, deal["group_id"], f"{m['name']} didn't reserve in time and was dropped.")
    _check_short(db, deal_id)
    check_close(db, deal_id)


def resolve_short(db, deal_id, agent_id, action):
    deal = get_deal_by_id(db, deal_id)
    if not svc.agent_manages(db, deal["group_id"], agent_id):
        raise svc.GroupError("You aren't the leasing agent for this apartment.")
    if deal["status"] != "short":
        raise svc.GroupError("This deal doesn't need a decision.")
    gid = deal["group_id"]
    if action == "honor":
        db.execute("UPDATE deals SET status = 'reserving', honored = 1 WHERE id = ?", (deal_id,))
        svc.post_system(db, gid, "Good news: the leasing agent is honoring the deal with the renters still in.", agent_id)
        check_close(db, deal_id)
    elif action == "extend":
        db.execute(
            "UPDATE deals SET status = 'reserving', extended = 1, window_closed = 0, window_ends_at = ? WHERE id = ?",
            (clock.later(hours=EXTENSION_HOURS), deal_id),
        )
        svc.post_system(db, gid, f"The leasing agent extended the deal {EXTENSION_HOURS} hours. "
                                 f"New renters can commit to fill the open spots. Invite friends!", agent_id)
    elif action == "cancel":
        db.execute("UPDATE deals SET status = 'cancelled', closed_at = ? WHERE id = ?", (clock.ts(clock.now()), deal_id))
        for dep in db.execute("SELECT id FROM deposits WHERE group_id = ? AND status = 'held'", (gid,)).fetchall():
            payments.resolve(db, dep["id"], "refunded", "Deal cancelled by the leasing agent")
        svc.post_system(db, gid, "The leasing agent cancelled the deal. All deposits were refunded.", agent_id)
    else:
        raise svc.GroupError("Unknown decision.")


def nudge(db, deal_id, agent_id, user_id):
    deal = get_deal_by_id(db, deal_id)
    if not svc.agent_manages(db, deal["group_id"], agent_id):
        raise svc.GroupError("You aren't the leasing agent for this apartment.")
    member = deal_member(db, deal_id, user_id)
    if member is None or member["status"] not in ("pending", "reserved"):
        raise svc.GroupError("Nothing to nudge.")
    step = "reserve your unit" if member["status"] == "pending" else "sign your lease"
    svc.post_system(db, deal["group_id"], f"Reminder for {svc.user_name(db, user_id)}: please {step}.", agent_id)


# --- deadlines -------------------------------------------------------------

def run_deadlines(db):
    """Advance anything whose deadline passed. Cheap enough to call on every request."""
    changed = False
    now = clock.ts(clock.now())
    for g in db.execute(
        """SELECT id FROM groups WHERE kind = 'committed' AND status IN ('forming', 'agent_invited', 'offer_active')
           AND due_at IS NOT NULL AND due_at <= ?""", (now,)
    ).fetchall():
        svc.expire_group(db, g["id"])
        changed = True
    for p in db.execute("SELECT * FROM pitches WHERE status = 'sent' AND respond_by <= ?", (now,)).fetchall():
        expire_pitch(db, p)
        changed = True
    for p in db.execute("SELECT id FROM pitches WHERE status = 'countered' AND vote_ends_at <= ?", (now,)).fetchall():
        resolve_vote(db, p["id"])
        changed = True
    for d in db.execute(
        "SELECT id FROM deals WHERE status = 'reserving' AND window_closed = 0 AND window_ends_at <= ?", (now,)
    ).fetchall():
        end_window(db, d["id"])
        changed = True
    return changed
