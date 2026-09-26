import pytest

from app import create_app
from app.db import connect, init_schema
from app.seed import BUILDINGS_JSON, seed


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "CSRF_ENABLED": False,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "AGENT_INVITE_THRESHOLD": 3,
    })
    conn = connect(app.config["DATABASE"])
    init_schema(conn)
    conn.close()
    return app


@pytest.fixture
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


@pytest.fixture
def seeded(app):
    if not BUILDINGS_JSON.exists():
        pytest.skip("app/data/buildings.json not present")
    conn = connect(app.config["DATABASE"])
    seed(conn)
    conn.close()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(db, name, role="renter"):
    email = f"{name.lower().replace(' ', '.')}@t.test"
    return db.execute("INSERT INTO users (name, email, role) VALUES (?, ?, ?)", (name, email, role)).lastrowid


def make_apartment(db, agent_id=None, slug="test-tower"):
    return db.execute(
        """INSERT INTO apartments (slug, name, address, neighborhood, zip, lat, lng, kind, leasing, floor_plans,
           rent_min, rent_max, manager_company, contact_name, contact_email, agent_user_id)
           VALUES (?, 'Test Tower', '1 Test St', 'Test', '53703', 43.0766, -89.3831, 'market', 'per_unit',
           '["Studio","1BR"]', 1000, 2000, 'Fictional Mgmt', 'Pat Contact', 'pat@t.test', ?)""",
        (slug, agent_id),
    ).lastrowid
