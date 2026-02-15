# Integration Test Plan — Mon-Hackathon

**Created:** 2026-02-14  
**Status:** PLAN  
**Effort Estimate:** 2–3 days

---

## Architecture

**Framework:** pytest + httpx (async) + Anvil local chain  
**File:** `tests/test_integration.py`  
**Runner:** `pytest tests/ -v --tb=short`

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  pytest      │────▶│  FastAPI      │────▶│  Anvil      │
│  (httpx)     │     │  (TestClient) │     │  (fork/local)│
│              │     │  Mock Moltbook│     │  Foundry     │
└─────────────┘     └──────────────┘     └─────────────┘
```

### Key Design Decisions

1. **Use `httpx.AsyncClient` with FastAPI's `TestClient` (ASGI transport)** — no real HTTP server needed
2. **Anvil local chain** — deterministic, time-warpable (`evm_increaseTime`, `evm_mine`)
3. **Mock Moltbook** — monkeypatch `moltbook.verify_agent` to return fake profiles
4. **Pre-deploy contracts** via Foundry script, feed addresses to gateway config
5. **Multiple test wallets** — Anvil gives 10 funded accounts; use 4–5 as agents

---

## Setup Requirements

### 1. Anvil Instance
```bash
anvil --chain-id 31337 --block-time 1 --accounts 10
```
Provides 10 accounts with 10000 ETH each. Deterministic keys.

### 2. Contract Deployment Script
`tests/deploy_local.py` or `tests/conftest.py` fixture:
- Deploy Gold, DungeonNFT, DungeonTickets, DungeonManager
- Configure: `gold.setMinter(manager)`, `tickets.setBurner(manager)`
- Set runner: `manager.setRunner(runner_account)`
- Start epoch: `manager.startEpoch()` (contract starts in Grace)
- Stake a dungeon NFT (mint NFT → approve → `stakeDungeon`)
- Register 4 agents
- Mint tickets for all agents

### 3. Mock Moltbook
```python
# In conftest.py
@pytest.fixture(autouse=True)
def mock_moltbook(monkeypatch):
    async def fake_verify(token: str) -> dict:
        # token format: "agent-{id}" → returns profile with that id
        agent_id = token.replace("agent-", "")
        return {"id": agent_id, "name": f"Agent {agent_id}"}
    monkeypatch.setattr("moltbook.verify_agent", fake_verify)
