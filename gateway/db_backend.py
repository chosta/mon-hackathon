"""Database backend abstraction: SQLite (default) and PostgreSQL."""
import re
from abc import ABC, abstractmethod
from typing import Any


def _q(sql: str) -> str:
    """Convert ? placeholders to $1, $2, ... for asyncpg."""
    counter = 0
    def _replace(m):
        nonlocal counter
        counter += 1
        return f"${counter}"
    return re.sub(r"\?", _replace, sql)


class DatabaseBackend(ABC):
    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def close(self): ...
    @abstractmethod
    async def execute(self, sql: str, params: tuple = ()) -> Any: ...
    @abstractmethod
    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]: ...
    @abstractmethod
    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None: ...
    @abstractmethod
    async def executescript(self, sql: str): ...
    @abstractmethod
    async def begin(self): ...
    @abstractmethod
    async def commit(self): ...
    @abstractmethod
    async def rollback(self): ...
    @abstractmethod
    async def lastrowid(self) -> int | None: ...


class SQLiteBackend(DatabaseBackend):
    def __init__(self, db_path: str):
        self._path = db_path
        self._db = None
        self._last_rowid = None

    async def connect(self):
        import aiosqlite
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def execute(self, sql: str, params: tuple = ()):
        cursor = await self._db.execute(sql, params)
        self._last_rowid = cursor.lastrowid
        return cursor

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        rows = await self._db.execute_fetchall(sql, params)
        return [dict(r) for r in rows] if rows else []

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self._db.execute_fetchall(sql, params)
        return dict(rows[0]) if rows else None

    async def executescript(self, sql: str):
        await self._db.executescript(sql)

    async def begin(self):
        await self._db.execute("BEGIN")

    async def commit(self):
        await self._db.commit()

    async def rollback(self):
        await self._db.rollback()

    async def lastrowid(self) -> int | None:
        return self._last_rowid


class PostgresBackend(DatabaseBackend):
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None
        self._conn = None  # for transaction support
        self._last_rowid = None

    async def connect(self):
        import asyncpg
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
        if self._pool:
            await self._pool.close()
            self._pool = None

    def _target(self):
        """Return current connection (transaction) or pool."""
        return self._conn if self._conn else self._pool

    async def execute(self, sql: str, params: tuple = ()):
        q = _q(sql)
        result = await self._target().execute(q, *params)
        # Try to extract lastrowid from RETURNING or INSERT
        self._last_rowid = None
        return result

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        q = _q(sql)
        rows = await self._target().fetch(q, *params)
        return [dict(r) for r in rows] if rows else []

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        q = _q(sql)
        row = await self._target().fetchrow(q, *params)
        return dict(row) if row else None

    async def executescript(self, sql: str):
        """Execute a multi-statement SQL script."""
        await self._target().execute(sql)

    async def begin(self):
        self._conn = await self._pool.acquire()
        self._tr = self._conn.transaction()
        await self._tr.start()

    async def commit(self):
        if self._conn:
            await self._tr.commit()
            await self._pool.release(self._conn)
            self._conn = None

    async def rollback(self):
        if self._conn:
            await self._tr.rollback()
            await self._pool.release(self._conn)
            self._conn = None

    async def lastrowid(self) -> int | None:
        return self._last_rowid


# --- Schema adaptation ---

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_bindings (
    moltbook_id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    linked_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS nonces (
    wallet_address TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    moltbook_id TEXT NOT NULL,
    method TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tx_hash TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(moltbook_id, action_id)
);

CREATE TABLE IF NOT EXISTS xp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    moltbook_id TEXT NOT NULL,
    session_id INTEGER,
    epoch_id INTEGER,
    event_type TEXT NOT NULL,
    xp_amount INTEGER DEFAULT 0,
    gold_amount INTEGER DEFAULT 0,
    source TEXT,
    metadata TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xp_events_agent ON xp_events(moltbook_id);
CREATE INDEX IF NOT EXISTS idx_xp_events_epoch ON xp_events(epoch_id);

CREATE TABLE IF NOT EXISTS agent_stats (
    moltbook_id TEXT PRIMARY KEY,
    display_name TEXT,
    total_xp INTEGER DEFAULT 0,
    current_level TEXT DEFAULT 'novice',
    lifetime_sessions INTEGER DEFAULT 0,
    lifetime_wins INTEGER DEFAULT 0,
    lifetime_gold INTEGER DEFAULT 0,
    dm_sessions INTEGER DEFAULT 0,
    last_session_at REAL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_stats_xp ON agent_stats(total_xp DESC);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT UNIQUE NOT NULL,
    session_id INTEGER,
    moltbook_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    epoch_id INTEGER,
    action_text TEXT,
    dm_actions_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_log_agent ON action_log(moltbook_id, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    moltbook_id TEXT PRIMARY KEY,
    jwt_token TEXT NOT NULL,
    agent_name TEXT,
    agent_id TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_bindings (
    moltbook_id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    linked_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS nonces (
    wallet_address TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_queue (
    id SERIAL PRIMARY KEY,
    action_id TEXT NOT NULL,
    moltbook_id TEXT NOT NULL,
    method TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tx_hash TEXT,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(moltbook_id, action_id)
);

CREATE TABLE IF NOT EXISTS xp_events (
    id SERIAL PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    moltbook_id TEXT NOT NULL,
    session_id INTEGER,
    epoch_id INTEGER,
    event_type TEXT NOT NULL,
    xp_amount INTEGER DEFAULT 0,
    gold_amount INTEGER DEFAULT 0,
    source TEXT,
    metadata TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xp_events_agent ON xp_events(moltbook_id);
CREATE INDEX IF NOT EXISTS idx_xp_events_epoch ON xp_events(epoch_id);

CREATE TABLE IF NOT EXISTS agent_stats (
    moltbook_id TEXT PRIMARY KEY,
    display_name TEXT,
    total_xp INTEGER DEFAULT 0,
    current_level TEXT DEFAULT 'novice',
    lifetime_sessions INTEGER DEFAULT 0,
    lifetime_wins INTEGER DEFAULT 0,
    lifetime_gold INTEGER DEFAULT 0,
    dm_sessions INTEGER DEFAULT 0,
    last_session_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_agent_stats_xp ON agent_stats(total_xp DESC);

CREATE TABLE IF NOT EXISTS action_log (
    id SERIAL PRIMARY KEY,
    action_id TEXT UNIQUE NOT NULL,
    session_id INTEGER,
    moltbook_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    epoch_id INTEGER,
    action_text TEXT,
    dm_actions_json TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_log_agent ON action_log(moltbook_id, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    moltbook_id TEXT PRIMARY KEY,
    jwt_token TEXT NOT NULL,
    agent_name TEXT,
    agent_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
"""


def get_schema(backend_type: str) -> str:
    return POSTGRES_SCHEMA if backend_type == "postgres" else SQLITE_SCHEMA


def create_backend(database_url: str) -> DatabaseBackend:
    """Factory: create the right backend from a URL."""
    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return PostgresBackend(database_url)
    else:
        # SQLite: strip sqlite:/// prefix if present
        path = database_url
        if path.startswith("sqlite:///"):
            path = path[len("sqlite:///"):]
        elif path.startswith("sqlite://"):
            path = path[len("sqlite://"):]
        return SQLiteBackend(path or "gateway.db")
