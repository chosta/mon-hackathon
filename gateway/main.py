"""Auth Gateway for Dungeons — FastAPI application."""
import asyncio
import json
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from auth import create_jwt, require_auth, generate_nonce, verify_signature
from moltbook import verify_agent, MoltbookError
from contract import client, nft_client
from database import (
    get_db, close_db,
    create_wallet_binding, get_wallet_binding,
    store_nonce, consume_nonce,
    enqueue_tx, get_tx, get_tx_by_action, count_recent_txs,
    get_agent_stats as db_get_agent_stats,
    award_xp, log_action,
    get_leaderboard as db_get_leaderboard,
    get_agent_full_stats as db_get_agent_full_stats,
    get_agent_history as db_get_agent_history,
)
from tx_worker import tx_worker_loop
from models import (
    VerifyRequest, VerifyResponse,
    NonceResponse, LinkWalletRequest, LinkWalletResponse,
    SimpleLinkRequest, EnterBuildRequest,
    EnterDungeonRequest, SubmitActionRequest, SubmitDMRequest,
    AcceptDMRequest, SessionInfoResponse, ErrorResponse, EpochInfoResponse,
    TxStatusResponse, AgentStatsResponse, HealthResponse,
    LeaderboardEntry, AgentFullStatsResponse, ActionLogEntry,
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown."""
    await get_db()
    logger.info("database_ready")
    worker_task = asyncio.create_task(tx_worker_loop())
    logger.info("tx_worker_started")
    yield
    worker_task.cancel()
    await close_db()
    logger.info("shutdown_complete")


app = FastAPI(
    title="Dungeons Auth Gateway",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ─────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    db_ok = False
    try:
        db = await get_db()
        await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    rpc_ok = client.is_healthy()

    return HealthResponse(
        status="ok" if (db_ok and rpc_ok) else "degraded",
        db=db_ok,
        rpc=rpc_ok,
        runner_address=client.runner_address,
        attribution_model="off-chain: runner wallet submits txs, moltbook_id tracked in DB",
    )


# ─── Auth ───────────────────────────────────────────────

@app.post("/auth/verify", response_model=VerifyResponse)
async def auth_verify(req: VerifyRequest):
    """Verify Moltbook token, return JWT."""
    try:
        profile = await verify_agent(req.token)
    except MoltbookError as e:
        raise HTTPException(status_code=401, detail=str(e))

    agent_id = profile.get("id", "unknown")
    agent_name = profile.get("name", "unknown")
    token = create_jwt(agent_id, agent_name)

    return VerifyResponse(jwt=token, agent_id=agent_id, agent_name=agent_name)


@app.get("/auth/nonce", response_model=NonceResponse)
async def auth_nonce(wallet_address: str = Query(..., pattern=r"^0x[a-fA-F0-9]{40}$")):
    """Generate a nonce for wallet linking."""
    nonce = generate_nonce()
    await store_nonce(wallet_address.lower(), nonce)
    return NonceResponse(
        nonce=nonce,
        message=f"Link wallet to Dungeons Gateway\nNonce: {nonce}",
    )


@app.post("/auth/link", response_model=LinkWalletResponse)
async def auth_link(req: LinkWalletRequest, claims: dict = Depends(require_auth)):
    """Link a wallet address to a Moltbook agent via signed message."""
    wallet = req.wallet_address.lower()
    nonce = await consume_nonce(wallet)
    if not nonce:
        raise HTTPException(status_code=400, detail="No valid nonce found. Request one first.")

    if not verify_signature(wallet, nonce, req.signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    moltbook_id = claims["sub"]
    await create_wallet_binding(moltbook_id, wallet)
    logger.info("wallet_linked", moltbook_id=moltbook_id, wallet=wallet)

    return LinkWalletResponse(success=True, wallet_address=wallet)


@app.post("/auth/link-simple")
async def auth_link_simple(req: SimpleLinkRequest, claims: dict = Depends(require_auth)):
    """Link wallet without signature (trusted via JWT). Auto-registers agent and mints tickets."""
    from web3 import Web3
    moltbook_id = claims["sub"]
    wallet = Web3.to_checksum_address(req.wallet_address)

    # Prevent re-linking (and duplicate ticket minting)
    existing = await get_wallet_binding(moltbook_id)
    if existing:
        raise HTTPException(400, "Wallet already linked. Cannot re-link.")

    await create_wallet_binding(moltbook_id, wallet)

    # Register agent on-chain
    await enqueue_tx(f"register:{wallet}", moltbook_id, "registerAgent", json.dumps([wallet]))

    # Mint 5 tickets to agent directly (DungeonTickets.mint is onlyOwner)
    try:
        tx_hash = await client.send_tickets_mint(wallet, 5)
        logger.info("tickets_minted", wallet=wallet, amount=5, tx_hash=tx_hash)
    except Exception as e:
        logger.warning("tickets_mint_failed", wallet=wallet, error=str(e))

    logger.info("wallet_linked_simple", moltbook_id=moltbook_id, wallet=wallet)
    return {"ok": True, "wallet": wallet, "message": "Registered + 5 tickets minted"}


@app.post("/game/enter-build")
async def game_enter_build(req: EnterBuildRequest, claims: dict = Depends(require_auth)):
    """Build unsigned enterDungeon tx for agent to sign."""
    moltbook_id = claims["sub"]
    binding = await get_wallet_binding(moltbook_id)
    if not binding:
        raise HTTPException(400, "Link wallet first via /auth/link-simple")

    agent_wallet = binding["wallet_address"]

    # Build tx data
    fn = client.contract.functions.enterDungeon(req.dungeon_id)
    tx_data = fn.build_transaction({
        "from": agent_wallet,
        "value": 10_000_000_000_000_000,  # 0.01 MON
        "gas": 500_000,
        "gasPrice": client.w3.eth.gas_price,
        "nonce": client.w3.eth.get_transaction_count(agent_wallet),
        "chainId": settings.chain_id,
    })

    return {
        "tx": {k: hex(v) if isinstance(v, int) else v for k, v in tx_data.items()},
        "message": "Sign this transaction with your wallet private key and broadcast",
    }


# ─── Helpers ─────────────────────────────────────────────

def error_response(code: str, message: str, status: int = 400, **kwargs) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, **kwargs},
    )


async def validate_turn_index(session_id: int, turn_index: int) -> dict | JSONResponse:
    """Validate turn_index matches on-chain state. Returns session_info or error."""
    info = client.get_session_info(session_id)
    if not info:
        return error_response("SESSION_NOT_FOUND", f"Session {session_id} not found", 404)
    if info["state"] != 2:  # Not Active
        return error_response(
            "SESSION_NOT_ACTIVE",
            f"Session is in state {info['state_name']}, not Active",
            409, current_state=info["state"],
        )
    if turn_index != info["current_turn"]:
        return error_response(
            "TURN_MISMATCH",
            f"Expected turn {info['current_turn']}, got {turn_index}",
            409, expected=info["current_turn"], got=turn_index,
        )
    return info


# ─── Game Endpoints ─────────────────────────────────────

async def _check_rate_limit(moltbook_id: str):
    count = await count_recent_txs(moltbook_id)
    if count >= settings.max_tx_per_hour:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10 tx/hour)")


async def _check_idempotency(moltbook_id: str, action_id: str) -> dict | None:
    existing = await get_tx_by_action(moltbook_id, action_id)
    if existing:
        return TxStatusResponse(
            id=existing["id"],
            action_id=existing["action_id"],
            status=existing["status"],
            tx_hash=existing.get("tx_hash"),
            error=existing.get("error"),
        )
    return None


@app.post("/game/enter", response_model=TxStatusResponse)
async def game_enter(req: EnterDungeonRequest, claims: dict = Depends(require_auth)):
    """Enter a dungeon. Queues enterDungeon(dungeonId) tx."""
    moltbook_id = claims["sub"]
    await _check_rate_limit(moltbook_id)

    # Epoch pre-flight check
    epoch_info = client.get_epoch_info()
    if epoch_info and epoch_info["epoch_state_raw"] != 0:  # Not Active
        return error_response(
            "EPOCH_NOT_ACTIVE",
            "Cannot enter dungeon during Grace period. Wait for next epoch.",
            409,
            current_epoch=epoch_info["current_epoch"],
            epoch_state=epoch_info["epoch_state"],
        )

    existing = await _check_idempotency(moltbook_id, req.action_id)
    if existing:
        return existing

    params = json.dumps([req.dungeon_id])
    tx_id = await enqueue_tx(req.action_id, moltbook_id, "enterDungeon", params)
    logger.info("enter_queued", moltbook_id=moltbook_id, dungeon_id=req.dungeon_id, tx_id=tx_id)

    # Log action
    epoch_info_log = epoch_info or {}
    await log_action(req.action_id, 0, moltbook_id, "enter", epoch_info_log.get("current_epoch", 0))

    return TxStatusResponse(id=tx_id, action_id=req.action_id, status="pending")


@app.post("/game/action", response_model=TxStatusResponse)
async def game_action(req: SubmitActionRequest, claims: dict = Depends(require_auth)):
    """Submit a player action. Queues submitAction(sessionId, turnIndex, action) tx."""
    moltbook_id = claims["sub"]
    await _check_rate_limit(moltbook_id)

    existing = await _check_idempotency(moltbook_id, req.action_id)
    if existing:
        return existing

    # Pre-flight validation
    validation = await validate_turn_index(req.session_id, req.turn_index)
    if isinstance(validation, JSONResponse):
        return validation

    # Get player wallet address for the contract call
    binding = await get_wallet_binding(moltbook_id)
    if not binding:
        raise HTTPException(400, "Link wallet first via /auth/link-simple")
    player_address = binding["wallet_address"]

    params = json.dumps([req.session_id, req.turn_index, req.action, player_address])
    tx_id = await enqueue_tx(req.action_id, moltbook_id, "submitAction", params)
    logger.info("action_queued", moltbook_id=moltbook_id, session_id=req.session_id, turn_index=req.turn_index, tx_id=tx_id)

    # Log action (no XP)
    ei = client.get_epoch_info()
    await log_action(req.action_id, req.session_id, moltbook_id, "action", (ei or {}).get("current_epoch", 0))

    return TxStatusResponse(id=tx_id, action_id=req.action_id, status="pending")


@app.post("/game/dm", response_model=TxStatusResponse)
async def game_dm(req: SubmitDMRequest, claims: dict = Depends(require_auth)):
    """Submit a DM response. Queues submitDMResponse(sessionId, turnIndex, narrative, actions) tx."""
    moltbook_id = claims["sub"]
    await _check_rate_limit(moltbook_id)

    existing = await _check_idempotency(moltbook_id, req.action_id)
    if existing:
        return existing

    # Pre-flight validation
    validation = await validate_turn_index(req.session_id, req.turn_index)
    if isinstance(validation, JSONResponse):
        return validation

    # Convert actions to DMAction tuple format for the contract
    # DMActionType: NARRATE=0, REWARD_GOLD=1, REWARD_XP=2, DAMAGE=3, KILL_PLAYER=4, COMPLETE=5, FAIL=6
    # DMAction struct: (uint8 actionType, address target, uint256 value, string narrative)
    dm_actions = []
    for a in req.actions:
        if a.gold_reward > 0:
            dm_actions.append((1, a.target, a.gold_reward, ""))
        if a.xp_reward > 0:
            dm_actions.append((2, a.target, a.xp_reward, ""))
        if a.damage > 0:
            dm_actions.append((3, a.target, a.damage, ""))
        if a.is_killed:
            dm_actions.append((4, a.target, 0, ""))

    # Add COMPLETE or FAIL action
    ZERO_ADDR = "0x0000000000000000000000000000000000000000"
    if req.is_complete:
        dm_actions.append((5, ZERO_ADDR, 0, ""))
    if req.is_failed:
        dm_actions.append((6, ZERO_ADDR, 0, ""))

    # Get DM address from session info for the contract call
    dm_address = validation.get("dm") or validation.get("current_actor")
    params = json.dumps([req.session_id, req.turn_index, req.narrative, dm_actions, dm_address])
    tx_id = await enqueue_tx(req.action_id, moltbook_id, "submitDMResponse", params)
    logger.info("dm_response_queued", moltbook_id=moltbook_id, session_id=req.session_id, turn_index=req.turn_index, tx_id=tx_id)

    # Log action with narrative and actions
    ei = client.get_epoch_info()
    dm_actions_log = json.dumps([{"target": a.target, "gold_reward": a.gold_reward, "xp_reward": a.xp_reward, "damage": a.damage, "is_killed": a.is_killed} for a in req.actions]) if req.actions else None
    await log_action(req.action_id, req.session_id, moltbook_id, "dm_response", (ei or {}).get("current_epoch", 0),
                     action_text=req.narrative, dm_actions_json=dm_actions_log)

    return TxStatusResponse(id=tx_id, action_id=req.action_id, status="pending")


@app.post("/game/accept-dm", response_model=TxStatusResponse)
async def game_accept_dm(req: AcceptDMRequest, claims: dict = Depends(require_auth)):
    """Accept DM role. Queues acceptDM(sessionId, epoch) tx."""
    moltbook_id = claims["sub"]
    await _check_rate_limit(moltbook_id)

    existing = await _check_idempotency(moltbook_id, req.action_id)
    if existing:
        return existing

    # Pre-flight validation
    info = client.get_session_info(req.session_id)
    if not info:
        return error_response("SESSION_NOT_FOUND", f"Session {req.session_id} not found", 404)

    if info["state"] != 1:  # Not WaitingDM
        if info["state"] == 2:  # Already Active
            return error_response("ALREADY_ACCEPTED", "DM already accepted", 200)
        return error_response(
            "SESSION_NOT_WAITING_DM",
            f"Session is in state {info['state_name']}, not WaitingDM",
            409, current_state=info["state"],
        )

    if req.dm_epoch != info["dm_epoch"]:
        return error_response(
            "DM_EPOCH_MISMATCH",
            f"Expected dm_epoch {info['dm_epoch']}, got {req.dm_epoch}",
            409, expected=info["dm_epoch"], got=req.dm_epoch,
        )

    # acceptDM now takes (sessionId, epoch, dmAddress) - runner submits on behalf of dm
    dm_wallet = info["dm"]
    params = json.dumps([req.session_id, req.dm_epoch, dm_wallet])
    tx_id = await enqueue_tx(req.action_id, moltbook_id, "acceptDM", params)
    logger.info("accept_dm_queued", moltbook_id=moltbook_id, session_id=req.session_id, dm_epoch=req.dm_epoch, tx_id=tx_id)

    # Log action + award DM XP
    ei = client.get_epoch_info()
    current_epoch = (ei or {}).get("current_epoch", 0)
    await log_action(req.action_id, req.session_id, moltbook_id, "accept_dm", current_epoch)
    await award_xp(
        idempotency_key=f"dm_hosted:{req.session_id}:{moltbook_id}",
        moltbook_id=moltbook_id,
        session_id=req.session_id,
        epoch_id=current_epoch,
        event_type="dm_hosted",
        xp_amount=75,
        source="accept_dm",
    )

    return TxStatusResponse(id=tx_id, action_id=req.action_id, status="pending")


@app.get("/game/epoch", response_model=EpochInfoResponse)
async def get_epoch():
    """Get current epoch info from on-chain state."""
    info = client.get_epoch_info()
    if not info:
        raise HTTPException(500, "Failed to fetch epoch info")
    return EpochInfoResponse(**info)


@app.get("/game/session/{session_id}", response_model=SessionInfoResponse)
async def get_session(session_id: int):
    """Get session info from on-chain state."""
    info = client.get_session_info(session_id)
    if not info:
        raise HTTPException(404, "Session not found")
    return SessionInfoResponse(**info)


# ─── Transaction Status ────────────────────────────────

@app.get("/tx/{tx_id}", response_model=TxStatusResponse)
async def tx_status(tx_id: int):
    """Get transaction status."""
    tx = await get_tx(tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TxStatusResponse(
        id=tx["id"],
        action_id=tx["action_id"],
        status=tx["status"],
        tx_hash=tx.get("tx_hash"),
        error=tx.get("error"),
    )


# ─── Stats ──────────────────────────────────────────────

@app.get("/stats/agent/{moltbook_id}", response_model=AgentStatsResponse)
async def agent_stats(moltbook_id: str):
    """Get agent stats (off-chain + on-chain)."""
    db_stats = await db_get_agent_stats(moltbook_id)

    # Try to get on-chain stats via wallet binding
    on_chain = {}
    binding = await get_wallet_binding(moltbook_id)
    if binding:
        on_chain = client.get_agent_stats(binding["wallet_address"])

    return AgentStatsResponse(
        moltbook_id=moltbook_id,
        total_xp=db_stats["total_xp"],
        total_gold=db_stats["total_gold"],
        total_events=db_stats["total_events"],
        on_chain=on_chain,
    )


# ─── Leaderboard & Agent Stats ──────────────────────────

@app.get("/leaderboard/table")
async def leaderboard_table(limit: int = 50):
    """Get all agents with XP, gold, and sessions for table view."""
    db = await get_db()
    rows = await db.fetch_all(
        """SELECT moltbook_id, display_name, total_xp, lifetime_gold, lifetime_sessions, current_level
           FROM agent_stats
           ORDER BY total_xp DESC
           LIMIT ?""",
        (min(limit, 100),),
    )
    return [dict(r) for r in rows]


@app.get("/leaderboard/{metric}", response_model=list[LeaderboardEntry])
async def leaderboard(metric: str, limit: int = 20):
    """Get leaderboard by metric (xp, gold, sessions)."""
    if metric not in ("xp", "gold", "sessions"):
        raise HTTPException(400, "Invalid metric. Use: xp, gold, sessions")
    return await db_get_leaderboard(metric, min(limit, 100))


@app.get("/agent/{moltbook_id}/stats", response_model=AgentFullStatsResponse)
async def agent_full_stats(moltbook_id: str):
    """Get full stats for an agent."""
    stats = await db_get_agent_full_stats(moltbook_id)
    if not stats:
        raise HTTPException(404, "Agent not found")
    return AgentFullStatsResponse(**stats)


@app.get("/agent/{moltbook_id}/history", response_model=list[ActionLogEntry])
async def agent_history(moltbook_id: str, limit: int = 50):
    """Get action history for an agent."""
    return await db_get_agent_history(moltbook_id, min(limit, 200))


# ─── Dashboard Endpoints ────────────────────────────────

@app.get("/stats/overview")
async def stats_overview():
    """Overview stats for the dashboard."""
    import time
    db = await get_db()
    now = time.time()

    total_agents = (await db.fetch_one("SELECT COUNT(*) as c FROM agent_stats")) or {"c": 0}
    total_sessions_row = (await db.fetch_one("SELECT SUM(lifetime_sessions) as c FROM agent_stats")) or {"c": 0}
    total_actions = (await db.fetch_one("SELECT COUNT(*) as c FROM action_log")) or {"c": 0}

    # Actions per minute (last 5 min)
    five_min_ago = now - 300
    recent_actions = (await db.fetch_one(
        "SELECT COUNT(*) as c FROM action_log WHERE created_at > ?", (five_min_ago,)
    )) or {"c": 0}
    actions_per_minute = round((recent_actions["c"] or 0) / 5.0, 1)

    # Recent errors from tx_queue
    recent_errors = (await db.fetch_one(
        "SELECT COUNT(*) as c FROM tx_queue WHERE status = 'failed' AND updated_at > ?",
        (now - 3600,)
    )) or {"c": 0}
    last_error_row = await db.fetch_one(
        "SELECT error, updated_at FROM tx_queue WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 1"
    )

    return {
        "total_agents": total_agents["c"] or 0,
        "total_sessions": total_sessions_row["c"] or 0,
        "total_actions": total_actions["c"] or 0,
        "actions_per_minute": actions_per_minute,
        "recent_errors": recent_errors["c"] or 0,
        "last_error": last_error_row["error"] if last_error_row else None,
        "last_error_at": last_error_row["updated_at"] if last_error_row else None,
        "timestamp": now,
    }


@app.get("/activity/recent")
async def activity_recent(minutes: int = 1440, limit: int = 100):
    """Recent actions for the activity feed. Default: last 24 hours."""
    import time
    db = await get_db()
    cutoff = time.time() - (minutes * 60)
    rows = await db.fetch_all(
        """SELECT al.action_id, al.session_id, al.moltbook_id, al.action_type,
                  al.epoch_id, al.created_at, al.action_text, al.dm_actions_json,
                  COALESCE(ag.display_name, al.moltbook_id) as agent_name,
                  tq.status as tx_status, tq.error as tx_error
           FROM action_log al
           LEFT JOIN agent_stats ag ON al.moltbook_id = ag.moltbook_id
           LEFT JOIN tx_queue tq ON al.action_id = tq.action_id
           WHERE al.created_at > ?
           ORDER BY al.created_at DESC LIMIT ?""",
        (cutoff, min(limit, 200)),
    )
    return [dict(r) for r in rows]


# ─── Dungeon Endpoints ──────────────────────────────────

@app.get("/dungeons/overview")
async def dungeons_overview():
    """Get dungeon stats overview from on-chain state."""
    overview = client.get_dungeons_overview()
    return overview


@app.get("/dungeons/list")
async def dungeons_list():
    """Get all dungeons with their traits and session state."""
    dungeons = client.get_all_dungeons()
    result = []
    for d in dungeons:
        # Get NFT traits
        traits = nft_client.get_traits(d["nft_id"]) if d["nft_id"] is not None else None
        
        # Get session state if there's an active session
        session_state = None
        session_info = None
        if d["current_session_id"] and d["current_session_id"] > 0:
            session_info = client.get_session_info(d["current_session_id"])
            if session_info:
                session_state = session_info["state_name"]
        
        result.append({
            "dungeon_id": d["dungeon_id"],
            "nft_id": d["nft_id"],
            "owner": d["owner"],
            "active": d["active"],
            "loot_pool": d["loot_pool"],
            "current_session_id": d["current_session_id"],
            "session_state": session_state,
            "traits": traits,
        })
    return result


@app.get("/sessions/list")
async def sessions_list(limit: int = 50, include_completed: bool = False):
    """Get recent sessions with their info."""
    db = await get_db()
    
    # Get sessions from action_log (grouped by session_id)
    rows = await db.fetch_all(
        """SELECT DISTINCT session_id FROM action_log 
           WHERE session_id > 0 
           ORDER BY session_id DESC LIMIT ?""",
        (limit,),
    )
    
    result = []
    for row in rows:
        session_id = row["session_id"]
        
        # Try contract first, fall back to computing from action_log
        session_info = client.get_session_info(session_id)
        on_chain = session_info and session_info.get("state", 0) > 0
        
        # Compute stats from action_log (authoritative for off-chain sessions)
        stats = await db.fetch_one(
            """SELECT 
                COUNT(*) as action_count,
                COUNT(CASE WHEN action_type='dm_response' THEN 1 END) as turn_count,
                COUNT(CASE WHEN action_type='action' THEN 1 END) as player_actions,
                MIN(created_at) as first_action,
                MAX(created_at) as last_action
               FROM action_log WHERE session_id = ?""",
            (session_id,),
        )
        
        # Compute gold/xp totals from dm_actions_json
        dm_rows = await db.fetch_all(
            "SELECT dm_actions_json FROM action_log WHERE session_id = ? AND dm_actions_json IS NOT NULL",
            (session_id,),
        )
        total_gold = 0
        total_xp = 0
        total_damage = 0
        is_completed = False
        for dr in dm_rows:
            try:
                import json as _json
                actions = _json.loads(dr["dm_actions_json"])
                for a in actions:
                    total_gold += a.get("gold_reward", 0)
                    total_xp += a.get("xp_reward", 0)
                    total_damage += a.get("damage", 0)
                    if a.get("is_killed"):
                        is_completed = True
            except Exception:
                pass
        
        # Check for "SESSION COMPLETE" in action_text
        if not is_completed:
            complete_check = await db.fetch_one(
                "SELECT 1 FROM action_log WHERE session_id = ? AND action_text LIKE '%SESSION COMPLETE%' LIMIT 1",
                (session_id,),
            )
            if complete_check:
                is_completed = True
        
        # Determine state
        if on_chain:
            state = session_info["state"]
            state_name = session_info["state_name"]
        else:
            if is_completed:
                state = 3
                state_name = "Completed"
            elif stats["turn_count"] > 0:
                state = 2
                state_name = "Active"
            else:
                state = 1
                state_name = "Waiting"
        
        # Skip completed if not requested
        if not include_completed and state >= 3:
            continue
        
        # Build session info - prefer computed stats over on-chain zeros
        current_turn = stats["turn_count"] if stats else 0
        if on_chain and session_info.get("current_turn", 0) > current_turn:
            current_turn = session_info["current_turn"]
        
        gold_pool = total_gold
        if on_chain and session_info.get("gold_pool", 0) > gold_pool:
            gold_pool = session_info["gold_pool"]
        
        # Get dungeon traits
        dungeon_id = session_info["dungeon_id"] if session_info else 0
        dungeon_info = client.get_dungeon_info(dungeon_id)
        traits = None
        if dungeon_info:
            traits = nft_client.get_traits(dungeon_info["nft_id"])
        
        info_base = session_info if session_info else {}
        result.append({
            **info_base,
            "session_id": session_id,
            "state": state,
            "state_name": state_name,
            "current_turn": current_turn,
            "gold_pool": gold_pool,
            "total_xp": total_xp,
            "total_damage": total_damage,
            "action_count": stats["action_count"] if stats else 0,
            "player_actions": stats["player_actions"] if stats else 0,
            "dungeon_traits": traits,
        })
    
    return result


# ─── Internal Endpoints (for devenv scenario runner) ────

from pydantic import BaseModel as PydanticBaseModel


class InternalLogActionRequest(PydanticBaseModel):
    action_id: str
    session_id: int = 0
    moltbook_id: str
    action_type: str
    action_text: str | None = None
    dm_actions_json: str | None = None


class InternalAwardXPRequest(PydanticBaseModel):
    idempotency_key: str
    moltbook_id: str
    session_id: int = 0
    event_type: str = "action"
    xp_amount: int = 0
    gold_amount: int = 0


@app.post("/internal/log-action")
async def internal_log_action(req: InternalLogActionRequest, claims: dict = Depends(require_auth)):
    """Log an action from the scenario runner (stats tracking only, no tx)."""
    # Ensure agent_stats row exists
    db = await get_db()
    import time as _time
    await db.execute(
        """INSERT INTO agent_stats (moltbook_id, display_name, total_xp, created_at)
           VALUES (?, ?, 0, ?)
           ON CONFLICT(moltbook_id) DO NOTHING""",
        (req.moltbook_id, claims.get("name", req.moltbook_id), _time.time()),
    )
    await db.commit()

    ei = client.get_epoch_info()
    epoch = (ei or {}).get("current_epoch", 0)
    await log_action(req.action_id, req.session_id, req.moltbook_id, req.action_type, epoch,
                     action_text=req.action_text, dm_actions_json=req.dm_actions_json)
    return {"ok": True}


@app.post("/internal/award-xp")
async def internal_award_xp(req: InternalAwardXPRequest, claims: dict = Depends(require_auth)):
    """Award XP/gold from the scenario runner."""
    ei = client.get_epoch_info()
    epoch = (ei or {}).get("current_epoch", 0)
    ok = await award_xp(
        idempotency_key=req.idempotency_key,
        moltbook_id=req.moltbook_id,
        session_id=req.session_id,
        epoch_id=epoch,
        event_type=req.event_type,
        xp_amount=req.xp_amount,
        gold_amount=req.gold_amount,
        source="scenario_runner",
    )
    return {"ok": ok}


# ─── Playthrough ────────────────────────────────────────

@app.get("/session/{session_id}/playthrough")
async def session_playthrough(session_id: int):
    """Get full narrative playthrough for a session."""
    db = await get_db()
    rows = await db.fetch_all(
        """SELECT al.action_id, al.moltbook_id, al.action_type, al.action_text, al.dm_actions_json, al.created_at,
                  COALESCE(ag.display_name, al.moltbook_id) as agent_name
           FROM action_log al
           LEFT JOIN agent_stats ag ON al.moltbook_id = ag.moltbook_id
           WHERE al.session_id = ?
           ORDER BY al.created_at ASC""",
        (session_id,),
    )
    entries = []
    for r in rows:
        entry = {
            "action_type": r["action_type"],
            "agent_name": r["agent_name"],
            "moltbook_id": r["moltbook_id"],
            "action_text": r["action_text"],
            "timestamp": r["created_at"],
        }
        if r["dm_actions_json"]:
            try:
                entry["dm_actions"] = json.loads(r["dm_actions_json"])
            except (json.JSONDecodeError, TypeError):
                entry["dm_actions"] = r["dm_actions_json"]
        entries.append(entry)
    return {"session_id": session_id, "entries": entries}


# ─── Static Dashboard ──────────────────────────────────

from pathlib import Path
from fastapi.staticfiles import StaticFiles

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/dashboard", StaticFiles(directory=str(static_dir), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


# ─── Admin Endpoints (dev only) ──────────────────────────

@app.post("/admin/register-runner")
async def admin_register_runner():
    """Register the runner wallet as an agent."""
    try:
        # Check if already registered
        is_registered = client.contract.functions.registeredAgents(client.runner_address).call()
        if is_registered:
            return {"ok": True, "message": "Runner already registered", "address": client.runner_address}
        
        # Register
        tx_hash = await client.send_tx("registerAgent", client.runner_address)
        return {"ok": True, "tx_hash": tx_hash, "address": client.runner_address}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/mint-runner-tickets")
async def admin_mint_runner_tickets():
    """Mint tickets to the runner wallet."""
    from config import settings
    from web3 import Web3
    try:
        # Load tickets contract
        tickets_abi_path = os.path.join(os.path.dirname(__file__), "abi", "DungeonTickets.json")
        with open(tickets_abi_path) as f:
            tickets_abi = json.load(f)["abi"]
        
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        tickets = w3.eth.contract(
            address=Web3.to_checksum_address(settings.dungeon_tickets),
            abi=tickets_abi,
        )
        
        # Build mint tx
        fn = tickets.functions.mint(client.runner_address, 5)
        nonce = w3.eth.get_transaction_count(client.runner_address, "pending")
        
        tx = fn.build_transaction({
            "from": client.runner_address,
            "nonce": nonce,
            "chainId": settings.chain_id,
            "gas": 200_000,
            "gasPrice": int(w3.eth.gas_price * 1.5),
        })
        
        signed = w3.eth.account.sign_transaction(tx, settings.runner_private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        
        return {"ok": True, "tx_hash": tx_hash.hex(), "to": client.runner_address, "amount": 5}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/unlink-wallet")
async def admin_unlink_wallet(moltbook_id: str = Query(...)):
    """Admin: Remove wallet binding for an agent (allows re-linking)."""
    db = await get_db()
    result = await db.execute("DELETE FROM wallet_bindings WHERE moltbook_id = ?", (moltbook_id,))
    await db.commit()
    if result.rowcount == 0:
        return {"ok": False, "message": f"No wallet binding found for {moltbook_id}"}
    logger.info("wallet_unlinked", moltbook_id=moltbook_id)
    return {"ok": True, "message": f"Unlinked wallet for {moltbook_id}"}
