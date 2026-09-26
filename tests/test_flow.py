"""End-to-end HTTP tests on the seeded demo data."""

import re

import pytest

from app import deals
from app import groups as svc
from app.db import connect


@pytest.fixture
def db(seeded):
    conn = connect(seeded.config["DATABASE"])
    yield conn
    conn.close()


@pytest.fixture
def client(seeded):
    return seeded.test_client()


def login(client, email, password="demo1234"):
    return client.post("/login", data={"email": email, "password": password})


def slug(db, keyword):
    return db.execute("SELECT slug FROM apartments WHERE slug LIKE ?", (f"%{keyword}%",)).fetchone()["slug"]


def apt_id(db, keyword):
    return db.execute("SELECT id FROM apartments WHERE slug LIKE ?", (f"%{keyword}%",)).fetchone()["id"]


def pay_test_card(client, location):
    token = location.rstrip("/").split("/")[-1]
    return client.post(f"/checkout/{token}/pay",
                       data={"number": "4242 4242 4242 4242", "exp": "12/30", "cvc": "123", "postal": "53703"})


def test_pages_render(client, db):
    s = slug(db, "seven20")
    gid = svc.open_committed_group(db, apt_id(db, "seven20"))["id"]
    for url in ["/", "/search?zip=53703&radius=2", f"/buildings/{s}", f"/groups/{gid}", f"/groups/{gid}/status",
                f"/groups/{gid}/feed", "/login", "/signup", "/signup?role=agent"]:
        assert client.get(url).status_code == 200, url


def test_non_madison_zip_falls_back_with_message(client):
    resp = client.get("/search?zip=10001", follow_redirects=True)
    assert b"only covers Madison" in resp.data


def test_signup_login_logout(client, db):
    resp = client.post("/signup", data={"name": "New Person", "email": "new@x.test", "password": "longenough"})
    assert resp.status_code == 302
    client.post("/logout")
    assert b"don&#39;t match" in client.post("/login", data={"email": "new@x.test", "password": "wrong"}).data
    assert login(client, "new@x.test", "longenough").status_code == 302
    assert client.post("/signup", data={"name": "X", "email": "new@x.test", "password": "longenough"}).status_code == 200


def test_csrf_blocks_posts_without_token(seeded):
    seeded.config["CSRF_ENABLED"] = True
    client = seeded.test_client()
    assert client.post("/login", data={"email": "sam@example.test", "password": "demo1234"}).status_code == 400


def test_offer_path_commit_unlock_reserve_sign(client, db):
    """Seven20: Sam pays $100, simulated renters fill it, Sam reserves and signs."""
    s = slug(db, "seven20")
    gid = svc.open_committed_group(db, apt_id(db, "seven20"))["id"]
    login(client, "sam@example.test")

    resp = client.post(f"/buildings/{s}/commit")
    assert "/checkout/" in resp.headers["Location"]
    bad = client.post(resp.headers["Location"].replace("/checkout/", "/checkout/", 1).rstrip("/") + "/pay",
                      data={"number": "4111 1111 1111 1111", "exp": "12/30", "cvc": "123", "postal": "53703"})
    assert bad.status_code == 422 and b"test cards" in bad.data
    assert "/receipt" in pay_test_card(client, resp.headers["Location"]).headers["Location"]
    assert svc.is_member(db, gid, 1)

    client.post(f"/demo/groups/{gid}/add-members", data={"n": 5})
    assert svc.get_group(db, gid)["status"] == "unlocked"

    resp = client.post(f"/groups/{gid}/reserve")
    loc = resp.headers["Location"]
    token = loc.rstrip("/").split("/")[-1]
    client.post(f"/checkout/{token}/plan", data={"floor_plan": "1BR", "move_in": "2027-08-01"})
    client.post(f"/checkout/{token}/application", data={
        "legal_name": "Sam Park", "dob": "2000-01-01", "current_address": "1 Fake St", "monthly_income": "5000",
        "ssn_last4": "0000", "consent": "on"})
    assert "/receipt" in pay_test_card(client, loc).headers["Location"]

    assert b"Residential lease" in client.get(f"/groups/{gid}/sign").data
    client.post(f"/groups/{gid}/sign", data={"signature": "Sam Park"})
    deal = deals.get_deal(db, gid)
    assert deals.deal_member(db, deal["id"], 1)["status"] == "signed"
    ledger = client.get("/me").data
    assert b"Refunded" in ledger and b"Applied to rent" in ledger


