"""Database layer with pluggable backend (SQLite default, PostgreSQL optional)."""
import time
from config import settings
from db_backend import DatabaseBackend, SQLiteBackend, PostgresBackend, create_backend, get_schema

_backend: DatabaseBackend | None = None
_backend_type: str = "sqlite"


async def _migrate_db(backend: DatabaseBackend):
    """Handle schema migrations for existing databases (SQLite only)."""
    if not isinstance(backend, SQLiteBackend):
        return
    rows = await backend.fetch_all("PRAGMA table_info(xp_events)")
    cols = {r["name"] for r in rows} if rows else set()
    if cols and "idempotency_key" not in cols:
        await backend.execute("DROP TABLE IF EXISTS xp_events")
        await backend.commit()
    # Migrate action_log: add action_text and dm_actions_json columns
    al_rows = await backend.fetch_all("PRAGMA table_info(action_log)")
    al_cols = {r["name"] for r in al_rows} if al_rows else set()
    if al_cols and "action_text" not in al_cols:
        await backend.execute("ALTER TABLE action_log ADD COLUMN action_text TEXT")
        await backend.execute("ALTER TABLE action_log ADD COLUMN dm_actions_json TEXT")
        await backend.commit()


async def get_db() -> DatabaseBackend:
    global _backend, _backend_type
    if _backend is None:
        url = getattr(settings, "database_url", None) or f"sqlite:///{settings.db_path}"
        _backend = create_backend(url)
        _backend_type = "postgres" if isinstance(_backend, PostgresBackend) else "sqlite"
        await _backend.connect()
        await _migrate_db(_backend)
        schema = get_schema(_backend_type)
        await _backend.executescript(schema)
        if _backend_type == "sqlite":
            await _backend.commit()
    return _backend


async def close_db():
    global _backend
    if _backend:
        await _backend.close()
        _backend = None


# --- Wallet Bindings ---

async def get_wallet_binding(moltbook_id: str) -> dict | None:
    db = await get_db()
    return await db.fetch_one(
        "SELECT * FROM wallet_bindings WHERE moltbook_id = ?", (moltbook_id,)
    )


async def create_wallet_binding(moltbook_id: str, wallet_address: str):
    db = await get_db()
    await db.execute(
        "INSERT INTO wallet_bindings (moltbook_id, wallet_address, linked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(moltbook_id) DO UPDATE SET wallet_address = EXCLUDED.wallet_address, linked_at = EXCLUDED.linked_at",
        (moltbook_id, wallet_address, time.time()),
    )
    await db.commit()


# --- Nonces ---

async def store_nonce(wallet_address: str, nonce: str, ttl: int = 300):
    db = await get_db()
    now = time.time()
    await db.execute(
        "INSERT INTO nonces (wallet_address, nonce, created_at, expires_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(wallet_address) DO UPDATE SET nonce = EXCLUDED.nonce, created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at",
        (wallet_address, nonce, now, now + ttl),
    )
    await db.commit()


async def consume_nonce(wallet_address: str) -> str | None:
    db = await get_db()
    row = await db.fetch_one(
        "SELECT nonce FROM nonces WHERE wallet_address = ? AND expires_at > ?",
        (wallet_address, time.time()),
    )
    if not row:
        return None
    nonce = row["nonce"]
    await db.execute("DELETE FROM nonces WHERE wallet_address = ?", (wallet_address,))
    await db.commit()
    return nonce


# --- TX Queue ---

async def enqueue_tx(action_id: str, moltbook_id: str, method: str, params: str) -> int:
    db = await get_db()
    now = time.time()
    if isinstance(db, PostgresBackend):
        row = await db.fetch_one(
            "INSERT INTO tx_queue (action_id, moltbook_id, method, params, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?) RETURNING id",
            (action_id, moltbook_id, method, params, now, now),
        )
        await db.commit()
        return row["id"]
    else:
        await db.execute(
            "INSERT INTO tx_queue (action_id, moltbook_id, method, params, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (action_id, moltbook_id, method, params, now, now),
        )
        await db.commit()
        return await db.lastrowid()


