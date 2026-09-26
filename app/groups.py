"""Group lifecycle rules. Pure functions over a sqlite3 connection; callers commit.

Committed groups (see docs/v2-design.md):
  offer path (manager on aptapt):  forming -> agent_invited -> offer_active -> unlocked
  pitch path (manager not on aptapt): forming (with an ask) -> pitch_sent -> unlocked | offer_active | forming
A committed group that passes its due date without unlocking expires and refunds deposits.
"""

import secrets

from . import clock

INCENTIVE_TYPES = {
    "rent_discount": "Reduced monthly rent",
    "waived_fees": "Waived fees",
    "free_parking": "Free parking",
    "free_month": "Free month(s)",
    "other": "Other perk",
}
OPEN_COMMITTED = ("forming", "agent_invited", "offer_active", "pitch_sent")
COMMIT_WINDOW_DAYS = 30
COMMITMENT_CENTS = 1500
MIN_REQUIRED_SIZE = 2
MAX_REQUIRED_SIZE = 200
MAX_GROUP_NAME = 60
MAX_MESSAGE = 1000


class GroupError(Exception):
    """A rule violation that should be shown to the user."""


# --- reads -----------------------------------------------------------------

def get_group(db, group_id):
    row = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise GroupError("That group doesn't exist.")
    return row


def get_apartment(db, apartment_id):
    return db.execute("SELECT * FROM apartments WHERE id = ?", (apartment_id,)).fetchone()


def member_count(db, group_id):
    return db.execute("SELECT COUNT(*) FROM memberships WHERE group_id = ?", (group_id,)).fetchone()[0]


def members(db, group_id):
    return db.execute(
        """SELECT u.* FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.group_id = ? ORDER BY m.joined_at, u.id""",
        (group_id,),
    ).fetchall()


def is_member(db, group_id, user_id):
    return db.execute(
        "SELECT 1 FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, user_id)
    ).fetchone() is not None


def active_offer(db, group_id):
    """The agent offer currently on the table."""
    return db.execute(
        """SELECT * FROM offers WHERE group_id = ? AND source = 'agent' AND status = 'active'
           ORDER BY id DESC LIMIT 1""",
        (group_id,),
    ).fetchone()


def group_ask(db, group_id):
    """The group's own ask (pitch path), while it's live."""
    return db.execute(
        """SELECT * FROM offers WHERE group_id = ? AND source = 'group' AND status = 'active'
           ORDER BY id DESC LIMIT 1""",
        (group_id,),
    ).fetchone()


def current_offer(db, group_id):
    """What to show as "the deal": an accepted offer, the active agent offer, or the group's ask."""
    return db.execute(
        """SELECT * FROM offers WHERE group_id = ? AND status IN ('accepted', 'active')
           ORDER BY status = 'accepted' DESC, source = 'agent' DESC, id DESC LIMIT 1""",
        (group_id,),
    ).fetchone()


def open_committed_group(db, apartment_id):
    placeholders = ",".join("?" * len(OPEN_COMMITTED))
    return db.execute(
        f"""SELECT * FROM groups WHERE apartment_id = ? AND kind = 'committed'
            AND status IN ({placeholders}) ORDER BY seq DESC LIMIT 1""",
        (apartment_id, *OPEN_COMMITTED),
    ).fetchone()


def manager_on_aptapt(db, apartment_id):
    return get_apartment(db, apartment_id)["agent_user_id"] is not None


def agent_manages(db, group_id, agent_id):
    return db.execute(
        """SELECT 1 FROM groups g JOIN apartments a ON a.id = g.apartment_id
           WHERE g.id = ? AND a.agent_user_id = ?""",
        (group_id, agent_id),
    ).fetchone() is not None


def feed(db, group_id, limit=100):
    """Most recent messages, newest first."""
    return db.execute(
        """SELECT m.*, u.name AS user_name, u.role AS user_role FROM messages m
           LEFT JOIN users u ON u.id = m.user_id
           WHERE m.group_id = ? ORDER BY m.id DESC LIMIT ?""",
        (group_id, limit),
    ).fetchall()


def target_size(db, group_id):
    offer = current_offer(db, group_id)
    return offer["required_size"] if offer else None


