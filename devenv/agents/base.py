"""Base agent — wallet + on-chain + gateway API calls."""
import json
import os
import subprocess
import uuid
import httpx
from web3 import Web3

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers.mock_auth import create_mock_jwt, get_moltbook_id

FOUNDRY_BIN = os.path.expanduser("~/.foundry/bin")
RPC_URL = "http://127.0.0.1:8545"
GATEWAY_URL = "http://127.0.0.1:8000"


class BaseAgent:
    """Agent with wallet and game interaction methods."""

    def __init__(self, address: str, private_key: str, name: str = None):
        self.address = Web3.to_checksum_address(address)
        self.private_key = private_key
        self.name = name or address[:10]
        self.moltbook_id = get_moltbook_id(address)
        self.jwt = None
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self._load_deployment()

    def _load_deployment(self):
        """Load contract addresses from local-deployment.json."""
        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        manifest_path = os.path.join(project_dir, "local-deployment.json")
        with open(manifest_path) as f:
            data = json.load(f)
        self.contracts = data["contracts"]
        self.manager_address = self.contracts["DungeonManager"]

        # Load ABI
        abi_path = os.path.join(project_dir, "out", "DungeonManager.sol", "DungeonManager.json")
        with open(abi_path) as f:
            self.manager_abi = json.load(f)["abi"]

        self.manager = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.manager_address),
            abi=self.manager_abi,
        )

    def auth(self) -> str:
        """Get a JWT token (mock auth, no Moltbook needed)."""
        self.jwt = create_mock_jwt(self.address)
        return self.jwt

    def _headers(self) -> dict:
        if not self.jwt:
            self.auth()
        return {"Authorization": f"Bearer {self.jwt}"}

    def _action_id(self) -> str:
        return str(uuid.uuid4())[:16]

    # === Direct on-chain calls (for functions requiring msg.sender) ===

    def _cast_send(self, to: str, sig: str, *args, value: str = None) -> dict:
        """Call cast send with this agent's private key."""
        cmd = [
            f"{FOUNDRY_BIN}/cast", "send", "--json",
            "--rpc-url", RPC_URL,
            "--private-key", self.private_key,
            to, sig,
        ] + list(args)
        if value:
            cmd += ["--value", value]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        if data.get("status") not in ("0x1", "1"):
            raise RuntimeError(f"Tx failed for {self.name}: {data}")
        return data

    def enter_dungeon(self, dungeon_id: int = 0) -> int:
        """Enter a dungeon on-chain (requires msg.sender). Returns session_id."""
        print(f"  [{self.name}] Entering dungeon {dungeon_id}...")
        tx_data = self._cast_send(
            self.manager_address,
            "enterDungeon(uint256)", str(dungeon_id),
            value="10000000000000000",  # 0.01 ether ENTRY_BOND
        )
        # Parse session_id from PlayerEntered event in tx logs
        # PlayerEntered(uint256 indexed sessionId, address indexed agent)
        # Topic0 = keccak256("PlayerEntered(uint256,address)")
        session_id = None
        for log in tx_data.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) >= 2 and topics[0] and "PlayerEntered" in str(topics[0]):
                session_id = int(topics[1], 16)
                break
            # Match by topic hash for PlayerEntered
            if len(topics) >= 2 and topics[0] == "0x" + self.w3.keccak(text="PlayerEntered(uint256,address)").hex():
                session_id = int(topics[1], 16)
                break
        if session_id is None:
            # Fallback: read currentSessionId (may be wrong if party just filled)
            dungeon_data = self.manager.functions.dungeons(dungeon_id).call()
            session_id = dungeon_data[4]
        print(f"  [{self.name}] Entered session {session_id}")
        return session_id

    def accept_dm(self, session_id: int) -> bool:
        """Accept DM role on-chain (requires msg.sender == selected DM)."""
        info = self.get_session(session_id)
        if info["state"] != 1:  # WaitingDM
            print(f"  [{self.name}] Session not in WaitingDM state (state={info['state']})")
            return False
        dm_epoch = info["dm_epoch"]
        print(f"  [{self.name}] Accepting DM role (epoch={dm_epoch})...")
        self._cast_send(
            self.manager_address,
            "acceptDM(uint256,uint64)", str(session_id), str(dm_epoch),
        )
        print(f"  [{self.name}] DM role accepted!")
        return True

    # === Gateway API calls (for runner-relayed functions) ===

    def submit_action(self, session_id: int, turn_index: int, action: str) -> dict:
        """Submit a player action via gateway."""
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{GATEWAY_URL}/game/action",
                headers=self._headers(),
                json={
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "action": action,
                    "action_id": self._action_id(),
                },
            )
            resp.raise_for_status()
            return resp.json()

    def submit_dm_response(self, session_id: int, turn_index: int,
                           narrative: str, actions: list[dict]) -> dict:
        """Submit a DM response via gateway."""
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{GATEWAY_URL}/game/dm",
                headers=self._headers(),
                json={
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "narrative": narrative,
                    "actions": actions,
                    "action_id": self._action_id(),
                },
            )
            resp.raise_for_status()
            return resp.json()

    def get_session(self, session_id: int) -> dict:
        """Get session info from the contract directly."""
        SESSION_STATES = {0: "Waiting", 1: "WaitingDM", 2: "Active", 3: "Completed", 4: "Failed", 5: "Cancelled", 6: "TimedOut"}
        result = self.manager.functions.sessions(session_id).call()
        return {
            "session_id": session_id,
            "dungeon_id": result[0],
            "dm": result[1],
            "state": result[2],
            "state_name": SESSION_STATES.get(result[2], "Unknown"),
            "current_turn": result[3],
            "current_actor": result[4],
            "turn_deadline": result[5],
            "gold_pool": result[6],
            "max_gold": result[7],
            "dm_accept_deadline": result[9],
            "dm_epoch": result[11],
            "epoch_id": result[12],
        }

    def get_party(self, session_id: int) -> list[str]:
        """Get party members for a session."""
        try:
            return self.manager.functions.getParty(session_id).call()
        except Exception:
            return []

    def is_alive(self, session_id: int, player: str = None) -> bool:
        """Check if a player is alive in session."""
        addr = player or self.address
        try:
            return self.manager.functions.sessionPlayerAlive(session_id, addr).call()
        except Exception:
            return True
