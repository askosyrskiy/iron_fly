from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

SESSION_TTL = timedelta(days=14)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            google_sub TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            name TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS credential (
            id INTEGER PRIMARY KEY,
            broker TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            secrets_enc BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_session (
            id TEXT PRIMARY KEY,
            google_sub TEXT NOT NULL,
            csrf TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fly (
            id INTEGER PRIMARY KEY,
            strike REAL NOT NULL,
            wings INTEGER NOT NULL,
            credit REAL NOT NULL,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL,
            close_debit REAL,
            closed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS desk_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            regime TEXT NOT NULL DEFAULT 's1',
            stop_pad INTEGER NOT NULL DEFAULT 0,
            em_open REAL,
            em_open_day TEXT
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(desk_state)")}
    if "spec_yaml" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN spec_yaml TEXT NOT NULL DEFAULT ''")
    if "run_mode" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN run_mode TEXT NOT NULL DEFAULT 'manual'")
    if "book_day" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN book_day TEXT")
    if "use_config" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN use_config INTEGER NOT NULL DEFAULT 1")
    if "auto_open" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN auto_open INTEGER NOT NULL DEFAULT 0")
    if "auto_drop" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN auto_drop INTEGER NOT NULL DEFAULT 0")
    if "eff_regime" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN eff_regime TEXT")
    if "eff_stop" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN eff_stop INTEGER")
    if "eff_wings" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN eff_wings TEXT")
    if "eff_f1" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN eff_f1 TEXT")
    if "eff_clock" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN eff_clock TEXT")
    if "applied_at" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN applied_at TEXT")
    if "wings" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN wings INTEGER NOT NULL DEFAULT 50")
    if "ladder_json" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN ladder_json TEXT NOT NULL DEFAULT '[]'")
    if "em_open_samples" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_open_samples TEXT NOT NULL DEFAULT '[]'")
    fly_columns = {row[1] for row in conn.execute("PRAGMA table_info(fly)")}
    if "origin" not in fly_columns:
        conn.execute("ALTER TABLE fly ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
    _migrate_players(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(desk_state)")}
    if "em_pct" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_pct REAL")
    if "put40_wings" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN put40_wings REAL")
    if "em_now" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_now REAL")
    if "step_json" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN step_json TEXT NOT NULL DEFAULT ''")
    if "open_paused" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN open_paused INTEGER NOT NULL DEFAULT 0")
    if "cash_close" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN cash_close REAL")
    if "cash_close_day" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN cash_close_day TEXT")
    if "em_low" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_low REAL")
    if "em_pct_low" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_pct_low REAL")
    if "em_low_day" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN em_low_day TEXT")
    if "regime_latch" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN regime_latch TEXT")
    if "open_conflict" not in columns:
        conn.execute("ALTER TABLE desk_state ADD COLUMN open_conflict TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leftover_long (
            player_id TEXT NOT NULL,
            occ TEXT NOT NULL,
            right TEXT NOT NULL,
            strike REAL NOT NULL,
            quantity INTEGER NOT NULL,
            PRIMARY KEY (player_id, occ)
        )
        """
    )
    long_columns = {row[1] for row in conn.execute("PRAGMA table_info(leftover_long)")}
    if "from_strike" not in long_columns:
        conn.execute("ALTER TABLE leftover_long ADD COLUMN from_strike REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS desk_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leftover_roll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            rolled_at TEXT NOT NULL,
            right TEXT NOT NULL,
            from_strike REAL NOT NULL,
            to_strike REAL NOT NULL,
            mark REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS parked_cashflow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            session_day TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            occ TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            amount_usd_cents INTEGER NOT NULL
        )
        """
    )
    fly_columns = {row[1] for row in conn.execute("PRAGMA table_info(fly)")}
    if "pnl" not in fly_columns:
        conn.execute("ALTER TABLE fly ADD COLUMN pnl REAL")
    if "sales_json" not in fly_columns:
        conn.execute("ALTER TABLE fly ADD COLUMN sales_json TEXT NOT NULL DEFAULT '[]'")
    if "call_wings" not in fly_columns:
        conn.execute("ALTER TABLE fly ADD COLUMN call_wings INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fly_history (
            id INTEGER PRIMARY KEY,
            player_id TEXT NOT NULL,
            session_day TEXT NOT NULL,
            strike REAL NOT NULL,
            wings INTEGER NOT NULL,
            credit REAL NOT NULL,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL,
            close_debit REAL,
            closed_at TEXT,
            origin TEXT NOT NULL DEFAULT '',
            pnl REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_pending_close (
            player_id TEXT NOT NULL,
            strike REAL NOT NULL,
            opened_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            wings INTEGER NOT NULL,
            PRIMARY KEY (player_id, strike, opened_at)
        )
        """
    )


_PLAYER_STATE = """
CREATE TABLE desk_state (
    player_id TEXT PRIMARY KEY,
    regime TEXT NOT NULL DEFAULT 's1',
    stop_pad INTEGER NOT NULL DEFAULT 0,
    em_open REAL,
    em_open_day TEXT,
    spec_yaml TEXT NOT NULL DEFAULT '',
    run_mode TEXT NOT NULL DEFAULT 'manual',
    book_day TEXT,
    use_config INTEGER NOT NULL DEFAULT 1,
    auto_open INTEGER NOT NULL DEFAULT 0,
    auto_drop INTEGER NOT NULL DEFAULT 0,
    eff_regime TEXT,
    eff_stop INTEGER,
    eff_wings TEXT,
    eff_f1 TEXT,
    eff_clock TEXT,
    applied_at TEXT,
    wings INTEGER NOT NULL DEFAULT 50,
    ladder_json TEXT NOT NULL DEFAULT '[]',
    em_open_samples TEXT NOT NULL DEFAULT '[]',
    em_pct REAL,
    put40_wings REAL,
    em_now REAL,
    step_json TEXT NOT NULL DEFAULT '',
    em_low REAL,
    em_pct_low REAL,
    em_low_day TEXT,
    regime_latch TEXT
)
"""


def _migrate_players(conn: sqlite3.Connection) -> None:
    """One book per player. Private desks keep the existing row as 'owner'."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(desk_state)")}
    if "player_id" not in columns:
        previous = conn.execute("SELECT * FROM desk_state WHERE id = 1").fetchone()
        conn.execute("ALTER TABLE desk_state RENAME TO desk_state_v1")
        conn.execute(_PLAYER_STATE)
        if previous is not None:
            fields = (
                "regime",
                "stop_pad",
                "em_open",
                "em_open_day",
                "spec_yaml",
                "run_mode",
                "book_day",
                "use_config",
                "auto_open",
                "auto_drop",
                "eff_regime",
                "eff_stop",
                "eff_wings",
                "eff_f1",
                "eff_clock",
                "applied_at",
                "wings",
                "ladder_json",
                "em_open_samples",
            )
            present = [name for name in fields if name in previous.keys()]
            conn.execute(
                f"INSERT INTO desk_state (player_id, {', '.join(present)}) VALUES (?, {', '.join('?' for _ in present)})",
                ["owner", *[previous[name] for name in present]],
            )
        conn.execute("DROP TABLE desk_state_v1")
    fly_columns = {row[1] for row in conn.execute("PRAGMA table_info(fly)")}
    if "player_id" not in fly_columns:
        conn.execute("ALTER TABLE fly ADD COLUMN player_id TEXT NOT NULL DEFAULT 'owner'")


def _now() -> datetime:
    return datetime.now(UTC)


def start_login_session(conn: sqlite3.Connection, *, google_sub: str, session_id: str, csrf: str) -> None:
    """Replace every login. Only the new session can open the desk."""
    now = _now()
    expires = now + SESSION_TTL
    conn.execute("DELETE FROM login_session")
    conn.execute(
        """
        INSERT INTO login_session (id, google_sub, csrf, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, google_sub, csrf, now.isoformat(), expires.isoformat()),
    )


def get_login_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM login_session WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= _now():
        conn.execute("DELETE FROM login_session WHERE id = ?", (session_id,))
        return None
    return row


def delete_login_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM login_session WHERE id = ?", (session_id,))


def get_user(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM user WHERE id = 1").fetchone()


def claim_user(
    conn: sqlite3.Connection,
    *,
    google_sub: str,
    email: str,
    name: str | None,
    allowed_email: str,
) -> str:
    """Return ok, wrong_email, or claimed."""
    if email.casefold() != allowed_email.casefold():
        return "wrong_email"
    existing = get_user(conn)
    if existing is not None:
        if existing["google_sub"] == google_sub:
            return "ok"
        return "claimed"
    now = datetime.now(UTC).isoformat()
    try:
        conn.execute(
            """
            INSERT INTO user (id, google_sub, email, name, created_at)
            VALUES (1, ?, ?, ?, ?)
            """,
            (google_sub, email, name, now),
        )
    except sqlite3.IntegrityError:
        return "claimed"
    return "ok"


def get_credential(conn: sqlite3.Connection, broker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM credential WHERE broker = ?",
        (broker,),
    ).fetchone()


def upsert_credential(
    conn: sqlite3.Connection,
    *,
    broker: str,
    label: str,
    secrets_enc: bytes,
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO credential (broker, label, secrets_enc, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(broker) DO UPDATE SET
            label = excluded.label,
            secrets_enc = excluded.secrets_enc,
            updated_at = excluded.updated_at
        """,
        (broker, label, secrets_enc, now, now),
    )


def delete_credential(conn: sqlite3.Connection, broker: str) -> None:
    conn.execute("DELETE FROM credential WHERE broker = ?", (broker,))