def test_pitch_path_with_counter_and_vote(client, db):
    """The Bella: manager isn't on aptapt. Filling the ask sends a pitch; the manager counters; members vote."""
    bella = apt_id(db, "bella")
    gid = svc.open_committed_group(db, bella)["id"]
    login(client, "sam@example.test")
    resp = client.post(f"/buildings/{slug(db, 'bella')}/commit")
    pay_test_card(client, resp.headers["Location"])
    client.post(f"/demo/groups/{gid}/add-members", data={"n": 1})
    assert svc.get_group(db, gid)["status"] == "pitch_sent"
    pitch = deals.latest_pitch(db, gid)
    assert client.get(f"/pitch/{pitch['token']}/email").status_code == 200

    manager = connect(client.application.config["DATABASE"])
    agent_client = client.application.test_client()
    login(agent_client, pitch["to_email"])
    agent_client.post(f"/pitch/{pitch['token']}", data={
        "action": "counter", "incentive_type": "waived_fees", "title": "Waived fees only", "required_size": "2"})
    manager.close()
    assert deals.latest_pitch(db, gid)["status"] == "countered"
    assert b"countered your ask" in client.get(f"/groups/{gid}/status").data

    client.post(f"/groups/{gid}/vote", data={"choice": "accept"})
    for m in svc.members(db, gid):
        if m["id"] != 1:
            deals.vote(db, pitch["id"], m["id"], "reject" if m["is_demo"] else "accept")
    db.commit()
    assert svc.get_group(db, gid)["status"] == "unlocked"
    assert svc.get_apartment(db, bella)["agent_user_id"] is not None


def test_agent_dashboard_and_offer(client, db):
    theory = apt_id(db, "theory")
    gid = svc.open_committed_group(db, theory)["id"]
    login(client, "alex@example.test")
    client.post(f"/demo/groups/{gid}/add-members", data={"n": 1})
    client.post(f"/groups/{gid}/invite-agent")
    assert svc.get_group(db, gid)["status"] == "agent_invited"

    agent = client.application.test_client()
    login(agent, "dana@campuskey.test")
    assert b"Make an offer" in agent.get("/agent/").data
    agent.post(f"/agent/groups/{gid}/offer", data={"incentive_type": "free_month", "title": "First month free",
                                                   "required_size": "6"})
    assert svc.get_group(db, gid)["status"] == "offer_active"
    other = client.application.test_client()
    login(other, "morgan@eastline.test")
    assert other.get(f"/agent/groups/{gid}").status_code == 403


def test_leaving_committed_forfeits(client, db):
    gid = svc.open_committed_group(db, apt_id(db, "seven20"))["id"]
    login(client, "jordan@example.test")
    client.post(f"/groups/{gid}/leave")
    row = db.execute("SELECT status FROM deposits WHERE group_id = ? AND user_id = "
                     "(SELECT id FROM users WHERE email = 'jordan@example.test')", (gid,)).fetchone()
    assert row["status"] == "forfeited"
    assert re.search(rb"Forfeited", client.get("/me").data)


def test_standalone_group_shortlist_and_commit_from_it(client, db):
    login(client, "priya@example.test")
    resp = client.post("/groups/new", data={"name": "Priya's crew"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert svc.get_group(db, gid)["apartment_id"] is None
    theory = apt_id(db, "theory")
    client.post(f"/groups/{gid}/shortlist", data={"apartment_id": theory})
    page = client.get(f"/groups/{gid}").data
    assert b"Theory Madison" in page and b"Commit here" in page
    assert b"Priya&#39;s crew" in client.get(f"/buildings/{slug(db, 'theory')}").data
    resp = client.post(f"/buildings/{slug(db, 'theory')}/commit")
    pay_test_card(client, resp.headers["Location"])
    assert b"1 from this group" in client.get(f"/groups/{gid}").data
