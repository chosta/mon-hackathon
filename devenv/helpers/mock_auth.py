"""Mock Moltbook auth — generates JWTs for test agents without real Moltbook."""
import os
import time
import jwt

JWT_SECRET = os.environ.get("GW_JWT_SECRET", "local-dev-secret")  # Must match gateway config
JWT_EXPIRY = 3600


# Map agent addresses to fake Moltbook IDs
MOCK_AGENTS = {
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8": {"id": "agent-001", "name": "TestWarrior"},
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC": {"id": "agent-002", "name": "TestMage"},
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906": {"id": "agent-003", "name": "TestRogue"},
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65": {"id": "agent-004", "name": "TestCleric"},
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc": {"id": "agent-005", "name": "TestPaladin"},
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266": {"id": "agent-000", "name": "Deployer"},
}


def create_mock_jwt(address: str) -> str:
    """Create a JWT for a test agent, bypassing Moltbook."""
    agent = MOCK_AGENTS.get(address)
    if not agent:
        raise ValueError(f"Unknown test agent: {address}")

    payload = {
        "sub": agent["id"],
        "name": agent["name"],
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_moltbook_id(address: str) -> str:
    """Get the mock Moltbook ID for an address."""
    agent = MOCK_AGENTS.get(address)
    return agent["id"] if agent else f"mock-{address[:10]}"
