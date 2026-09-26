import pytest

from app import deals, payments
from app import groups as svc

from .conftest import make_apartment, make_user
from .test_groups import commit, deposit_status, fill


@pytest.fixture
def deal(db):
    agent = make_user(db, "Agent A", role="agent")
    apt = make_apartment(db, agent_id=agent)
    renters = [make_user(db, f"Renter {i}") for i in range(8)]
    gid = fill(db, apt, renters[:3])
    svc.post_offer(db, gid, agent, "rent_discount", "$100 off", "", 4)
    commit(db, apt, renters[3])
    d = deals.get_deal(db, gid)
    return {"id": d["id"], "group": gid, "apt": apt, "agent": agent, "renters": renters}


def reserve(db, d, user_id, plan="1BR"):
    token = payments.start_holding(db, d["group"], user_id)
    co = payments.get_checkout(db, token)
    payments.save_plan(db, co, plan, "2027-06-01", ["Studio", "1BR"], None)
    payments.save_application(db, payments.get_checkout(db, token), {
        "legal_name": "X", "dob": "2000-01-01", "current_address": "1 Main", "monthly_income": "5000",
        "ssn_last4": "0000", "consent": "on"})
    payments.pay(db, payments.get_checkout(db, token), "4242 4242 4242 4242", "12/30", "123", "53703")


def test_unlock_opens_deal_with_everyone_pending(db, deal):
    counts = deals.deal_counts(db, deal["id"])
    assert counts["pending"] == 4 and counts["active"] == 4


def test_reserve_then_sign_refunds_commitment_and_applies_holding(db, deal):
    u = deal["renters"][0]
    reserve(db, deal, u)
    assert deals.deal_member(db, deal["id"], u)["status"] == "reserved"
    assert deposit_status(db, deal["group"], u, "holding") == "held"
    with pytest.raises(svc.GroupError, match="exactly"):
        deals.sign(db, deal["id"], u, "Wrong Name")
    deals.sign(db, deal["id"], u, "renter 0")
    assert deposit_status(db, deal["group"], u) == "refunded"
    assert deposit_status(db, deal["group"], u, "holding") == "applied"


def test_everyone_signing_closes_deal(db, deal):
    for u in deal["renters"][:4]:
        reserve(db, deal, u)
        deals.sign(db, deal["id"], u, svc.user_name(db, u))
    assert deals.get_deal_by_id(db, deal["id"])["status"] == "closed"


def test_window_end_drops_unreserved_and_goes_short(db, deal):
    r = deal["renters"]
    reserve(db, deal, r[0])
    reserve(db, deal, r[1])
    reserve(db, deal, r[2])
    db.execute("UPDATE deals SET window_ends_at = '2000-01-01 00:00:00'")
    deals.run_deadlines(db)
    assert deals.deal_member(db, deal["id"], r[3])["status"] == "dropped"
    assert deposit_status(db, deal["group"], r[3]) == "forfeited"
    assert deals.get_deal_by_id(db, deal["id"])["status"] == "short"


def test_short_deal_honor_then_close(db, deal):
    r = deal["renters"]
    for u in r[:3]:
        reserve(db, deal, u)
    deals.end_window(db, deal["id"])
    deals.resolve_short(db, deal["id"], deal["agent"], "honor")
    for u in r[:3]:
        deals.sign(db, deal["id"], u, svc.user_name(db, u))
    assert deals.get_deal_by_id(db, deal["id"])["status"] == "closed"


def test_short_deal_extend_lets_new_renters_commit(db, deal):
    r = deal["renters"]
    for u in r[:3]:
        reserve(db, deal, u)
    deals.end_window(db, deal["id"])
    deals.resolve_short(db, deal["id"], deal["agent"], "extend")
    payments.record_commitment(db, deal["group"], r[6], "Visa", "4242")
    assert deals.deal_member(db, deal["id"], r[6])["status"] == "pending"


def test_short_deal_cancel_refunds_all_held_deposits(db, deal):
    r = deal["renters"]
    reserve(db, deal, r[0])
    deals.end_window(db, deal["id"])
    deals.resolve_short(db, deal["id"], deal["agent"], "cancel")
    assert deposit_status(db, deal["group"], r[0]) == "refunded"
    assert deposit_status(db, deal["group"], r[0], "holding") == "refunded"


def test_backing_out_after_unlock_forfeits_commitment(db, deal):
    u = deal["renters"][0]
    reserve(db, deal, u)
    svc.leave_committed(db, deal["group"], u)
    assert deposit_status(db, deal["group"], u) == "forfeited"
    assert deposit_status(db, deal["group"], u, "holding") == "refunded"


def test_nudge_requires_agent(db, deal):
    with pytest.raises(svc.GroupError):
        deals.nudge(db, deal["id"], deal["renters"][0], deal["renters"][1])
    deals.nudge(db, deal["id"], deal["agent"], deal["renters"][1])
