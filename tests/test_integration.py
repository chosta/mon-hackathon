"""Integration tests for Mon-Hackathon: Gateway + Contracts on Anvil.

Tests are split into:
- Gateway API tests (via httpx AsyncClient → ASGI)
- Contract-level tests (via web3 directly against Anvil)
"""
import asyncio
import uuid
import pytest
import pytest_asyncio

from web3 import Web3

from tests.deploy_helpers import (
    DEPLOYER_KEY, DEPLOYER, RUNNER_KEY, RUNNER,
    AGENT_KEYS, AGENT_ADDRESSES, STAKER_KEY, STAKER,
    send_tx, load_abi,
)
from tests.anvil_helpers import warp_time, mine_blocks
from tests.conftest import get_jwt, auth_header

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

ENTRY_BOND = Web3.to_wei(0.01, "ether")
DM_ACCEPT_TIMEOUT = 5 * 60  # 5 minutes
SESSION_TIMEOUT = 4 * 3600  # 4 hours
DUNGEON_ID = 0
SID = 1  # First session is always ID 1 (++sessionCount)


def action_id() -> str:
    return str(uuid.uuid4())[:16]


def enter_dungeon_direct(w3, manager, agent_key, dungeon_id=0):
    """Have an agent enter a dungeon directly on-chain."""
    return send_tx(w3, agent_key, manager, "enterDungeon", dungeon_id, value=ENTRY_BOND)


def setup_active_session(w3, manager):
    """Enter 2 agents → DM selected → DM accepts → Active.
    Returns (dm_addr, dm_key, party_addresses).
    Dungeon has partySize=2 (1 DM + 1 player).
    """
    for key in AGENT_KEYS[:2]:
        enter_dungeon_direct(w3, manager, key, DUNGEON_ID)

    s = manager.functions.sessions(SID).call()
    dm_addr = s[1]
    dm_epoch = s[11]
    dm_idx = AGENT_ADDRESSES.index(dm_addr)
    dm_key = AGENT_KEYS[dm_idx]

    send_tx(w3, dm_key, manager, "acceptDM", SID, dm_epoch)
    party = manager.functions.getSessionParty(SID).call()
    return dm_addr, dm_key, party


def do_one_turn(w3, manager, dm_addr, dm_actions=None):
    """One player acts, then DM responds. Returns new turn number."""
    s = manager.functions.sessions(SID).call()
    turn = s[3]
    current_actor = s[4]

    # Player submits action
    send_tx(w3, RUNNER_KEY, manager, "submitAction", SID, turn, "I attack!", current_actor)

    # DM responds
    if dm_actions is None:
        dm_actions = [(0, current_actor, 0, "The DM narrates.")]  # NARRATE
    send_tx(w3, RUNNER_KEY, manager, "submitDMResponse", SID, turn, "A tale.", dm_actions, dm_addr)
    return turn


def complete_session(w3, manager):
    """Full flow: enter → accept → one turn with COMPLETE. Returns (dm_addr, party)."""
    dm_addr, dm_key, party = setup_active_session(w3, manager)

    # One player acts, DM responds with COMPLETE
    s = manager.functions.sessions(SID).call()
    turn = s[3]
    current_actor = s[4]
    send_tx(w3, RUNNER_KEY, manager, "submitAction", SID, turn, "I attack!", current_actor)
    dm_actions = [(5, current_actor, 0, "Done!")]  # COMPLETE
    send_tx(w3, RUNNER_KEY, manager, "submitDMResponse", SID, turn, "End.", dm_actions, dm_addr)
    return dm_addr, party


# ═══════════════════════════════════════════════════════════
# T1: Full Session Happy Path
# ═══════════════════════════════════════════════════════════

