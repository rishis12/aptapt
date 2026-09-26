# aptapt

Group leasing for Madison, WI renters. Form a group, pick a building together, and commit as a group,
so the leasing office sees ten signatures instead of one and gives all of you a better deal.

Hackathon MVP: Flask + SQLite + Jinja, with htmx for live updates. Five real downtown Madison buildings,
fictional property managers, and simulated test-mode payments (nothing is ever charged).
The full product design is in [`docs/v2-design.md`](docs/v2-design.md).

## Run it

```bash
uv venv && uv pip install -r requirements.txt   # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py                          # http://127.0.0.1:5050
```

The database (`instance/aptapt.sqlite3`) is created and seeded on first start. To reset it, run
`.venv/bin/flask --app run init-db`, or use "Reset data" in the demo bar.

Tests: `.venv/bin/python -m pytest`

Demo accounts all use the password `demo1234`. Renters: sam@, jordan@, priya@, alex@, taylor@example.test.
Leasing agents: morgan@eastline.test (Seven20), dana@campuskey.test (Theory Madison).
The demo bar at the top also lets you switch users without a password.

Configuration (environment variables):
- `SECRET_KEY`
- `AGENT_INVITE_THRESHOLD` (default 3)
- `DEMO_MODE` (default 1; set it to 0 to hide the user switcher and demo controls)

## How it works

- **Groups** are free and not tied to a building. Members chat, invite friends, and keep a shortlist of buildings.
- **Committing** to a building costs a $100 deposit (simulated). You're refunded when you sign your lease, or if the
  group misses its due date. Leaving the group forfeits it.
- **If the manager is on aptapt:** at 3 committed renters the group invites the leasing office. The office posts an offer,
  and when the group reaches the offer's size, the deal unlocks.
- **If the manager isn't on aptapt:** the group sets its own ask. When enough renters commit, aptapt pitches it to the
  manager, who can accept, counter (members vote on it), or decline.
- **After a deal unlocks:** there's a 72-hour window to reserve (unit, then application, then a $250 holding deposit),
  then you sign the lease. Anyone who doesn't reserve in time is dropped. If that leaves the group short, the manager
  can honor the deal, extend it 48 hours, or cancel it and refund everyone.
- **Payments** accept only test cards: `4242 4242 4242 4242` succeeds, `4000 0027 6000 3184` asks for bank confirmation,
  and `4000 0000 0000 0002` is declined. Only the card brand and last 4 digits are stored.

## Data and photos

Building facts live in `app/data/buildings.json`, with sources and caveats for each building. Photos are the
developers' and architects' official renderings, credited on each building page. They're fine for a private demo but
must be replaced before any public launch.

## Layout

```
app/
  groups.py      group rules: free groups, shortlists, committed groups, offers, asks
  deals.py       pitches, counter-offer votes, reservation window, signing, deadlines
  payments.py    test-mode checkout and deposit ledger
  geo.py         ZIP lookup and radius search
  seed.py        buildings, fictional managers, demo scenarios
  routes/        auth, apartments, groups, checkout, agent, pitch, demo
  templates/     Jinja pages and htmx partials
  static/        CSS, JS, vendored htmx, building photos
tests/           service tests and end-to-end HTTP tests
```
