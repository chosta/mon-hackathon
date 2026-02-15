"""Deploy all contracts to Anvil and configure them for testing."""
import json
import os
from web3 import Web3

# Anvil deterministic accounts (first 10)
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # 4
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",  # 6
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",  # 7
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",  # 8
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",  # 9
]

ANVIL_ADDRESSES = [
    Web3.to_checksum_address(Web3().eth.account.from_key(k).address)
    for k in ANVIL_KEYS
]

# Roles:
# Account 0 = deployer/owner
# Account 1 = runner (gateway hot wallet)
# Accounts 2-5 = test agents (players)
# Account 6 = dungeon NFT owner (staker)

DEPLOYER_KEY = ANVIL_KEYS[0]
DEPLOYER = ANVIL_ADDRESSES[0]
RUNNER_KEY = ANVIL_KEYS[1]
RUNNER = ANVIL_ADDRESSES[1]
AGENT_KEYS = ANVIL_KEYS[2:6]
AGENT_ADDRESSES = ANVIL_ADDRESSES[2:6]
STAKER_KEY = ANVIL_KEYS[6]
STAKER = ANVIL_ADDRESSES[6]

ABI_DIR = os.path.join(os.path.dirname(__file__), "..", "out")


def load_abi(contract_name: str) -> tuple[list, str]:
    """Load ABI and bytecode from forge output."""
    path = os.path.join(ABI_DIR, f"{contract_name}.sol", f"{contract_name}.json")
    with open(path) as f:
        data = json.load(f)
    return data["abi"], data["bytecode"]["object"]


def deploy_contract(w3: Web3, key: str, abi: list, bytecode: str, *args, value: int = 0) -> str:
    """Deploy a contract, return its address."""
    account = w3.eth.account.from_key(key)
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor(*args).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 31337,
        "gas": 8_000_000,
        "gasPrice": w3.eth.gas_price,
        "value": value,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1, f"Deploy failed: {contract_name}"
    return receipt["contractAddress"]


def send_tx(w3: Web3, key: str, contract, method: str, *args, value: int = 0):
    """Call a contract method as a transaction."""
    account = w3.eth.account.from_key(key)
    fn = getattr(contract.functions, method)(*args)
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 31337,
        "gas": 2_000_000,
        "gasPrice": w3.eth.gas_price,
        "value": value,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1, f"TX failed: {method} — {receipt}"
    return receipt


def deploy_all(w3: Web3) -> dict:
    """Deploy Gold, DungeonNFT, DungeonTickets, DungeonManager and configure.
    
    Returns dict with contract addresses and web3 contract objects.
    """
    # Deploy Gold
    gold_abi, gold_bytecode = load_abi("Gold")
    gold_addr = deploy_contract(w3, DEPLOYER_KEY, gold_abi, gold_bytecode)
    gold = w3.eth.contract(address=gold_addr, abi=gold_abi)

    # Deploy DungeonNFT
    nft_abi, nft_bytecode = load_abi("DungeonNFT")
    nft_addr = deploy_contract(w3, DEPLOYER_KEY, nft_abi, nft_bytecode)
    nft = w3.eth.contract(address=nft_addr, abi=nft_abi)

    # Deploy DungeonTickets
    tickets_abi, tickets_bytecode = load_abi("DungeonTickets")
    ticket_price = Web3.to_wei(100, "ether")  # 100 GOLD per ticket
    tickets_addr = deploy_contract(w3, DEPLOYER_KEY, tickets_abi, tickets_bytecode, gold_addr, ticket_price)
    tickets = w3.eth.contract(address=tickets_addr, abi=tickets_abi)

    # Deploy DungeonManager
    mgr_abi, mgr_bytecode = load_abi("DungeonManager")
    mgr_addr = deploy_contract(w3, DEPLOYER_KEY, mgr_abi, mgr_bytecode, gold_addr, nft_addr, tickets_addr)
    manager = w3.eth.contract(address=mgr_addr, abi=mgr_abi)

    # Configure
    # Gold: set minter to DungeonManager
    send_tx(w3, DEPLOYER_KEY, gold, "setMinter", mgr_addr)

    # Tickets: set burner to DungeonManager
    send_tx(w3, DEPLOYER_KEY, tickets, "setBurner", mgr_addr)

    # Manager: set runner
    send_tx(w3, DEPLOYER_KEY, manager, "setRunner", RUNNER)

    # Contract starts in Grace state — stake dungeon BEFORE startEpoch
    # Mint a DungeonNFT to staker, approve, stake
    # mint(to, difficulty=1, partySize=2, theme=0, rarity=0)
    # partySize=2: 1 DM + 1 player per session (avoids actionSubmitted conflict)
    send_tx(w3, DEPLOYER_KEY, nft, "mint", STAKER, 1, 2, 0, 0)
    nft_id = 0  # first token
    send_tx(w3, STAKER_KEY, nft, "approve", mgr_addr, nft_id)
    send_tx(w3, STAKER_KEY, manager, "stakeDungeon", nft_id)

    # Register agents
    send_tx(w3, DEPLOYER_KEY, manager, "batchRegisterAgents", AGENT_ADDRESSES)

    # Now start epoch (Grace → Active)
    send_tx(w3, DEPLOYER_KEY, manager, "startEpoch")

    # Mint tickets for all agents (10 each)
    for agent_addr in AGENT_ADDRESSES:
        send_tx(w3, DEPLOYER_KEY, tickets, "mint", agent_addr, 10)

    return {
        "gold_addr": gold_addr,
        "nft_addr": nft_addr,
        "tickets_addr": tickets_addr,
        "manager_addr": mgr_addr,
        "gold": gold,
        "nft": nft,
        "tickets": tickets,
        "manager": manager,
    }
