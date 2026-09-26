import pytest

from app import clock, deals, payments
from app import groups as svc

from .conftest import make_apartment, make_user


@pytest.fixture
def world(db):
    agent = make_user(db, "Agent A", role="agent")
    apt = make_apartment(db, agent_id=agent)
    offplat = make_apartment(db, agent_id=None, slug="off-platform")
    manager = make_user(db, "Pat Contact", role="agent")
    renters = [make_user(db, f"Renter {i}") for i in range(12)]
    return {"agent": agent, "apt": apt, "offplat": offplat, "manager": manager, "renters": renters}


def commit(db, apartment_id, user_id):
    group = svc.get_or_create_open_committed(db, apartment_id)
    payments.record_commitment(db, group["id"], user_id, "Visa", "4242")
    return group["id"]


def fill(db, apartment_id, renters):
    gid = None
    for r in renters:
        gid = commit(db, apartment_id, r)
    return gid


def deposit_status(db, group_id, user_id, kind="commitment"):
    row = db.execute("SELECT status FROM deposits WHERE group_id = ? AND user_id = ? AND kind = ? ORDER BY id DESC",
                     (group_id, user_id, kind)).fetchone()
    return row["status"] if row else None


# --- committing & deposits -------------------------------------------------

def test_commit_holds_deposit_and_new_group_has_due_date(db, world):
    r = world["renters"]
    gid = commit(db, world["apt"], r[0])
    assert deposit_status(db, gid, r[0]) == "held"
    group = svc.get_group(db, gid)
    assert group["status"] == "forming" and clock.parse(group["due_at"]) > clock.now()
    with pytest.raises(svc.GroupError, match="already committed"):
        payments.record_commitment(db, gid, r[0], "Visa", "4242")


def test_leaving_committed_group_always_forfeits(db, world):
    r = world["renters"]
    gid = fill(db, world["apt"], r[:2])
    svc.leave_committed(db, gid, r[0])
    assert not svc.is_member(db, gid, r[0])
    assert deposit_status(db, gid, r[0]) == "forfeited"


def test_exploratory_groups_are_free(db, world):
    r = world["renters"]
    gid = svc.create_exploratory(db, r[0], "Grad students", world["apt"])
    assert svc.get_group(db, gid)["apartment_id"] is None
    assert [i["apt"]["id"] for i in svc.shortlist(db, gid)] == [world["apt"]]
    svc.join_exploratory(db, gid, r[1])
    assert svc.member_count(db, gid) == 2
    assert db.execute("SELECT COUNT(*) FROM deposits").fetchone()[0] == 0
    with pytest.raises(svc.GroupError, match="deposit"):
        svc.join_exploratory(db, svc.get_or_create_open_committed(db, world["apt"])["id"], r[2])


def test_due_date_expiry_refunds_everyone(db, world):
    r = world["renters"]
    gid = fill(db, world["apt"], r[:3])
    db.execute("UPDATE groups SET due_at = ? WHERE id = ?", ("2000-01-01 00:00:00", gid))
    assert deals.run_deadlines(db)
    assert svc.get_group(db, gid)["status"] == "expired"
    assert {deposit_status(db, gid, u) for u in r[:3]} == {"refunded"}
    assert commit(db, world["apt"], r[4]) != gid  # a fresh group forms


# --- offer path ------------------------------------------------------------

def test_invite_agent_threshold_and_offer_unlock_rolls_over(db, world):
    r = world["renters"]
    gid = fill(db, world["apt"], r[:2])
    with pytest.raises(svc.GroupError, match="at least 3"):
        svc.invite_agent(db, gid, r[0], threshold=3)
    commit(db, world["apt"], r[2])
    svc.invite_agent(db, gid, r[0], threshold=3)
    svc.post_offer(db, gid, world["agent"], "rent_discount", "$100 off", "", 5)
    fill(db, world["apt"], r[3:5])
    assert svc.get_group(db, gid)["status"] == "unlocked"
    assert deals.get_deal(db, gid)["status"] == "reserving"
    nxt = svc.open_committed_group(db, world["apt"])
    assert nxt["seq"] == 2 and nxt["id"] != gid


def test_only_the_buildings_agent_can_offer(db, world):
    gid = fill(db, world["apt"], world["renters"][:3])
    stranger = make_user(db, "Other Agent", role="agent")
    with pytest.raises(svc.GroupError, match="aren't the leasing agent"):
        svc.post_offer(db, gid, stranger, "rent_discount", "$100 off", "", 5)


def test_offer_validation(db, world):
    gid = fill(db, world["apt"], world["renters"][:3])
    for kwargs, msg in [({"incentive_type": "bogus"}, "incentive type"), ({"title": " "}, "headline"),
                        ({"required_size": "x"}, "number"), ({"required_size": 1}, "between")]:
        args = {"incentive_type": "free_parking", "title": "Free parking", "details": "", "required_size": 5, **kwargs}
        with pytest.raises(svc.GroupError, match=msg):
            svc.post_offer(db, gid, world["agent"], **args)


def test_ask_is_only_for_off_platform_managers(db, world):
    gid = fill(db, world["apt"], world["renters"][:1])
    with pytest.raises(svc.GroupError, match="on aptapt"):
        svc.set_ask(db, gid, world["renters"][0], "waived_fees", "No fees", "", 4)
    off = fill(db, world["offplat"], world["renters"][1:2])
    with pytest.raises(svc.GroupError, match="isn't on aptapt"):
        svc.invite_agent(db, off, world["renters"][1], threshold=1)