def apartment_stats(db, apartment_id):
    committed = open_committed_group(db, apartment_id)
    offer = current_offer(db, committed["id"]) if committed else None
    committed_count = member_count(db, committed["id"]) if committed else 0
    interested = db.execute(
        """SELECT COUNT(DISTINCT m.user_id) FROM memberships m JOIN shortlist s ON s.group_id = m.group_id
           WHERE s.apartment_id = ?""",
        (apartment_id,),
    ).fetchone()[0]
    return {
        "committed_group": committed,
        "committed_count": committed_count,
        "offer": offer,
        "needed": max(0, offer["required_size"] - committed_count) if offer else None,
        "interested": interested,
        "exploratory_groups": db.execute(
            "SELECT COUNT(*) FROM shortlist WHERE apartment_id = ?", (apartment_id,)
        ).fetchone()[0],
        "unlocked_groups": db.execute(
            "SELECT COUNT(*) FROM groups WHERE apartment_id = ? AND kind = 'committed' AND status = 'unlocked'",
            (apartment_id,),
        ).fetchone()[0],
    }


# --- messages --------------------------------------------------------------

def user_name(db, user_id):
    row = db.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["name"] if row else "Someone"


def post_system(db, group_id, body, user_id=None):
    db.execute(
        "INSERT INTO messages (group_id, user_id, kind, body) VALUES (?, ?, 'system', ?)",
        (group_id, user_id, body),
    )


def post_chat(db, group_id, user_id, body):
    body = (body or "").strip()
    if not body:
        raise GroupError("Message can't be empty.")
    if len(body) > MAX_MESSAGE:
        raise GroupError(f"Messages are limited to {MAX_MESSAGE} characters.")
    db.execute(
        "INSERT INTO messages (group_id, user_id, kind, body) VALUES (?, ?, 'chat', ?)",
        (group_id, user_id, body),
    )


# --- exploratory groups (free, not tied to a building) ---------------------

