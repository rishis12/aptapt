import pytest

from app import payments
from app import groups as svc

from .conftest import make_apartment, make_user


@pytest.fixture
def checkout(db):
    apt = make_apartment(db)
    user = make_user(db, "Payer")
    gid = svc.get_or_create_open_committed(db, apt)["id"]
    token = payments.start_commitment(db, gid, user)
    return {"db": db, "token": token, "group": gid, "user": user}


def co(c):
    return payments.get_checkout(c["db"], c["token"])


def test_real_looking_cards_are_refused(checkout):
    with pytest.raises(payments.PaymentError, match="only accepts test cards"):
        payments.pay(checkout["db"], co(checkout), "4111 1111 1111 1111", "12/30", "123", "53703")


def test_test_card_succeeds_and_stores_only_last4(checkout):
    assert payments.pay(checkout["db"], co(checkout), "4242 4242 4242 4242", "12/30", "123", "53703") == "succeeded"
    row = co(checkout)
    assert row["status"] == "succeeded" and row["card_last4"] == "4242"
    stored = " ".join(str(v) for v in row)
    assert "4242424242424242" not in stored
    assert svc.is_member(checkout["db"], checkout["group"], checkout["user"])


def test_decline_card(checkout):
    with pytest.raises(payments.PaymentError, match="declined"):
        payments.pay(checkout["db"], co(checkout), "4000 0000 0000 0002", "12/30", "123", "53703")
    assert not svc.is_member(checkout["db"], checkout["group"], checkout["user"])


def test_verification_card_needs_approval(checkout):
    assert payments.pay(checkout["db"], co(checkout), "4000002760003184", "12/30", "123", "53703") == "needs_verification"
    assert not svc.is_member(checkout["db"], checkout["group"], checkout["user"])
    payments.verify(checkout["db"], co(checkout), approved=True)
    assert svc.is_member(checkout["db"], checkout["group"], checkout["user"])


@pytest.mark.parametrize("exp,cvc,postal,msg", [
    ("13/30", "123", "53703", "MM/YY"), ("01/20", "123", "53703", "expired"),
    ("12/30", "1", "53703", "security code"), ("12/30", "123", "537", "ZIP"),
])
def test_card_field_validation(checkout, exp, cvc, postal, msg):
    with pytest.raises(payments.PaymentError, match=msg):
        payments.pay(checkout["db"], co(checkout), "4242424242424242", exp, cvc, postal)


def test_application_rejects_anything_but_four_digits(db):
    apt = make_apartment(db)
    c = {"id": 0}
    with pytest.raises(payments.PaymentError, match="4 digits"):
        payments.save_application(db, c, {"legal_name": "A", "dob": "x", "current_address": "x",
                                          "monthly_income": "1", "ssn_last4": "123-45-6789", "consent": "on"})
    assert apt
