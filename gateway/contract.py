"""Contract client with nonce manager for Monad testnet."""
import asyncio
import json
import os
import structlog
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from config import settings

logger = structlog.get_logger()

SESSION_STATES = {
    0: "Waiting",
    1: "WaitingDM",
    2: "Active",
    3: "Completed",
    4: "Failed",
    5: "Cancelled",
    6: "TimedOut",
}

# Load ABI - check multiple locations
_abi_paths = [
    os.path.join(os.path.dirname(__file__), "..", "out", "DungeonManager.sol", "DungeonManager.json"),
    os.path.join(os.path.dirname(__file__), "abi", "DungeonManager.json"),
]
_abi = []
for _abi_path in _abi_paths:
    if os.path.exists(_abi_path):
        with open(_abi_path) as f:
            _abi = json.load(f)["abi"]
        break
else:
    logger.warning("abi_not_found", paths=_abi_paths)


class NonceManager:
    """Thread-safe nonce manager for the runner wallet."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._nonce: int | None = None

    async def get_nonce(self, w3: Web3, address: str) -> int:
        async with self._lock:
            if self._nonce is None:
                self._nonce = w3.eth.get_transaction_count(address, "pending")
            else:
                self._nonce += 1
            return self._nonce

    def reset(self):
        self._nonce = None


nonce_manager = NonceManager()


class ContractClient:
    """Wrapper around DungeonManager contract."""

    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.dungeon_manager),
            abi=_abi,
        )
        if settings.runner_private_key:
            self.account = self.w3.eth.account.from_key(settings.runner_private_key)
            self.runner_address = self.account.address
        else:
            self.account = None
            self.runner_address = None

    def is_healthy(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

    async def send_tx(self, method: str, *args) -> str:
        """Build, sign, and send a transaction. Returns tx hash."""
        if not self.account:
            raise RuntimeError("No runner private key configured")

        fn = self.contract.functions[method](*args)
        nonce = await nonce_manager.get_nonce(self.w3, self.runner_address)

        # Get current gas price from the network
        gas_price = self.w3.eth.gas_price
        # Use 1.5x current gas price to ensure acceptance
        adjusted_gas = int(gas_price * 1.5)
        
        tx = fn.build_transaction({
            "from": self.runner_address,
            "nonce": nonce,
            "chainId": settings.chain_id,
            "gas": 500_000,
            "gasPrice": adjusted_gas,  # Use legacy gasPrice for Monad
        })

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("tx_sent", method=method, tx_hash=tx_hash.hex(), nonce=nonce)
        return tx_hash.hex()

    def get_receipt(self, tx_hash: str) -> dict | None:
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            return dict(receipt)
        except Exception:
            return None

    def get_agent_stats(self, address: str) -> dict:
        try:
            result = self.contract.functions.getAgentStats(
                Web3.to_checksum_address(address)
            ).call()
            return {
                "xp": result[0],
                "total_gold_earned": result[1],
                "games_played": result[2],
                "is_registered": result[3],
            }
        except Exception as e:
            logger.error("get_agent_stats_failed", error=str(e))
            return {}

    def get_session_info(self, session_id: int) -> dict | None:
        """Parse Session struct from sessions() public getter.
        
        Solidity auto-getter skips array fields (party, allPlayers).
        Struct field order (with arrays removed):
          0: dungeonId, 1: dm, 2: state, 3: turnNumber, 4: currentActor,
          5: turnDeadline, 6: goldPool, 7: maxGold, 8: actedThisTurn,
          9: dmAcceptDeadline, 10: lastActivityTs, 11: dmEpoch
        """
        ZERO_ADDR = "0x" + "0" * 40
        try:
            result = self.contract.functions.sessions(session_id).call()
            state = result[2]
            return {
                "session_id": session_id,
                "dungeon_id": result[0],
                "dm": result[1] if result[1] != ZERO_ADDR else None,
                "state": state,
                "state_name": SESSION_STATES.get(state, "Unknown"),
                "current_turn": result[3],
                "current_actor": result[4] if result[4] != ZERO_ADDR else None,
                "turn_deadline": result[5],
                "gold_pool": result[6],
                "max_gold": result[7],
                "dm_accept_deadline": result[9],
                "last_activity": result[10],
                "dm_epoch": result[11],
                "epoch_id": result[12],
            }
        except Exception as e:
            logger.error("get_session_failed", session_id=session_id, error=str(e))
            return None


    def get_epoch_info(self) -> dict:
        try:
            epoch = self.contract.functions.currentEpoch().call()
            state = self.contract.functions.epochState().call()  # 0=Active, 1=Grace
            grace_start = self.contract.functions.graceStartTime().call()
            session_count = self.contract.functions.sessionCount().call()
            active_session_count = self.contract.functions.activeSessionCount().call()
            return {
                "current_epoch": epoch,
                "epoch_state": "Active" if state == 0 else "Grace",
                "epoch_state_raw": state,
                "grace_start_time": grace_start,
                "session_counter": session_count,
                "active_session_count": active_session_count,
            }
        except Exception as e:
            logger.error("get_epoch_info_failed", error=str(e))
            return {}

    def get_dungeon_count(self) -> int:
        try:
            return self.contract.functions.dungeonCount().call()
        except Exception as e:
            logger.error("get_dungeon_count_failed", error=str(e))
            return 0

    def get_dungeon_info(self, dungeon_id: int) -> dict | None:
        """Get dungeon info from on-chain state.
        
        Dungeon struct fields:
          0: nftId, 1: owner, 2: active, 3: lootPool, 4: currentSessionId
        """
        ZERO_ADDR = "0x" + "0" * 40
        try:
            result = self.contract.functions.dungeons(dungeon_id).call()
            return {
                "dungeon_id": dungeon_id,
                "nft_id": result[0],
                "owner": result[1] if result[1] != ZERO_ADDR else None,
                "active": result[2],
                "loot_pool": result[3],
                "current_session_id": result[4],
            }
        except Exception as e:
            logger.error("get_dungeon_failed", dungeon_id=dungeon_id, error=str(e))
            return None

    def get_all_dungeons(self) -> list[dict]:
        """Get all dungeons from contract."""
        count = self.get_dungeon_count()
        dungeons = []
        for i in range(count):
            info = self.get_dungeon_info(i)
            if info:
                dungeons.append(info)
        return dungeons

    def get_dungeons_overview(self) -> dict:
        """Get dungeon stats overview."""
        dungeons = self.get_all_dungeons()
        total = len(dungeons)
        active = sum(1 for d in dungeons if d["active"])
        
        # Count sessions by state
        waiting = 0
        waiting_dm = 0
        in_progress = 0
        total_loot = 0
        
        for d in dungeons:
            total_loot += d["loot_pool"]
            if d["current_session_id"] and d["current_session_id"] > 0:
                session = self.get_session_info(d["current_session_id"])
                if session:
                    if session["state"] == 0:  # Waiting
                        waiting += 1
                    elif session["state"] == 1:  # WaitingDM
                        waiting_dm += 1
                    elif session["state"] == 2:  # Active
                        in_progress += 1
        
        return {
            "total_dungeons": total,
            "staked_dungeons": active,
            "awaiting_players": waiting,
            "awaiting_dm": waiting_dm,
            "in_progress": in_progress,
            "total_loot_pool": total_loot,
        }


# Load DungeonNFT ABI for traits - check multiple locations
_nft_abi_paths = [
    os.path.join(os.path.dirname(__file__), "..", "out", "DungeonNFT.sol", "DungeonNFT.json"),
    os.path.join(os.path.dirname(__file__), "abi", "DungeonNFT.json"),
]
_nft_abi = []
for _nft_abi_path in _nft_abi_paths:
    if os.path.exists(_nft_abi_path):
        with open(_nft_abi_path) as f:
            _nft_abi = json.load(f)["abi"]
        break
else:
    logger.warning("nft_abi_not_found", paths=_nft_abi_paths)


class DungeonNFTClient:
    """Wrapper for DungeonNFT contract to get traits."""
    
    THEMES = ["Cave", "Forest", "Crypt", "Ruins", "Abyss", "Temple", "Volcano", "Glacier", "Swamp", "Shadow"]
    RARITIES = ["Common", "Rare", "Epic", "Legendary"]
    
    def __init__(self):
        from config import settings
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.dungeon_nft),
            abi=_nft_abi,
        )
    
    def get_traits(self, nft_id: int) -> dict | None:
        """Get dungeon traits from NFT contract.
        
        DungeonTraits struct:
          0: difficulty (1-10), 1: partySize (2-6), 2: theme (enum), 3: rarity (enum)
        """
        try:
            result = self.contract.functions.getTraits(nft_id).call()
            return {
                "nft_id": nft_id,
                "difficulty": result[0],
                "party_size": result[1],
                "theme": self.THEMES[result[2]] if result[2] < len(self.THEMES) else f"Unknown({result[2]})",
                "rarity": self.RARITIES[result[3]] if result[3] < len(self.RARITIES) else f"Unknown({result[3]})",
            }
        except Exception as e:
            logger.error("get_traits_failed", nft_id=nft_id, error=str(e))
            return None


nft_client = DungeonNFTClient()
client = ContractClient()
