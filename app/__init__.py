import json
import os
import secrets
from datetime import datetime

import click
from flask import Flask, abort, g, request, session
from markupsafe import Markup

from . import clock, deals
from . import db as dbmod
from . import groups as svc
from . import payments
from .seed import seed, sync_building_links


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-not-secret"),
        DATABASE=os.path.join(app.instance_path, "aptapt.sqlite3"),
        AGENT_INVITE_THRESHOLD=int(os.environ.get("AGENT_INVITE_THRESHOLD", "3")),
        DEMO_MODE=os.environ.get("DEMO_MODE", "1") == "1",
        GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY"),
        GEMINI_MODEL=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
        CSRF_ENABLED=True,
        DEFAULT_ZIP="53703",
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)

    if not app.config.get("TESTING") and not os.path.exists(app.config["DATABASE"]):
        conn = dbmod.connect(app.config["DATABASE"])
        seed(conn)
        conn.close()

    if not app.config.get("TESTING"):
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            sync_building_links(conn)
        finally:
            conn.close()

    app.teardown_appcontext(dbmod.close_db)

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and app.config["CSRF_ENABLED"]:
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or sent != session.get("_csrf"):
                abort(400, "Your session expired. Go back, refresh the page, and try again.")

    @app.before_request
    def advance_deadlines():
        if request.endpoint == "static":
            return
        db = dbmod.get_db()
        if deals.run_deadlines(db):
            db.commit()

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)
        return session["_csrf"]

    from .routes import agent, apartments, auth, checkout, demo, groups, pitch

    for module in (auth, apartments, groups, checkout, agent, pitch, demo):
        app.register_blueprint(module.bp)

    @app.url_defaults
    def bust_static_cache(endpoint, values):
        """Version static URLs by file mtime so browsers never run a stale stylesheet or script."""
        if endpoint == "static" and "filename" in values:
            path = os.path.join(app.static_folder, values["filename"])
            if os.path.isfile(path):
                values["v"] = int(os.stat(path).st_mtime)

    @app.context_processor
    def inject_globals():
        demo_users = []
        if app.config["DEMO_MODE"]:
            demo_users = dbmod.get_db().execute(
                """SELECT id, name, role, company FROM users
                   WHERE is_demo = 0 AND password_hash IS NOT NULL ORDER BY role DESC, name"""
            ).fetchall()
        return {
            "current_user": g.get("user"),
            "demo_mode": app.config["DEMO_MODE"],
            "demo_users": demo_users,
            "incentive_types": svc.INCENTIVE_TYPES,
            "invite_threshold": app.config["AGENT_INVITE_THRESHOLD"],
            "csrf_token": csrf_token,
            "csrf_field": lambda: Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">'),
        }

    @app.template_test("divisibleby_target")
    def divisibleby_target(cols, target):
        return target % cols == 0

    @app.template_filter("first_name")
    def first_name(name):
        return (name or "").split()[0] if name else ""

    @app.template_filter("domain")
    def domain(url):
        from urllib.parse import urlparse
        host = urlparse(url or "").netloc
        return host[4:] if host.startswith("www.") else host

    @app.template_filter("money")
    def money(value):
        return "—" if value is None else f"${value:,.0f}"

    @app.template_filter("cents")
    def cents(value):
        return payments.cents(value)

    @app.template_filter("fromjson")
    def fromjson(value):
        return json.loads(value or "[]")

    @app.template_filter("initials")
    def initials(name):
        words = [w for w in name.split() if w.lower() != "the"]
        return "".join(w[0] for w in words[:2]).upper()

    @app.template_filter("until")
    def until(value):
        """'3 days left', '5 hours left', or 'now'."""
        target = clock.parse(value)
        if target is None:
            return ""
        seconds = (target - clock.now()).total_seconds()
        if seconds <= 0:
            return "now"
        if seconds >= 86400:
            n = int(seconds // 86400)
            return f"{n} day{'s' if n != 1 else ''} left"
        if seconds >= 3600:
            n = int(seconds // 3600)
            return f"{n} hour{'s' if n != 1 else ''} left"
        n = max(1, int(seconds // 60))
        return f"{n} minute{'s' if n != 1 else ''} left"

    @app.template_filter("date")
    def date_filter(value, fmt="%b %-d, %Y"):
        if not value:
            return ""
        if len(value) == 10:
            return clock.strftime(datetime.strptime(value, "%Y-%m-%d"), fmt)
        return clock.strftime(clock.parse(value), fmt)

    @app.cli.command("init-db")
    def init_db_command():
        """Drop everything and reseed the Madison demo data."""
        conn = dbmod.connect(app.config["DATABASE"])
        seed(conn)
        conn.close()
        click.echo(f"Seeded {app.config['DATABASE']}")

    return app