async def get_tx(tx_id: int) -> dict | None:
    db = await get_db()
    return await db.fetch_one("SELECT * FROM tx_queue WHERE id = ?", (tx_id,))


async def get_tx_by_action(moltbook_id: str, action_id: str) -> dict | None:
    db = await get_db()
    return await db.fetch_one(
        "SELECT * FROM tx_queue WHERE moltbook_id = ? AND action_id = ?",
        (moltbook_id, action_id),
    )


async def get_pending_txs(limit: int = 10) -> list[dict]:
    db = await get_db()
    return await db.fetch_all(
        "SELECT * FROM tx_queue WHERE status = 'pending' ORDER BY created_at LIMIT ?",
        (limit,),
    )


async def update_tx_status(tx_id: int, status: str, tx_hash: str = None, error: str = None):
    db = await get_db()
    await db.execute(
        "UPDATE tx_queue SET status = ?, tx_hash = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, tx_hash, error, time.time(), tx_id),
    )
    await db.commit()


async def count_recent_txs(moltbook_id: str, window_seconds: int = 3600) -> int:
    db = await get_db()
    row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM tx_queue WHERE moltbook_id = ? AND created_at > ?",
        (moltbook_id, time.time() - window_seconds),
    )
    return row["cnt"] if row else 0


# --- XP Events ---

