import sqlite3
from contextlib import contextmanager

from . import codes, config

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT '',
    custom_slug TEXT NOT NULL DEFAULT '',
    tracked INTEGER NOT NULL DEFAULT 0,
    entry_fee_cents INTEGER NOT NULL DEFAULT 0,
    last_synced TEXT,
    venue_name TEXT NOT NULL DEFAULT '',
    venue_flat_cents INTEGER NOT NULL DEFAULT 0,
    venue_entry_pct TEXT NOT NULL DEFAULT '0',
    venue_bracket_pct TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS tournament (
    id TEXT PRIMARY KEY,
    series_id INTEGER REFERENCES series(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    slug TEXT,
    start_date TEXT,
    num_attendees INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0,
    entry_fee_cents INTEGER,
    venue_mode TEXT NOT NULL DEFAULT 'series',
    venue_flat_cents INTEGER NOT NULL DEFAULT 0,
    venue_entry_pct TEXT NOT NULL DEFAULT '0',
    venue_bracket_pct TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS event (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES tournament(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    game_name TEXT NOT NULL DEFAULT '',
    entrant_count INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 1,
    slug TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    game_name TEXT NOT NULL DEFAULT '',
    event_id TEXT REFERENCES event(id) ON DELETE CASCADE,
    bracket_fee_cents INTEGER NOT NULL DEFAULT 0,
    payout_places INTEGER NOT NULL DEFAULT 3,
    fixed_pool_cents INTEGER NOT NULL DEFAULT 0,
    bracket_fee_pool_pct TEXT NOT NULL DEFAULT '0',
    entry_fee_share_pct TEXT NOT NULL DEFAULT '0',
    splits TEXT NOT NULL DEFAULT '["100"]',
    UNIQUE (series_id, game_name, event_id)
);
CREATE TABLE IF NOT EXISTS player (
    id TEXT PRIMARY KEY,
    gamer_tag TEXT NOT NULL,
    merged_into TEXT REFERENCES player(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS tournament_entry (
    tournament_id TEXT NOT NULL REFERENCES tournament(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    PRIMARY KEY (tournament_id, player_id)
);
CREATE TABLE IF NOT EXISTS event_entry (
    event_id TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    placement INTEGER,
    PRIMARY KEY (event_id, player_id)
);
CREATE TABLE IF NOT EXISTS payout (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    placement INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    computed_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'owed',
    paid_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (event_id, player_id)
);
CREATE TABLE IF NOT EXISTS venue_payment (
    tournament_id TEXT PRIMARY KEY REFERENCES tournament(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    computed_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'owed',
    paid_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_payout_player ON payout(player_id);
CREATE INDEX IF NOT EXISTS idx_event_tournament ON event(tournament_id);
CREATE INDEX IF NOT EXISTS idx_tournament_series ON tournament(series_id);
"""

# Entries keep the parry.gg user id they were imported under; these views report each one under the player it was
# merged into, so a merge survives re-syncs and can be undone. Created after ADDED_COLUMNS (they need merged_into).
VIEWS = """
CREATE VIEW IF NOT EXISTS event_entry_c AS
    SELECT ee.event_id, COALESCE(p.merged_into, ee.player_id) AS player_id, MIN(ee.placement) AS placement
    FROM event_entry ee JOIN player p ON p.id=ee.player_id GROUP BY 1, 2;
CREATE VIEW IF NOT EXISTS tournament_entry_c AS
    SELECT DISTINCT te.tournament_id, COALESCE(p.merged_into, te.player_id) AS player_id
    FROM tournament_entry te JOIN player p ON p.id=te.player_id;
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release; databases created earlier gain them on startup.
ADDED_COLUMNS = {
    "series": {
        "code": "TEXT NOT NULL DEFAULT ''",
        "custom_slug": "TEXT NOT NULL DEFAULT ''",
        "venue_name": "TEXT NOT NULL DEFAULT ''",
        "venue_flat_cents": "INTEGER NOT NULL DEFAULT 0",
        "venue_entry_pct": "TEXT NOT NULL DEFAULT '0'",
        "venue_bracket_pct": "TEXT NOT NULL DEFAULT '0'",
    },
    "player": {
        "merged_into": "TEXT REFERENCES player(id) ON DELETE SET NULL",
    },
    "event": {
        "slug": "TEXT NOT NULL DEFAULT ''",
    },
    "tournament": {
        "venue_mode": "TEXT NOT NULL DEFAULT 'series'",
        "venue_flat_cents": "INTEGER NOT NULL DEFAULT 0",
        "venue_entry_pct": "TEXT NOT NULL DEFAULT '0'",
        "venue_bracket_pct": "TEXT NOT NULL DEFAULT '0'",
    },
}


def _add_columns(conn):
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _backfill_series_codes(conn):
    """Give series created before codes existed a code derived from their name, then enforce uniqueness."""
    taken = {r["code"] for r in conn.execute("SELECT code FROM series WHERE code != ''")}
    for r in conn.execute("SELECT id, name FROM series WHERE code='' ORDER BY id").fetchall():
        code = codes.suggest_code(r["name"], taken)
        taken.add(code)
        conn.execute("UPDATE series SET code=? WHERE id=?", (code, r["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_series_code ON series(code) WHERE code != ''")


def init():
    with session() as conn:
        conn.executescript(SCHEMA)
        _add_columns(conn)
        conn.executescript(VIEWS)
        _backfill_series_codes(conn)