# --- pitch path ------------------------------------------------------------

def pitched_group(db, world, size=4):
    r = world["renters"]
    gid = commit(db, world["offplat"], r[0])
    svc.set_ask(db, gid, r[0], "waived_fees", "No fees", "", size)
    fill(db, world["offplat"], r[1:size])
    return gid


def test_reaching_ask_sends_pitch(db, world):
    gid = pitched_group(db, world)
    assert svc.get_group(db, gid)["status"] == "pitch_sent"
    pitch = deals.latest_pitch(db, gid)
    assert pitch["status"] == "sent" and pitch["to_email"] == "pat@t.test"


def test_pitch_accept_puts_manager_on_platform_and_unlocks(db, world):
    gid = pitched_group(db, world)
    deals.respond_to_pitch(db, deals.latest_pitch(db, gid)["token"], world["manager"], "accept")
    assert svc.get_group(db, gid)["status"] == "unlocked"
    assert svc.get_apartment(db, world["offplat"])["agent_user_id"] == world["manager"]
    assert deals.get_deal(db, gid) is not None


def test_pitch_decline_reopens_group_without_ask(db, world):
    gid = pitched_group(db, world)
    deals.respond_to_pitch(db, deals.latest_pitch(db, gid)["token"], world["manager"], "decline")
    assert svc.get_group(db, gid)["status"] == "forming"
    assert svc.group_ask(db, gid) is None


def test_pitch_expires_after_no_response(db, world):
    gid = pitched_group(db, world)
    db.execute("UPDATE pitches SET respond_by = '2000-01-01 00:00:00'")
    deals.run_deadlines(db)
    assert svc.get_group(db, gid)["status"] == "forming"
    assert deals.latest_pitch(db, gid)["status"] == "expired"


def test_counter_vote_releases_rejecters_with_refund(db, world):
    r = world["renters"]
    gid = pitched_group(db, world, size=4)
    token = deals.latest_pitch(db, gid)["token"]
    deals.respond_to_pitch(db, token, world["manager"], "counter", "waived_fees", "Half fees", "", 3)
    pitch = deals.latest_pitch(db, gid)
    assert pitch["status"] == "countered"
    deals.vote(db, pitch["id"], r[0], "accept")
    deals.vote(db, pitch["id"], r[1], "accept")
    deals.vote(db, pitch["id"], r[2], "accept")
    deals.vote(db, pitch["id"], r[3], "reject")  # last vote resolves
    assert deposit_status(db, gid, r[3]) == "refunded"
    assert not svc.is_member(db, gid, r[3])
    assert svc.get_group(db, gid)["status"] == "unlocked"  # 3 accepted >= counter size 3


def test_counter_vote_below_target_keeps_recruiting(db, world):
    r = world["renters"]
    gid = pitched_group(db, world, size=4)
    deals.respond_to_pitch(db, deals.latest_pitch(db, gid)["token"], world["manager"], "counter",
                           "rent_discount", "$50 off", "", 6)
    pitch = deals.latest_pitch(db, gid)
    deals.vote(db, pitch["id"], r[0], "accept")
    db.execute("UPDATE pitches SET vote_ends_at = '2000-01-01 00:00:00'")
    deals.run_deadlines(db)  # non-voters are released
    assert svc.member_count(db, gid) == 1
    assert svc.get_group(db, gid)["status"] == "offer_active"
    assert svc.active_offer(db, gid)["title"] == "$50 off"


# --- demo helper -----------------------------------------------------------

def test_add_fake_members_commits_with_deposits_and_stops_at_unlock(db, world):
    gid = fill(db, world["apt"], world["renters"][:3])
    svc.post_offer(db, gid, world["agent"], "rent_discount", "$50 off", "", 5)
    assert svc.add_fake_members(db, gid, 10) == 2
    assert svc.get_group(db, gid)["status"] == "unlocked"
    assert db.execute("SELECT COUNT(*) FROM deposits WHERE group_id = ? AND kind = 'commitment'",
                      (gid,)).fetchone()[0] == 5


def test_group_shortlist_tracks_members_commitments(db, world):
    r = world["renters"]
    gid = svc.create_exploratory(db, r[0], "Friends")
    svc.join_exploratory(db, gid, r[1])
    svc.add_to_shortlist(db, gid, world["apt"], r[0])
    svc.add_to_shortlist(db, gid, world["offplat"], r[1])
    with pytest.raises(svc.GroupError, match="Join the group"):
        svc.add_to_shortlist(db, gid, world["apt"], r[5])
    commit(db, world["apt"], r[0])
    commit(db, world["apt"], r[7])  # not in our group
    items = {i["apt"]["id"]: i for i in svc.shortlist(db, gid)}
    assert [m["id"] for m in items[world["apt"]]["ours"]] == [r[0]]
    assert items[world["offplat"]]["ours"] == []
    assert svc.apartment_stats(db, world["apt"])["interested"] == 2
    svc.remove_from_shortlist(db, gid, world["offplat"], r[1])
    assert len(svc.shortlist(db, gid)) == 1