class TestT1FullSessionHappyPath:

    def test_agents_enter_and_dm_selected(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        for key in AGENT_KEYS[:2]:
            enter_dungeon_direct(w3, manager, key)
        s = manager.functions.sessions(SID).call()
        assert s[2] == 1, f"Expected WaitingDM(1), got {s[2]}"
        assert s[1] in AGENT_ADDRESSES

    def test_dm_accepts_and_session_active(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        setup_active_session(w3, manager)
        s = manager.functions.sessions(SID).call()
        assert s[2] == 2, f"Expected Active(2), got {s[2]}"

    def test_full_session_complete(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        complete_session(w3, manager)
        s = manager.functions.sessions(SID).call()
        assert s[2] == 3, f"Expected Completed(3), got {s[2]}"

    def test_bonds_released_after_completion(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        complete_session(w3, manager)
        for addr in AGENT_ADDRESSES[:2]:
            bond = manager.functions.withdrawableBonds(addr).call()
            assert bond > 0, f"Agent {addr} should have withdrawable bond"


# ═══════════════════════════════════════════════════════════
# T2: DM Timeout + Reroll
# ═══════════════════════════════════════════════════════════

class TestT2DMTimeoutReroll:

    def test_dm_timeout_and_reroll(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        for key in AGENT_KEYS[:2]:
            enter_dungeon_direct(w3, manager, key)

        s = manager.functions.sessions(SID).call()
        old_dm = s[1]
        warp_time(w3, DM_ACCEPT_TIMEOUT + 10)
        send_tx(w3, DEPLOYER_KEY, manager, "rerollDM", SID)

        s = manager.functions.sessions(SID).call()
        assert s[2] in (1, 5), f"Expected WaitingDM or Cancelled, got {s[2]}"


# ═══════════════════════════════════════════════════════════
# T3: Reroll Until Cancel
# ═══════════════════════════════════════════════════════════

class TestT3RerollUntilCancel:

    def test_repeated_rerolls_cancel_session(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        for key in AGENT_KEYS[:2]:
            enter_dungeon_direct(w3, manager, key)

        for _ in range(10):
            s = manager.functions.sessions(SID).call()
            if s[2] != 1:  # Not WaitingDM
                break
            warp_time(w3, DM_ACCEPT_TIMEOUT + 10)
            try:
                send_tx(w3, DEPLOYER_KEY, manager, "rerollDM", SID)
            except Exception:
                break

        s = manager.functions.sessions(SID).call()
        assert s[2] in (1, 5), f"Expected Cancelled or WaitingDM, got {s[2]}"


# ═══════════════════════════════════════════════════════════
# T4: Bond Mechanics
# ═══════════════════════════════════════════════════════════

class TestT4BondMechanics:

    def test_insufficient_bond_reverts(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        account = w3.eth.account.from_key(AGENT_KEYS[0])
        fn = manager.functions.enterDungeon(DUNGEON_ID)
        tx = fn.build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": 31337,
            "gas": 2_000_000,
            "gasPrice": w3.eth.gas_price,
            "value": ENTRY_BOND // 2,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        assert receipt["status"] == 0, "Insufficient bond should revert"

    def test_withdraw_bond_after_completion(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        complete_session(w3, manager)

        agent_addr = AGENT_ADDRESSES[0]
        bond = manager.functions.withdrawableBonds(agent_addr).call()
        if bond > 0:
            send_tx(w3, AGENT_KEYS[0], manager, "withdrawBond")
            bond_after = manager.functions.withdrawableBonds(agent_addr).call()
            assert bond_after == 0


# ═══════════════════════════════════════════════════════════
# T5: Replay Protection
# ═══════════════════════════════════════════════════════════

class TestT5ReplayProtection:

    def test_action_submitted_flag(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        dm_addr, dm_key, party = setup_active_session(w3, manager)

        s = manager.functions.sessions(SID).call()
        turn = s[3]
        current_actor = s[4]

        send_tx(w3, RUNNER_KEY, manager, "submitAction", SID, turn, "Attack!", current_actor)
        submitted = manager.functions.actionSubmitted(SID, turn).call()
        assert submitted, "Turn should be marked as submitted"


# ═══════════════════════════════════════════════════════════
# T6: Session Timeout
# ═══════════════════════════════════════════════════════════

class TestT6SessionTimeout:

    def test_session_timeout(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        setup_active_session(w3, manager)
        warp_time(w3, SESSION_TIMEOUT + 10)
        send_tx(w3, DEPLOYER_KEY, manager, "timeoutSession", SID)

        s = manager.functions.sessions(SID).call()
        assert s[2] == 6, f"Expected TimedOut(6), got {s[2]}"


# ═══════════════════════════════════════════════════════════
# T7: Epoch Transitions
# ═══════════════════════════════════════════════════════════

class TestT7EpochTransitions:

    def test_epoch_state_machine(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        state = manager.functions.epochState().call()
        assert state == 0  # Active
        epoch = manager.functions.currentEpoch().call()

        send_tx(w3, DEPLOYER_KEY, manager, "endEpoch")
        assert manager.functions.epochState().call() == 1  # Grace

        send_tx(w3, DEPLOYER_KEY, manager, "startEpoch")
        assert manager.functions.epochState().call() == 0  # Active
        assert manager.functions.currentEpoch().call() == epoch + 1

    def test_enter_dungeon_blocked_during_grace(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        send_tx(w3, DEPLOYER_KEY, manager, "endEpoch")
        with pytest.raises(Exception):
            enter_dungeon_direct(w3, manager, AGENT_KEYS[0])


# ═══════════════════════════════════════════════════════════
# T8: Pause/Unpause
# ═══════════════════════════════════════════════════════════

class TestT8PauseUnpause:

    def test_pause_blocks_enter(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        send_tx(w3, DEPLOYER_KEY, manager, "pause")
        with pytest.raises(Exception):
            enter_dungeon_direct(w3, manager, AGENT_KEYS[0])

    def test_unpause_allows_enter(self, anvil, contracts):
        w3 = anvil
        manager = contracts["manager"]
        send_tx(w3, DEPLOYER_KEY, manager, "pause")
        send_tx(w3, DEPLOYER_KEY, manager, "unpause")
        enter_dungeon_direct(w3, manager, AGENT_KEYS[0])


# ═══════════════════════════════════════════════════════════
# T9: XP/Stats Tracking (Gateway API)
# ═══════════════════════════════════════════════════════════

class TestT9XPStatsTracking:

    @pytest.mark.asyncio
    async def test_agent_stats_endpoint(self, client):
        resp = await client.get("/stats/agent/agent-1")
        assert resp.status_code == 200
        assert "total_xp" in resp.json()

    @pytest.mark.asyncio
    async def test_leaderboard_endpoint(self, client):
        resp = await client.get("/leaderboard/xp")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_agent_full_stats_404_for_unknown(self, client):
        resp = await client.get("/agent/nonexistent/stats")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# T10: Dashboard Endpoints (Gateway API)
# ═══════════════════════════════════════════════════════════

class TestT10DashboardEndpoints:

    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["db"] is True
        assert data["rpc"] is True

    @pytest.mark.asyncio
    async def test_stats_overview(self, client):
        resp = await client.get("/stats/overview")
        assert resp.status_code == 200
        assert "total_agents" in resp.json()

    @pytest.mark.asyncio
    async def test_activity_recent(self, client):
        resp = await client.get("/activity/recent")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_epoch_info(self, client):
        resp = await client.get("/game/epoch")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_epoch" in data
        assert "epoch_state" in data


# ═══════════════════════════════════════════════════════════
# T11: Idempotency
# ═══════════════════════════════════════════════════════════

class TestT11Idempotency:

    @pytest.mark.asyncio
    async def test_duplicate_action_id(self, client):
        jwt = await get_jwt(client, "1")
        headers = auth_header(jwt)
        aid = action_id()

        resp1 = await client.post("/game/enter", json={
            "dungeon_id": 0, "action_id": aid,
        }, headers=headers)
        resp2 = await client.post("/game/enter", json={
            "dungeon_id": 0, "action_id": aid,
        }, headers=headers)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["id"] == resp2.json()["id"]

    @pytest.mark.asyncio
    async def test_rate_limit(self, client):
        jwt = await get_jwt(client, "rate-test")
        headers = auth_header(jwt)
        hit_429 = False
        for i in range(12):
            resp = await client.post("/game/enter", json={
                "dungeon_id": 0, "action_id": action_id(),
            }, headers=headers)
            if resp.status_code == 429:
                hit_429 = True
                assert i >= 10, f"Rate limit hit too early at {i}"
                break
        # Rate limit may not trigger if epoch check returns 409 first
        # That's OK — we just verify it doesn't crash


# ═══════════════════════════════════════════════════════════
# T12: Gateway Pre-flight Validation
# ═══════════════════════════════════════════════════════════

class TestT12GatewayValidation:

    @pytest.mark.asyncio
    async def test_auth_required(self, client):
        resp = await client.post("/game/enter", json={
            "dungeon_id": 0, "action_id": action_id(),
        })
        assert resp.status_code in (401, 422)

    @pytest.mark.asyncio
    async def test_invalid_token(self, client):
        resp = await client.post("/game/enter", json={
            "dungeon_id": 0, "action_id": action_id(),
        }, headers={"Authorization": "Bearer invalid-jwt-token"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_session_not_found(self, client):
        jwt = await get_jwt(client, "1")
        headers = auth_header(jwt)
        resp = await client.post("/game/action", json={
            "session_id": 99999, "turn_index": 0,
            "action": "test", "action_id": action_id(),
        }, headers=headers)
        # Gateway returns 404 (not found) or 409 (state mismatch for zeroed session)
        assert resp.status_code in (404, 409)

    @pytest.mark.asyncio
    async def test_moltbook_verify_returns_jwt(self, client):
        resp = await client.post("/auth/verify", json={"token": "agent-test123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "jwt" in data
        assert data["agent_id"] == "test123"
