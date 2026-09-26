"""Email + password accounts. Leasing agents sign up through an invite or pitch link (or pick "agent")."""

import functools
import re

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import get_db

bp = Blueprint("auth", __name__)
MIN_PASSWORD = 8


@bp.before_app_request
def load_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def safe_next(target, default=None):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return default or url_for("apartments.index")


def current_path():
    return request.full_path.rstrip("?")


def _sign_in(user_id):
    csrf = session.get("_csrf")
    session.clear()
    session["user_id"] = user_id
    if csrf:
        session["_csrf"] = csrf
    session.permanent = True


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=current_path()))
        return view(*args, **kwargs)
    return wrapped


def renter_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "renter":
            flash("That's for renters. You're signed in as a leasing agent.", "error")
            return redirect(url_for("agent.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def agent_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None or g.user["role"] != "agent":
            flash("Sign in with your leasing agent account to continue.", "info")
            return redirect(url_for("auth.login", role="agent", next=current_path()))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    role = request.values.get("role", "renter")
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
            flash("That email and password don't match an account.", "error")
        else:
            _sign_in(user["id"])
            default = url_for("agent.dashboard") if user["role"] == "agent" else None
            return redirect(safe_next(next_url, default))
    return render_template("auth/login.html", next=next_url, role=role, email=email)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    next_url = request.values.get("next", "")
    role = request.values.get("role", "renter")
    if role not in ("renter", "agent"):
        role = "renter"
    form = {"name": "", "email": "", "company": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        form["email"] = form["email"].lower()
        password = request.form.get("password", "")
        db = get_db()
        error = None
        if not form["name"]:
            error = "Enter your name."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form["email"]):
            error = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD:
            error = f"Use at least {MIN_PASSWORD} characters for your password."
        elif db.execute("SELECT 1 FROM users WHERE email = ?", (form["email"],)).fetchone():
            error = "There's already an account with that email. Sign in instead."
        if error:
            flash(error, "error")
        else:
            cur = db.execute(
                "INSERT INTO users (name, email, role, company, password_hash) VALUES (?, ?, ?, ?, ?)",
                (form["name"], form["email"], role, form["company"] or None, generate_password_hash(password)),
            )
            db.commit()
            _sign_in(cur.lastrowid)
            flash(f"Welcome to aptapt, {form['name'].split()[0]}.", "success")
            default = url_for("agent.dashboard") if role == "agent" else None
            return redirect(safe_next(next_url, default))
    return render_template("auth/signup.html", next=next_url, role=role, form=form)


@bp.post("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("apartments.index"))


@bp.post("/switch")
def switch_user():
    """Demo only: act as any seeded account without a password."""
    if not current_app.config["DEMO_MODE"]:
        return redirect(url_for("apartments.index"))
    user = get_db().execute(
        "SELECT * FROM users WHERE id = ? AND is_demo = 0", (request.form.get("user_id", type=int),)
    ).fetchone()
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("apartments.index"))
    _sign_in(user["id"])
    flash(f"You're now using the app as {user['name']}.", "info")
    next_url = request.form.get("next", "")
    if user["role"] == "agent" and not next_url.startswith(("/agent", "/pitch")):
        return redirect(url_for("agent.dashboard"))
    if user["role"] == "renter" and next_url.startswith(("/agent", "/pitch")):
        return redirect(url_for("groups.my_groups"))
    return redirect(safe_next(next_url))
