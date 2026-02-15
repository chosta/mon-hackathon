"""Demo script: end-to-end flow through the gateway."""
import asyncio
import httpx
import json
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

BASE = "http://localhost:8080"

# Generate a throwaway wallet for demo
demo_account = Account.create()
DEMO_WALLET = demo_account.address
DEMO_KEY = demo_account.key.hex()


async def demo():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # 1. Health check
        r = await c.get("/health")
        print(f"1. Health: {r.json()}")

        # 2. Verify (will fail without real Moltbook token — that's expected)
        print("\n2. Auth verify (mock — will fail without real Moltbook API):")
        r = await c.post("/auth/verify", json={"token": "moltbook_demo_token_123"})
        if r.status_code == 401:
            print(f"   Expected 401: {r.json()['detail']}")
            # For demo, we'll manually create a JWT
            from auth import create_jwt
            jwt_token = create_jwt("demo-agent-001", "DemoAgent")
            print(f"   Created demo JWT directly")
        else:
            jwt_token = r.json()["jwt"]
            print(f"   Got JWT for {r.json()['agent_name']}")

        headers = {"Authorization": f"Bearer {jwt_token}"}

        # 3. Get nonce for wallet linking
        print(f"\n3. Get nonce for wallet {DEMO_WALLET[:10]}...:")
        r = await c.get(f"/auth/nonce?wallet_address={DEMO_WALLET}")
        nonce_data = r.json()
        print(f"   Nonce: {nonce_data['nonce'][:16]}...")

        # 4. Sign and link wallet
        print("\n4. Link wallet:")
        message = encode_defunct(text=nonce_data["message"])
        sig = demo_account.sign_message(message)
        r = await c.post("/auth/link", json={
            "wallet_address": DEMO_WALLET,
            "signature": "0x" + (sig.signature.hex() if isinstance(sig.signature, bytes) else hex(sig.signature)[2:]),
        }, headers=headers)
        print(f"   Link result: {r.json()}")

        # 5. Enter dungeon
        print("\n5. Enter dungeon 0:")
        r = await c.post("/game/enter", json={
            "dungeon_id": 0,
            "action_id": "demo-enter-001",
        }, headers=headers)
        enter_result = r.json()
        print(f"   Result: {enter_result}")

        # 6. Submit action
        print("\n6. Submit action:")
        r = await c.post("/game/action", json={
            "session_id": 0,
            "action": "I draw my sword and cautiously enter the dark cave.",
            "action_id": "demo-action-001",
        }, headers=headers)
        action_result = r.json()
        print(f"   Result: {action_result}")

        # 7. Check tx status
        if "id" in enter_result:
            print(f"\n7. Check tx status for enter (id={enter_result['id']}):")
            r = await c.get(f"/tx/{enter_result['id']}")
            print(f"   Status: {r.json()}")

        # 8. Agent stats
        print("\n8. Agent stats:")
        r = await c.get("/stats/agent/demo-agent-001")
        print(f"   Stats: {r.json()}")

        # 9. Idempotency test
        print("\n9. Idempotency (re-submit same action_id):")
        r = await c.post("/game/enter", json={
            "dungeon_id": 0,
            "action_id": "demo-enter-001",
        }, headers=headers)
        print(f"   Same tx returned: {r.json()}")

        print("\n✅ Demo complete!")


if __name__ == "__main__":
    asyncio.run(demo())
