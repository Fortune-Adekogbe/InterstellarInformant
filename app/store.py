# app/store.py
import os
import sqlite3
from typing import Dict, List, Optional

# Prefer your newer env var, fall back to old if present
DB_PATH = (
    os.getenv("ASTRO_DB")
    or os.getenv("DB_PATH")
    or os.path.join(os.getcwd(), "astro_users.sqlite3")
)

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  -- We keep both names for easy migration; at least one will exist/used.
  user_id INTEGER,
  chat_id INTEGER,
  tz TEXT NOT NULL,
  tad_path TEXT NOT NULL DEFAULT 'usa/detroit',
  lat REAL,
  lon REAL,
  daily_hour INTEGER NOT NULL DEFAULT 17,
  daily_minute INTEGER NOT NULL DEFAULT 0,
  use_llm INTEGER NOT NULL DEFAULT 0
);
"""

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def _columns(con: sqlite3.Connection) -> set:
    rows = con.execute("PRAGMA table_info('users')").fetchall()
    return {r[1] for r in rows}

def _ensure_schema_and_migrate() -> None:
    with _conn() as con:
        con.execute(BASE_SCHEMA)
        cols = _columns(con)

        # Legacy tables from previous bots:
        # - May have only chat_id as PK; add missing columns we use now.
        if "user_id" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN user_id INTEGER")
        if "chat_id" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN chat_id INTEGER")
        if "tz" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN tz TEXT NOT NULL DEFAULT 'UTC'")
        if "tad_path" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN tad_path TEXT NOT NULL DEFAULT 'usa/detroit'")
        if "lat" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN lat REAL")
        if "lon" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN lon REAL")
        if "daily_hour" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN daily_hour INTEGER NOT NULL DEFAULT 17")
        if "daily_minute" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN daily_minute INTEGER NOT NULL DEFAULT 0")
        if "use_llm" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN use_llm INTEGER NOT NULL DEFAULT 0")

        # Backfill user_id if only chat_id existed
        con.execute("UPDATE users SET user_id = COALESCE(user_id, chat_id)")
        # Backfill chat_id if only user_id existed
        con.execute("UPDATE users SET chat_id = COALESCE(chat_id, user_id)")

        # Ensure not-null defaults for schedule columns
        con.execute("UPDATE users SET daily_hour = COALESCE(daily_hour, 17)")
        con.execute("UPDATE users SET daily_minute = COALESCE(daily_minute, 0)")
        con.execute("UPDATE users SET tad_path = COALESCE(tad_path, 'usa/detroit')")
        con.execute("UPDATE users SET tz = COALESCE(tz, 'UTC')")
        con.execute("UPDATE users SET use_llm = COALESCE(use_llm, 0)")

        # Helpful unique index to support upsert by user_id/chat_id
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_user_id_uq ON users(user_id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_chat_id_uq ON users(chat_id)")
        con.commit()

_ensure_schema_and_migrate()

def upsert_user(
    user_id: int,
    tz: str,
    tad_path: str,
    lat: Optional[float],
    lon: Optional[float],
    daily_hour: int,
    daily_minute: int,
    use_llm: int
) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO users(user_id, chat_id, tz, tad_path, lat, lon, daily_hour, daily_minute, use_llm)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                tz=excluded.tz,
                tad_path=excluded.tad_path,
                lat=excluded.lat,
                lon=excluded.lon,
                daily_hour=excluded.daily_hour,
                daily_minute=excluded.daily_minute,
                use_llm=excluded.use_llm
            """,
            (user_id, user_id, tz, tad_path, lat, lon, daily_hour, daily_minute, use_llm),
        )
        con.commit()

def set_use_llm(user_id: int, on: bool) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET use_llm=? WHERE user_id=?", (1 if on else 0, user_id))
        con.commit()

def get_user(user_id: int) -> Dict:
    with _conn() as con:
        row = con.execute(
            "SELECT user_id, tz, tad_path, lat, lon, daily_hour, daily_minute, use_llm FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if not row:
            # Initialize with defaults for this user
            upsert_user(
                user_id=user_id,
                tz=os.getenv("ASTRO_TZ", "America/Detroit"),
                tad_path=os.getenv("ASTRO_TAD_PATH", "usa/detroit"),
                lat=None, lon=None,
                daily_hour=int(os.getenv("ASTRO_DAILY_HOUR", "17")),
                daily_minute=int(os.getenv("ASTRO_DAILY_MIN", "0")),
                use_llm=1 if os.getenv("ASTRO_USE_GEMINI", "0") == "1" else 0,
            )
            return get_user(user_id)
        keys = ["user_id", "tz", "tad_path", "lat", "lon", "daily_hour", "daily_minute", "use_llm"]
        return dict(zip(keys, row))

def get_all_users() -> List[Dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT user_id, tz, tad_path, lat, lon, daily_hour, daily_minute, use_llm FROM users"
        ).fetchall()
        keys = ["user_id", "tz", "tad_path", "lat", "lon", "daily_hour", "daily_minute", "use_llm"]
        return [dict(zip(keys, r)) for r in rows]