async def award_xp(
    idempotency_key: str,
    moltbook_id: str,
    session_id: int,
    epoch_id: int,
    event_type: str,
    xp_amount: int,
    gold_amount: int = 0,
    source: str = "system",
    metadata: str = None,
) -> bool:
    """Award XP with idempotency. Returns False if duplicate key."""
    db = await get_db()
    try:
        await db.begin()
        await db.execute(
            """INSERT INTO xp_events (idempotency_key, moltbook_id, session_id, epoch_id,
                                      event_type, xp_amount, gold_amount, source, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (idempotency_key, moltbook_id, session_id, epoch_id,
             event_type, xp_amount, gold_amount, source, metadata, time.time()),
        )
        await db.execute(
            """INSERT INTO agent_stats (moltbook_id, total_xp, lifetime_gold, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(moltbook_id) DO UPDATE SET
                   total_xp = agent_stats.total_xp + EXCLUDED.total_xp,
                   lifetime_gold = agent_stats.lifetime_gold + EXCLUDED.lifetime_gold,
                   current_level = CASE
                       WHEN agent_stats.total_xp + EXCLUDED.total_xp >= 10000 THEN 'legend'
                       WHEN agent_stats.total_xp + EXCLUDED.total_xp >= 2000 THEN 'veteran'
                       WHEN agent_stats.total_xp + EXCLUDED.total_xp >= 500 THEN 'adventurer'
                       ELSE 'novice'
                   END""",
            (moltbook_id, xp_amount, gold_amount, time.time()),
        )
        if event_type == "dm_hosted":
            await db.execute(
                "UPDATE agent_stats SET dm_sessions = dm_sessions + 1 WHERE moltbook_id = ?",
                (moltbook_id,),
            )
        elif event_type == "session_complete":
            await db.execute(
                "UPDATE agent_stats SET lifetime_sessions = lifetime_sessions + 1, last_session_at = ? WHERE moltbook_id = ?",
                (time.time(), moltbook_id),
            )
        elif event_type == "win":
            await db.execute(
                "UPDATE agent_stats SET lifetime_wins = lifetime_wins + 1 WHERE moltbook_id = ?",
                (moltbook_id,),
            )
        await db.commit()
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "unique" in err_str or "integrity" in err_str or "duplicate" in err_str:
            await db.rollback()
            return False
        await db.rollback()
        raise


async def log_action(action_id: str, session_id: int, moltbook_id: str,
                     action_type: str, epoch_id: int,
                     action_text: str = None, dm_actions_json: str = None) -> bool:
    """Log action with idempotency. Returns False if duplicate."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO action_log (action_id, session_id, moltbook_id, action_type, epoch_id, action_text, dm_actions_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_id, session_id, moltbook_id, action_type, epoch_id, action_text, dm_actions_json, time.time()),
        )
        await db.commit()
        return True
    except Exception as e:
        if "unique" in str(e).lower() or "integrity" in str(e).lower() or "duplicate" in str(e).lower():
            return False
        raise


async def get_leaderboard(metric: str = "xp", limit: int = 20) -> list[dict]:
    db = await get_db()
    col_map = {"xp": "total_xp", "gold": "lifetime_gold", "sessions": "lifetime_sessions"}
    col = col_map.get(metric, "total_xp")
    rows = await db.fetch_all(
        f"SELECT moltbook_id, display_name, {col} as value, current_level FROM agent_stats ORDER BY {col} DESC LIMIT ?",
        (limit,),
    )
    return [
        {"rank": i + 1, "moltbook_id": r["moltbook_id"], "display_name": r["display_name"],
         "value": r["value"], "level": r["current_level"]}
        for i, r in enumerate(rows)
    ]


async def get_agent_full_stats(moltbook_id: str) -> dict | None:
    db = await get_db()
    return await db.fetch_one(
        "SELECT * FROM agent_stats WHERE moltbook_id = ?", (moltbook_id,),
    )


async def get_agent_history(moltbook_id: str, limit: int = 50) -> list[dict]:
    db = await get_db()
    return await db.fetch_all(
        "SELECT * FROM action_log WHERE moltbook_id = ? ORDER BY created_at DESC LIMIT ?",
        (moltbook_id, limit),
    )


async def rebuild_agent_stats(moltbook_id: str):
    """Recompute agent_stats from xp_events (drift fix)."""
    db = await get_db()
    rows = await db.fetch_all(
        """SELECT COALESCE(SUM(xp_amount), 0) as total_xp,
                  COALESCE(SUM(gold_amount), 0) as total_gold,
                  SUM(CASE WHEN event_type = 'session_complete' THEN 1 ELSE 0 END) as sessions,
                  SUM(CASE WHEN event_type = 'win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN event_type = 'dm_hosted' THEN 1 ELSE 0 END) as dm_sessions
           FROM xp_events WHERE moltbook_id = ?""",
        (moltbook_id,),
    )
    if not rows:
        return
    r = rows[0]
    total_xp = r["total_xp"]
    level = "novice"
    if total_xp >= 10000:
        level = "legend"
    elif total_xp >= 2000:
        level = "veteran"
    elif total_xp >= 500:
        level = "adventurer"
    await db.execute(
        """INSERT INTO agent_stats (moltbook_id, total_xp, lifetime_gold, lifetime_sessions,
                                    lifetime_wins, dm_sessions, current_level, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(moltbook_id) DO UPDATE SET
               total_xp = EXCLUDED.total_xp, lifetime_gold = EXCLUDED.lifetime_gold,
               lifetime_sessions = EXCLUDED.lifetime_sessions, lifetime_wins = EXCLUDED.lifetime_wins,
               dm_sessions = EXCLUDED.dm_sessions, current_level = EXCLUDED.current_level""",
        (moltbook_id, total_xp, r["total_gold"], r["sessions"], r["wins"], r["dm_sessions"], level, time.time()),
    )
    await db.commit()


# Legacy compat
async def get_agent_stats(moltbook_id: str) -> dict:
    stats = await get_agent_full_stats(moltbook_id)
    if stats:
        return {"total_xp": stats["total_xp"], "total_gold": stats["lifetime_gold"], "total_events": 0}
    return {"total_xp": 0, "total_gold": 0, "total_events": 0}
