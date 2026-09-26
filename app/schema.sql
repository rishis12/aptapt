DROP TABLE IF EXISTS deal_members;
DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS checkouts;
DROP TABLE IF EXISTS deposits;
DROP TABLE IF EXISTS votes;
DROP TABLE IF EXISTS pitches;
DROP TABLE IF EXISTS reservations;
DROP TABLE IF EXISTS shortlist;
DROP TABLE IF EXISTS invites;
DROP TABLE IF EXISTS offers;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS memberships;
DROP TABLE IF EXISTS groups;
DROP TABLE IF EXISTS apartments;
DROP TABLE IF EXISTS zip_centroids;
DROP TABLE IF EXISTS users;

-- All timestamps are naive local time strings 'YYYY-MM-DD HH:MM:SS' (see clock.py).

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('renter', 'agent')),
  company TEXT,
  password_hash TEXT,                  -- NULL for generated demo renters (they can't sign in)
  is_demo INTEGER NOT NULL DEFAULT 0,  -- generated "fake member" accounts
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE zip_centroids (
  zip TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  lat REAL NOT NULL,
  lng REAL NOT NULL
);

CREATE TABLE apartments (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  neighborhood TEXT NOT NULL,
  zip TEXT NOT NULL,
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('market', 'student')),
  leasing TEXT NOT NULL CHECK (leasing IN ('per_unit', 'per_bed')),
  stories INTEGER,
  units INTEGER,
  beds INTEGER,
  developer TEXT,
  opening TEXT,
  status_text TEXT,
  floor_plans TEXT NOT NULL DEFAULT '[]',  -- JSON list
  rent_min INTEGER,
  rent_max INTEGER,
  rent_is_estimate INTEGER NOT NULL DEFAULT 1,
  amenities TEXT NOT NULL DEFAULT '[]',    -- JSON list
  description TEXT NOT NULL DEFAULT '',
  website TEXT,
  info_url TEXT,                           -- official site, else the best public project page
  sources TEXT NOT NULL DEFAULT '[]',      -- JSON list of source URLs
  photos TEXT NOT NULL DEFAULT '[]',       -- JSON list of {file, credit, source_url}
  manager_company TEXT NOT NULL,           -- fictional management company
  contact_name TEXT NOT NULL,              -- fictional leasing contact (pitch recipient)
  contact_email TEXT NOT NULL,
  agent_user_id INTEGER REFERENCES users (id)  -- NULL = manager not on aptapt yet
);

-- Exploratory groups: free, not tied to a building (apartment_id NULL). Members chat and keep a
--   shortlist of buildings, then each member commits to a building from there.
-- Committed groups: belong to one building; numbered series (seq), at most one "open" one at a time.
CREATE TABLE groups (
  id INTEGER PRIMARY KEY,
  apartment_id INTEGER REFERENCES apartments (id),
  kind TEXT NOT NULL CHECK (kind IN ('exploratory', 'committed')),
  name TEXT NOT NULL,
  seq INTEGER,
  status TEXT NOT NULL CHECK (status IN
    ('open', 'forming', 'agent_invited', 'offer_active', 'pitch_sent', 'unlocked', 'expired')),
  due_at TEXT,
  created_by INTEGER REFERENCES users (id),
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  closed_at TEXT
);
CREATE INDEX groups_apartment ON groups (apartment_id, kind);

CREATE TABLE memberships (
  group_id INTEGER NOT NULL REFERENCES groups (id),
  user_id INTEGER NOT NULL REFERENCES users (id),
  joined_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (group_id, user_id)
);

-- Chat messages and the activity feed share one table.
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups (id),
  user_id INTEGER REFERENCES users (id),
  kind TEXT NOT NULL CHECK (kind IN ('chat', 'system')),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX messages_group ON messages (group_id, id);