def create_exploratory(db, user_id, name, apartment_id=None):
    """Start a free group. Optionally seed its shortlist with the building it was started from."""
    name = (name or "").strip()[:MAX_GROUP_NAME] or "House hunting"
    cur = db.execute(
        "INSERT INTO groups (apartment_id, kind, name, status, created_by) VALUES (NULL, 'exploratory', ?, 'open', ?)",
        (name, user_id),
    )
    group_id = cur.lastrowid
    post_system(db, group_id, f"{user_name(db, user_id)} started this group.", user_id)
    db.execute("INSERT INTO memberships (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
    if apartment_id:
        add_to_shortlist(db, group_id, apartment_id, user_id, announce=False)
    return group_id


def add_to_shortlist(db, group_id, apartment_id, user_id, announce=True):
    group = get_group(db, group_id)
    if group["kind"] != "exploratory":
        raise GroupError("Only groups can keep a shortlist.")
    if not is_member(db, group_id, user_id):
        raise GroupError("Join the group to change its shortlist.")
    apt = get_apartment(db, apartment_id)
    if apt is None:
        raise GroupError("That building doesn't exist.")
    added = db.execute("INSERT OR IGNORE INTO shortlist (group_id, apartment_id, added_by) VALUES (?, ?, ?)",
                       (group_id, apartment_id, user_id)).rowcount
    if added and announce:
        post_system(db, group_id, f"{user_name(db, user_id)} added {apt['name']} to the shortlist.", user_id)


def remove_from_shortlist(db, group_id, apartment_id, user_id):
    if not is_member(db, group_id, user_id):
        raise GroupError("Join the group to change its shortlist.")
    removed = db.execute("DELETE FROM shortlist WHERE group_id = ? AND apartment_id = ?",
                         (group_id, apartment_id)).rowcount
    if removed:
        post_system(db, group_id, f"{user_name(db, user_id)} removed {get_apartment(db, apartment_id)['name']} "
                                  f"from the shortlist.", user_id)


def shortlist(db, group_id):
    """Shortlisted buildings, each with its open committed group and which of our members committed there."""
    member_ids = {m["id"] for m in members(db, group_id)}
    items = []
    for apt in db.execute(
        """SELECT a.* FROM shortlist s JOIN apartments a ON a.id = s.apartment_id
           WHERE s.group_id = ? ORDER BY s.created_at, a.name""", (group_id,)
    ).fetchall():
        stats = apartment_stats(db, apt["id"])
        committed_here = []
        if stats["committed_group"]:
            committed_here = [m for m in members(db, stats["committed_group"]["id"]) if m["id"] in member_ids]
        items.append({"apt": apt, "stats": stats, "ours": committed_here})
    return items


def groups_considering(db, apartment_id):
    return db.execute(
        """SELECT g.*, (SELECT COUNT(*) FROM memberships m WHERE m.group_id = g.id) AS size
           FROM shortlist s JOIN groups g ON g.id = s.group_id WHERE s.apartment_id = ? ORDER BY size DESC""",
        (apartment_id,),
    ).fetchall()


def user_groups(db, user_id):
    return db.execute(
        """SELECT g.* FROM memberships m JOIN groups g ON g.id = m.group_id
           WHERE m.user_id = ? AND g.kind = 'exploratory' ORDER BY g.name""", (user_id,)
    ).fetchall()


def join_exploratory(db, group_id, user_id):
    group = get_group(db, group_id)
    if group["kind"] != "exploratory":
        raise GroupError("Committed groups require a $15 commitment deposit to join.")
    if is_member(db, group_id, user_id):
        return
    db.execute("INSERT INTO memberships (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
    post_system(db, group_id, f"{user_name(db, user_id)} joined.", user_id)


def leave_exploratory(db, group_id, user_id):
    group = get_group(db, group_id)
    if group["kind"] != "exploratory":
        raise GroupError("Use the committed-group leave flow.")
    if not is_member(db, group_id, user_id):
        return
    db.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    post_system(db, group_id, f"{user_name(db, user_id)} left.", user_id)


# --- committed groups ------------------------------------------------------

def get_or_create_open_committed(db, apartment_id):
    existing = open_committed_group(db, apartment_id)
    if existing:
        return existing
    seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM groups WHERE apartment_id = ? AND kind = 'committed'",
        (apartment_id,),
    ).fetchone()[0]
    cur = db.execute(
        """INSERT INTO groups (apartment_id, kind, name, seq, status, due_at)
           VALUES (?, 'committed', ?, ?, 'forming', ?)""",
        (apartment_id, f"Committed group #{seq}", seq, clock.later(days=COMMIT_WINDOW_DAYS)),
    )
    return get_group(db, cur.lastrowid)


def can_commit(db, group_id, user_id):
    """Raise unless user can put down a commitment deposit for this group right now."""
    group = get_group(db, group_id)
    if group["kind"] != "committed":
        raise GroupError("Only committed groups take commitment deposits.")
    if is_member(db, group_id, user_id):
        raise GroupError("You're already committed to this group.")
    if group["status"] == "unlocked":
        from . import deals
        deal = deals.get_deal(db, group_id)
        if not (deal and deal["status"] == "reserving" and deal["extended"]):
            raise GroupError("This group already unlocked its deal and is closed.")
    elif group["status"] not in OPEN_COMMITTED:
        raise GroupError("This group is no longer accepting commitments.")
    return group


def add_committed_member(db, group_id, user_id):
    """Called once the commitment deposit succeeds. Returns the next group if this unlocked a deal."""
    group = can_commit(db, group_id, user_id)
    db.execute("INSERT INTO memberships (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
    post_system(db, group_id, f"{user_name(db, user_id)} committed.", user_id)
    if group["status"] == "unlocked":  # joining during a deal extension
        from . import deals
        deals.add_member(db, deals.get_deal(db, group_id)["id"], user_id)
        return None
    return check_target(db, group_id)


def leave_committed(db, group_id, user_id):
    """Leaving always forfeits the $15 commitment deposit."""
    from . import deals, payments
    group = get_group(db, group_id)
    if not is_member(db, group_id, user_id):
        return
    if group["status"] == "unlocked":
        deals.drop_member(db, deals.get_deal(db, group_id)["id"], user_id, reason="Backed out after the deal unlocked")
        return
    db.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    db.execute("DELETE FROM votes WHERE user_id = ? AND pitch_id IN (SELECT id FROM pitches WHERE group_id = ?)",
               (user_id, group_id))
    payments.resolve_commitment(db, group_id, user_id, "forfeited", "Left the group before it unlocked")
    post_system(db, group_id, f"{user_name(db, user_id)} left and forfeited their deposit.", user_id)


def expire_group(db, group_id):
    from . import payments
    group = get_group(db, group_id)
    if group["status"] not in OPEN_COMMITTED:
        return
    db.execute("UPDATE groups SET status = 'expired', closed_at = ? WHERE id = ?", (clock.ts(clock.now()), group_id))
    db.execute("UPDATE offers SET status = 'withdrawn' WHERE group_id = ? AND status IN ('active', 'proposed')",
               (group_id,))
    for member in members(db, group_id):
        payments.resolve_commitment(db, group_id, member["id"], "refunded", "Group missed its due date")
    post_system(db, group_id, "The due date passed without unlocking a deal. Everyone's $15 deposit was refunded.")


# --- invites ---------------------------------------------------------------

def get_or_create_invite(db, group_id, user_id, kind):
    row = db.execute(
        "SELECT token FROM invites WHERE group_id = ? AND created_by = ? AND kind = ?", (group_id, user_id, kind)
    ).fetchone()
    if row:
        return row["token"]
    token = secrets.token_urlsafe(9)
    db.execute("INSERT INTO invites (token, group_id, kind, created_by) VALUES (?, ?, ?, ?)",
               (token, group_id, kind, user_id))
    return token


def agent_invite_token(db, group_id):
    row = db.execute(
        "SELECT token FROM invites WHERE group_id = ? AND kind = 'agent' ORDER BY created_at LIMIT 1", (group_id,)
    ).fetchone()
    return row["token"] if row else None


def find_invite(db, token):
    return db.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()


# --- offer path (manager on aptapt) ---------------------------------------

def invite_agent(db, group_id, user_id, threshold):
    group = get_group(db, group_id)
    if group["kind"] != "committed" or group["status"] not in OPEN_COMMITTED:
        raise GroupError("This group can't invite the leasing agent right now.")
    if not is_member(db, group_id, user_id):
        raise GroupError("Only group members can invite the leasing agent.")
    if not manager_on_aptapt(db, group["apartment_id"]):
        raise GroupError("This building's manager isn't on aptapt yet. Set a group ask instead and we'll pitch it to them.")
    existing = agent_invite_token(db, group_id)
    if group["status"] != "forming" and existing:
        return existing
    count = member_count(db, group_id)
    if count < threshold:
        raise GroupError(f"You need at least {threshold} committed renters to invite the leasing agent ({count} so far).")
    if group["status"] == "forming":
        db.execute("UPDATE groups SET status = 'agent_invited' WHERE id = ?", (group_id,))
    post_system(db, group_id, f"{user_name(db, user_id)} invited the leasing agent.", user_id)
    return existing or get_or_create_invite(db, group_id, user_id, "agent")


def claim_agent_invite(db, token, agent_id):
    invite = find_invite(db, token)
    if invite is None or invite["kind"] != "agent":
        raise GroupError("That agent invite link isn't valid.")
    group = get_group(db, invite["group_id"])
    db.execute("UPDATE apartments SET agent_user_id = ? WHERE id = ?", (agent_id, group["apartment_id"]))
    return group["id"]


def _validate_terms(incentive_type, title, required_size):
    if incentive_type not in INCENTIVE_TYPES:
        raise GroupError("Pick an incentive type.")
    title = (title or "").strip()
    if not title:
        raise GroupError("Give it a short headline, e.g. \"$150 off monthly rent\".")
    try:
        required_size = int(required_size)
    except (TypeError, ValueError):
        raise GroupError("Group size must be a number.") from None
    if not MIN_REQUIRED_SIZE <= required_size <= MAX_REQUIRED_SIZE:
        raise GroupError(f"Group size must be between {MIN_REQUIRED_SIZE} and {MAX_REQUIRED_SIZE}.")
    return title[:80], required_size


def post_offer(db, group_id, agent_id, incentive_type, title, details, required_size):
    group = get_group(db, group_id)
    if group["kind"] != "committed" or group["status"] not in OPEN_COMMITTED:
        raise GroupError("This group isn't taking offers right now.")
    if not agent_manages(db, group_id, agent_id):
        raise GroupError("You aren't the leasing agent for this apartment.")
    if group["status"] == "pitch_sent":
        raise GroupError("Respond to the group's pitch instead.")
    title, required_size = _validate_terms(incentive_type, title, required_size)
    replaced = db.execute(
        "UPDATE offers SET status = 'superseded' WHERE group_id = ? AND status = 'active'", (group_id,)
    ).rowcount
    db.execute(
        """INSERT INTO offers (group_id, author_id, source, incentive_type, title, details, required_size, status)
           VALUES (?, ?, 'agent', ?, ?, ?, ?, 'active')""",
        (group_id, agent_id, incentive_type, title, (details or "").strip(), required_size),
    )
    db.execute("UPDATE groups SET status = 'offer_active' WHERE id = ?", (group_id,))
    verb = "updated the offer" if replaced else "made an offer"
    post_system(db, group_id, f"Leasing agent {verb}: {title}. Unlocks at {required_size} committed renters.", agent_id)
    return check_target(db, group_id)


def withdraw_offer(db, group_id, agent_id):
    if not agent_manages(db, group_id, agent_id):
        raise GroupError("You aren't the leasing agent for this apartment.")
    if get_group(db, group_id)["status"] != "offer_active":
        raise GroupError("There's no active offer to withdraw.")
    db.execute("UPDATE offers SET status = 'withdrawn' WHERE group_id = ? AND status = 'active'", (group_id,))
    db.execute("UPDATE groups SET status = 'agent_invited' WHERE id = ?", (group_id,))
    post_system(db, group_id, "Leasing agent withdrew the offer.", agent_id)


# --- pitch path (manager not on aptapt) ------------------------------------

def set_ask(db, group_id, user_id, incentive_type, title, details, required_size):
    group = get_group(db, group_id)
    if group["kind"] != "committed" or group["status"] != "forming":
        raise GroupError("The group's ask can only be changed while the group is forming.")
    if not is_member(db, group_id, user_id):
        raise GroupError("Only committed members can set the group's ask.")
    if manager_on_aptapt(db, group["apartment_id"]):
        raise GroupError("This building's manager is on aptapt. Invite them to make an offer instead.")
    title, required_size = _validate_terms(incentive_type, title, required_size)
    replaced = db.execute(
        "UPDATE offers SET status = 'superseded' WHERE group_id = ? AND source = 'group' AND status = 'active'",
        (group_id,),
    ).rowcount
    db.execute(
        """INSERT INTO offers (group_id, author_id, source, incentive_type, title, details, required_size, status)
           VALUES (?, ?, 'group', ?, ?, ?, ?, 'active')""",
        (group_id, user_id, incentive_type, title, (details or "").strip(), required_size),
    )
    verb = "changed the group's ask to" if replaced else "set the group's ask:"
    post_system(db, group_id, f"{user_name(db, user_id)} {verb} {title}. We pitch it at {required_size} committed renters.",
                user_id)
    return check_target(db, group_id)


# --- targets & unlocking ---------------------------------------------------

def check_target(db, group_id):
    """Advance the group if it hit its target. Returns the next committed group if a deal unlocked."""
    from . import deals
    group = get_group(db, group_id)
    count = member_count(db, group_id)
    if group["status"] == "offer_active":
        offer = active_offer(db, group_id)
        if offer and count >= offer["required_size"]:
            return unlock(db, group_id, offer["id"])
    elif group["status"] == "forming":
        ask = group_ask(db, group_id)
        if ask and count >= ask["required_size"] and not manager_on_aptapt(db, group["apartment_id"]):
            deals.send_pitch(db, group_id, ask)
    return None


def unlock(db, group_id, offer_id):
    from . import deals
    group = get_group(db, group_id)
    offer = db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    db.execute("UPDATE offers SET status = 'accepted' WHERE id = ?", (offer_id,))
    db.execute("UPDATE groups SET status = 'unlocked', closed_at = ? WHERE id = ?", (clock.ts(clock.now()), group_id))
    count = member_count(db, group_id)
    post_system(db, group_id, f"Deal unlocked! {count} renters secured: {offer['title']}. "
                              f"Reserve your unit within {deals.RESERVE_WINDOW_HOURS} hours.")
    deals.open_deal(db, group_id, offer_id)
    nxt = get_or_create_open_committed(db, group["apartment_id"])
    post_system(db, nxt["id"], f"{nxt['name']} is now forming. The previous group unlocked its deal.")
    return nxt


# --- demo helper -----------------------------------------------------------

FAKE_FIRST = ["Avery", "Riley", "Quinn", "Jamie", "Casey", "Drew", "Skyler", "Rowan", "Emerson", "Hayden",
              "Parker", "Reese", "Logan", "Sasha", "Blake", "Cameron", "Dakota", "Finley", "Kendall", "Marley"]
FAKE_LAST = "ABCDEFGHJKLMNPRSTVW"


def make_fake_renter(db):
    name = f"{secrets.choice(FAKE_FIRST)} {secrets.choice(FAKE_LAST)}."
    return db.execute(
        "INSERT INTO users (name, email, role, is_demo) VALUES (?, ?, 'renter', 1)",
        (name, f"demo-{secrets.token_hex(6)}@aptapt.test"),
    ).lastrowid


def add_fake_members(db, group_id, n):
    """Demo: n throwaway renters join (exploratory) or pay the test deposit and commit. Stops when the group moves on."""
    from . import payments
    group = get_group(db, group_id)
    added = 0
    for _ in range(n):
        group = get_group(db, group_id)
        if group["kind"] == "exploratory":
            join_exploratory(db, group_id, make_fake_renter(db))
        elif group["status"] in ("forming", "agent_invited", "offer_active"):
            payments.record_commitment(db, group_id, make_fake_renter(db), "Visa", "4242")
        else:
            break
        added += 1
    return added