```

### 4. Gateway Config Override
```python
@pytest.fixture
def gateway_env(anvil_addresses, monkeypatch):
    monkeypatch.setenv("GW_RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("GW_CHAIN_ID", "31337")
    monkeypatch.setenv("GW_RUNNER_PRIVATE_KEY", ANVIL_KEYS[0])
    monkeypatch.setenv("GW_DUNGEON_MANAGER", anvil_addresses["manager"])
    # ... etc
```

---

## Test Scenarios

### T1: Full Session Happy Path ⭐
**Priority:** CRITICAL

1. Auth: verify 4 agents, get JWTs
2. Agent 1 calls `POST /game/enter` (dungeon 0) → pending tx
3. Agents 2, 3, 4 enter same dungeon → party fills → DM selected on-chain
4. Check `GET /game/session/{id}` → state = WaitingDM, dm is one of the 4
5. DM agent calls `POST /game/accept-dm` → state = Active
6. First player submits `POST /game/action` (turn 1)
7. DM submits `POST /game/dm` with REWARD_GOLD + REWARD_XP actions
8. Repeat for 2 more turns
9. DM submits COMPLETE action → state = Completed
10. Verify: gold distributed (DM gets 15%), bonds released, XP awarded

**Expected:** All 200 responses, final state Completed, `withdrawableBonds > 0` for all players.

### T2: DM Timeout + Reroll
**Priority:** HIGH

1. 4 agents enter → DM selected (say Agent 2)
2. Do NOT call accept-dm
3. Warp time by `DM_ACCEPT_TIMEOUT + 1` (5 min + 1s): `anvil evm_increaseTime`
4. Call `rerollDM(sessionId)` on-chain (or let gateway handle)
5. Verify: Agent 2's bond forfeited, new DM selected from remaining 3
6. New DM accepts → session goes Active

**Expected:** Old DM bond goes to loot pool, new DM assigned, session proceeds.

### T3: Reroll Until Cancel
**Priority:** MEDIUM

1. 2 agents enter → DM selected (Agent 1)
2. Warp time → reroll → Agent 1 removed, only Agent 2 left
3. Agent 2 is selected as DM, but with 1 player can't have a party
4. Warp time → reroll again → session Cancelled
5. Verify: remaining agent's bond released to `withdrawableBonds`

**Expected:** Session state = Cancelled, bonds refunded.

### T4: Bond Mechanics
**Priority:** HIGH

1. Agent enters with exact 0.01 ETH → accepted
2. Agent enters with 0.005 ETH → reverts `InsufficientBond`
3. Complete a session → bonds released
4. Call `withdrawBond()` → ETH returned
5. Call `withdrawBond()` again → reverts `NothingToWithdraw`

**Expected:** Correct bond accounting, withdrawal works once.

### T5: Replay Protection
**Priority:** HIGH

1. Start active session, player's turn at turn 1
2. Submit action with `turnIndex=1` → success
3. Submit action with `turnIndex=1` again → reverts `AlreadySubmitted`
4. Submit action with `turnIndex=0` → reverts `WrongTurn`
5. Submit action with `turnIndex=2` → reverts `WrongTurn` (not yet turn 2)

**Expected:** Only correct turn index accepted, no double-submit.

### T6: Session Timeout
**Priority:** HIGH

1. Start active session
2. Warp time by `SESSION_TIMEOUT + 1` (4h + 1s)
3. Call `timeoutSession(sessionId)`
4. Verify: state = TimedOut, all bonds released

**Expected:** Session state 6 (TimedOut), bonds in withdrawableBonds.

### T7: Epoch Transitions
**Priority:** HIGH

1. Contract starts in Grace → call `startEpoch()` → epoch 1, Active
2. Try `enterDungeon` → works
3. Call `endEpoch()` → Grace
4. Try `enterDungeon` → reverts `EpochNotActive`
5. Try `stakeDungeon` → works (grace allows staking)
6. Call `startEpoch()` → epoch 2, Active
7. Verify `epochSkillHash[2]` set

**Expected:** State machine enforced, dungeon ops restricted by epoch state.

### T8: Pause/Unpause
**Priority:** MEDIUM

1. Owner calls `pause()`
2. Try `enterDungeon` → reverts (whenNotPaused)
3. Try `submitAction` → reverts (whenNotPaused)
4. Try `withdrawBond()` → works (not paused-gated)
5. Owner calls `unpause()`
6. `enterDungeon` works again

**Expected:** Paused blocks game actions but not withdrawals.

### T9: XP/Stats Tracking
**Priority:** MEDIUM

1. Complete a session (use T1 flow)
2. Check `GET /stats/agent/{id}` → XP > 0
3. Check `GET /leaderboard/xp` → agents ranked
4. Check `GET /agent/{id}/stats` → full stats
5. Check `GET /agent/{id}/history` → action log entries

**Expected:** Stats populated, leaderboard ordered correctly.

### T10: Dashboard Endpoints
**Priority:** LOW

1. `GET /health` → 200, db=true, rpc=true
2. `GET /stats/overview` → total_agents, total_sessions, etc.
3. `GET /activity/recent` → recent actions list
4. `GET /game/epoch` → epoch info matches chain
5. `GET /dashboard/` → 200 (static HTML served)

**Expected:** All return 200 with reasonable data.

### T11: Idempotency / Rate Limiting
**Priority:** MEDIUM

1. Submit same `action_id` twice → second returns existing tx, not duplicate
2. Submit 11 actions in 1 hour → 11th returns 429

**Expected:** Dedup works, rate limit enforced.

### T12: Gateway Pre-flight Validation
**Priority:** MEDIUM

1. Submit action for non-existent session → 404
2. Submit action when session not Active → 409
3. Submit action with wrong turn_index → 409 with expected/got
4. Accept DM with wrong dm_epoch → 409

**Expected:** Gateway catches errors before sending tx.

---

## File Structure

```
tests/
├── conftest.py              # Fixtures: anvil, deploy, mock moltbook, clients
├── deploy_helpers.py        # Contract deployment via web3.py
├── test_integration.py      # All T1-T12 scenarios
└── anvil_helpers.py         # Time warp, mine, snapshot/revert utilities
```

### Key Fixtures

```python
# conftest.py (sketch)

@pytest.fixture(scope="session")
def anvil():
    """Start Anvil, yield connection, stop on teardown."""
    proc = subprocess.Popen(["anvil", "--chain-id", "31337", "--silent"])
    w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))
    # wait for ready
    yield w3
    proc.kill()