-- source 'agent': an offer from the leasing agent (or a counter-offer, status 'proposed' while members vote).
-- source 'group': the group's own ask, used when the manager isn't on aptapt.
CREATE TABLE offers (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups (id),
  author_id INTEGER NOT NULL REFERENCES users (id),
  source TEXT NOT NULL CHECK (source IN ('agent', 'group')),
  incentive_type TEXT NOT NULL,
  title TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  required_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('active', 'proposed', 'superseded', 'withdrawn', 'accepted', 'declined', 'rejected')),
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Buildings an exploratory group is considering.
CREATE TABLE shortlist (
  group_id INTEGER NOT NULL REFERENCES groups (id),
  apartment_id INTEGER NOT NULL REFERENCES apartments (id),
  added_by INTEGER REFERENCES users (id),
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (group_id, apartment_id)
);

CREATE TABLE invites (
  token TEXT PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups (id),
  kind TEXT NOT NULL CHECK (kind IN ('friend', 'agent')),
  created_by INTEGER NOT NULL REFERENCES users (id),
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- A group's ask, sent to a manager who isn't on aptapt (simulated email).
CREATE TABLE pitches (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups (id),
  ask_offer_id INTEGER NOT NULL REFERENCES offers (id),
  token TEXT NOT NULL UNIQUE,
  to_name TEXT NOT NULL,
  to_email TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('sent', 'accepted', 'countered', 'declined', 'expired', 'resolved')),
  counter_offer_id INTEGER REFERENCES offers (id),
  sent_at TEXT NOT NULL,
  respond_by TEXT NOT NULL,
  vote_ends_at TEXT,
  responded_at TEXT
);

CREATE TABLE votes (
  pitch_id INTEGER NOT NULL REFERENCES pitches (id),
  user_id INTEGER NOT NULL REFERENCES users (id),
  choice TEXT NOT NULL CHECK (choice IN ('accept', 'reject')),
  PRIMARY KEY (pitch_id, user_id)
);

-- Simulated money ledger. Nothing here is real.
CREATE TABLE deposits (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users (id),
  group_id INTEGER NOT NULL REFERENCES groups (id),
  kind TEXT NOT NULL CHECK (kind IN ('commitment', 'holding')),
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('held', 'refunded', 'forfeited', 'applied')),
  card_brand TEXT NOT NULL,
  card_last4 TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  resolved_at TEXT
);

-- A test-mode checkout session. Card numbers are never stored, only brand + last 4.
CREATE TABLE checkouts (
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users (id),
  group_id INTEGER NOT NULL REFERENCES groups (id),
  purpose TEXT NOT NULL CHECK (purpose IN ('commitment', 'holding')),
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open', 'needs_verification', 'succeeded', 'failed')),
  step TEXT NOT NULL DEFAULT 'pay',       -- holding: 'plan' -> 'application' -> 'pay'
  floor_plan TEXT,
  move_in TEXT,
  card_brand TEXT,
  card_last4 TEXT,
  error TEXT,
  deposit_id INTEGER REFERENCES deposits (id),
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Opened when a committed group unlocks.
CREATE TABLE deals (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL UNIQUE REFERENCES groups (id),
  offer_id INTEGER NOT NULL REFERENCES offers (id),
  status TEXT NOT NULL CHECK (status IN ('reserving', 'short', 'closed', 'cancelled')),
  window_ends_at TEXT NOT NULL,
  window_closed INTEGER NOT NULL DEFAULT 0,
  extended INTEGER NOT NULL DEFAULT 0,
  honored INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  closed_at TEXT
);

CREATE TABLE deal_members (
  deal_id INTEGER NOT NULL REFERENCES deals (id),
  user_id INTEGER NOT NULL REFERENCES users (id),
  status TEXT NOT NULL CHECK (status IN ('pending', 'reserved', 'signed', 'dropped')),
  floor_plan TEXT,
  move_in TEXT,
  reserved_at TEXT,
  signed_at TEXT,
  signature TEXT,
  PRIMARY KEY (deal_id, user_id)
);
