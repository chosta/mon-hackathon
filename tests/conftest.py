"""Pytest fixtures: Anvil, contract deployment, mock moltbook, gateway client."""
import os
import sys
import time
import subprocess
import asyncio
import pytest
import pytest_asyncio
import httpx

from web3 import Web3

# Add gateway to path
GATEWAY_DIR = os.path.join(os.path.dirname(__file__), "..", "gateway")
sys.path.insert(0, GATEWAY_DIR)

from tests.deploy_helpers import (
    deploy_all, DEPLOYER_KEY, DEPLOYER, RUNNER_KEY, RUNNER,
    AGENT_KEYS, AGENT_ADDRESSES, STAKER_KEY, STAKER,
)
from tests.anvil_helpers import snapshot as evm_snapshot, revert as evm_revert


# ─── Anvil Process ──────────────────────────────────────

@pytest.fixture(scope="session")
def anvil():
    """Start Anvil, yield Web3 connection, stop on teardown."""
    proc = subprocess.Popen(
        [os.path.expanduser("~/.foundry/bin/anvil"), "--chain-id", "31337", "--silent", "--port", "18545"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:18545"))
    # Wait for Anvil to be ready
    for _ in range(50):
        try:
            w3.eth.block_number
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("Anvil failed to start")
    yield w3
    proc.kill()
    proc.wait()


# ─── Contract Deployment ────────────────────────────────

@pytest.fixture(scope="session")
def contracts(anvil):
    """Deploy all contracts once per session."""
    return deploy_all(anvil)


# ─── Snapshot/Revert Per Test ────────────────────────────

@pytest.fixture(autouse=True)
def evm_snapshot_revert(anvil):
    """Snapshot before each test, revert after."""
    snap_id = evm_snapshot(anvil)
    yield
    evm_revert(anvil, snap_id)


# ─── Gateway Environment ────────────────────────────────

@pytest.fixture(autouse=True)
def gateway_env(contracts, monkeypatch):
    """Set environment variables for the gateway to use our Anvil chain."""
    monkeypatch.setenv("GW_RPC_URL", "http://127.0.0.1:18545")
    monkeypatch.setenv("GW_CHAIN_ID", "31337")
    monkeypatch.setenv("GW_RUNNER_PRIVATE_KEY", RUNNER_KEY)
    monkeypatch.setenv("GW_DUNGEON_MANAGER", contracts["manager_addr"])
    monkeypatch.setenv("GW_GOLD_CONTRACT", contracts["gold_addr"])
    monkeypatch.setenv("GW_DUNGEON_NFT", contracts["nft_addr"])
    monkeypatch.setenv("GW_DUNGEON_TICKETS", contracts["tickets_addr"])
    monkeypatch.setenv("GW_DB_PATH", ":memory:")
    monkeypatch.setenv("GW_JWT_SECRET", "test-secret")


# ─── Mock Moltbook ──────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_moltbook(monkeypatch):
    """Mock moltbook.verify_agent to return fake profiles based on token."""
    async def fake_verify(token: str) -> dict:
        # token format: "agent-{N}" → returns profile with that id
        agent_id = token.replace("agent-", "")
        return {"id": agent_id, "name": f"Agent {agent_id}"}

    monkeypatch.setattr("moltbook.verify_agent", fake_verify)


# ─── Gateway App + Client ───────────────────────────────

@pytest_asyncio.fixture
async def client(gateway_env):
    """Async HTTP client bound to gateway ASGI app.
    
    We must reimport main to pick up the monkeypatched env.
    Mock moltbook is applied AFTER reimport.
    """
    # Force reimport of config and dependent modules so they pick up env vars
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("config", "contract", "database", "db_backend", "main", "tx_worker", "auth", "moltbook", "models"):
            del sys.modules[mod_name]

    import moltbook as moltbook_mod

    # Patch moltbook.verify_agent on the reimported module
    original = moltbook_mod.verify_agent
    async def fake_verify(token: str) -> dict:
        agent_id = token.replace("agent-", "")
        return {"id": agent_id, "name": f"Agent {agent_id}"}
    moltbook_mod.verify_agent = fake_verify

    from main import app
    
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    # Restore and cleanup
    moltbook_mod.verify_agent = original
    try:
        from database import close_db
        await close_db()
    except Exception:
        pass


# ─── Auth Helper ────────────────────────────────────────

async def get_jwt(client: httpx.AsyncClient, agent_id: str) -> str:
    """Verify a fake agent and return JWT."""
    resp = await client.post("/auth/verify", json={"token": f"agent-{agent_id}"})
    assert resp.status_code == 200, f"Auth failed: {resp.text}"
    return resp.json()["jwt"]


def auth_header(jwt_token: str) -> dict:
    """Return Authorization header dict."""
    return {"Authorization": f"Bearer {jwt_token}"}