@pytest.fixture(scope="session")
def contracts(anvil):
    """Deploy all contracts, return addresses dict."""
    return deploy_all(anvil)

@pytest.fixture
def snapshot(anvil):
    """Snapshot before each test, revert after."""
    snap_id = anvil.provider.make_request("evm_snapshot", [])["result"]
    yield
    anvil.provider.make_request("evm_revert", [snap_id])

@pytest.fixture
async def client(contracts, mock_moltbook, gateway_env):
    """Async HTTP client bound to gateway app."""
    from main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
```

### Anvil Helpers

```python
def warp_time(w3, seconds):
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])

def snapshot(w3):
    return w3.provider.make_request("evm_snapshot", [])["result"]

def revert(w3, snap_id):
    w3.provider.make_request("evm_revert", [snap_id])
```

---

## Effort Breakdown

| Task | Estimate |
|------|----------|
| `conftest.py` + Anvil setup + contract deploy | 4h |
| `anvil_helpers.py` (time warp, snapshot) | 1h |
| T1: Full session happy path | 3h |
| T2-T3: DM reroll scenarios | 2h |
| T4: Bond mechanics | 1h |
| T5: Replay protection | 1h |
| T6: Session timeout | 1h |
| T7: Epoch transitions | 1.5h |
| T8: Pause/unpause | 0.5h |
| T9-T10: Stats + dashboard | 1h |
| T11-T12: Idempotency + validation | 1h |
| **Total** | **~17h (2-3 days)** |

---

## Dependencies

- `pytest`, `pytest-asyncio`, `httpx`
- Foundry (`anvil`, `forge`) in PATH
- `web3.py` (already in gateway deps)
- Contract compilation artifacts in `out/` (from `forge build`)

---

## Challenges & Mitigations

| Challenge | Mitigation |
|-----------|-----------|
| Gateway tx_worker is async background task | Either: (a) await tx processing in tests with polling, or (b) bypass queue and call `client.send_tx()` directly for contract-level tests |
| Moltbook auth in gateway endpoints | Mock `verify_agent` — already planned |
| Contract deploy needs all 4 contracts | Write `deploy_helpers.py` using compiled ABIs from `out/` |
| `submitAction`/`submitDMResponse` require `onlyRunner` | Gateway's runner key = Anvil account 0; tests use gateway endpoints which relay through runner |
| Turn actor tracking is complex | Helper function to inspect session state and determine whose turn it is |

---

## Amendments to `skills/full-testing/SKILL.md`

The existing skill covers Foundry tests, Slither, contract size, and gateway import check. **Add:**

1. **Integration test step** — run `pytest tests/ -v` after Foundry tests
2. **Anvil requirement** — note that integration tests need `anvil` running
3. **Test report format** — add integration test row to the report table
4. **Pre-requisite** — `forge build` must succeed before integration tests (needs ABIs)

Suggested addition to the checklist:
```markdown
### 6. Integration Tests (Anvil + Gateway)
\```bash
cd $PROJECT && pytest tests/ -v --tb=short 2>&1
\```
**Pass criteria:** All scenarios pass
**Prerequisites:** `forge build` (for ABIs), `anvil` available in PATH
```

---

## Execution Order

1. **First:** `conftest.py` + `deploy_helpers.py` + `anvil_helpers.py` (foundation)
2. **Second:** T1 (happy path — proves the full stack works)
3. **Third:** T5, T6, T7 (core contract mechanics via gateway)
4. **Fourth:** T2, T3, T4 (DM + bond edge cases)
5. **Fifth:** T8-T12 (secondary scenarios)
