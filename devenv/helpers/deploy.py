"""Deploy all contracts to local Anvil and configure them."""
import json
import subprocess
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RPC_URL = "http://127.0.0.1:8545"
CHAIN_ID = 31337

# Anvil account #0 (deployer)
DEPLOYER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Test agents (accounts #1-5)
AGENTS = [
    {"address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", "key": "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"},
    {"address": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC", "key": "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"},
    {"address": "0x90F79bf6EB2c4f870365E785982E1f101E93b906", "key": "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"},
    {"address": "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65", "key": "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"},
    {"address": "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc", "key": "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"},
]

FOUNDRY_BIN = os.path.expanduser("~/.foundry/bin")


def _cast(*args, value=None) -> str:
    """Run a cast command and return stdout."""
    cmd = [f"{FOUNDRY_BIN}/cast"] + list(args) + ["--rpc-url", RPC_URL]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _cast_send(to, sig, *args, private_key=DEPLOYER_KEY, value=None):
    """Run cast send and return parsed JSON."""
    cmd = [f"{FOUNDRY_BIN}/cast", "send", "--json", "--rpc-url", RPC_URL, "--private-key", private_key, to, sig] + list(args)
    if value:
        cmd += ["--value", value]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    if data.get("status") not in ("0x1", "1"):
        raise RuntimeError(f"Tx failed: {data}")
    return data


def deploy() -> dict:
    """Deploy all contracts via forge script. Returns addresses dict."""
    env = os.environ.copy()
    env["PRIVATE_KEY"] = DEPLOYER_KEY
    env["PATH"] = f"{FOUNDRY_BIN}:{env.get('PATH', '')}"

    print("[deploy] Running forge script...")
    result = subprocess.run(
        [f"{FOUNDRY_BIN}/forge", "script", "script/Deploy.s.sol", "--rpc-url", RPC_URL, "--broadcast", "--slow"],
        cwd=PROJECT_DIR, capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(f"Forge deploy failed: {result.stderr[-500:]}")

    # Parse broadcast
    broadcast_file = os.path.join(PROJECT_DIR, f"broadcast/Deploy.s.sol/{CHAIN_ID}/run-latest.json")
    with open(broadcast_file) as f:
        broadcast = json.load(f)

    addresses = {}
    for tx in broadcast["transactions"]:
        name = tx.get("contractName")
        if name:
            addresses[name] = tx["contractAddress"]

    print(f"[deploy] Deployed: {addresses}")
    return addresses


def configure(addresses: dict):
    """Configure roles and game state after deployment."""
    gold = addresses["Gold"]
    nft = addresses["DungeonNFT"]
    tickets = addresses["DungeonTickets"]
    manager = addresses["DungeonManager"]

    # setMinter on Gold
    print("[configure] Setting Gold.minter → DungeonManager")
    _cast_send(gold, "setMinter(address)", manager)

    # setBurner on Tickets
    print("[configure] Setting Tickets.burner → DungeonManager")
    _cast_send(tickets, "setBurner(address)", manager)

    # setRunner on DungeonManager (deployer = runner for local)
    print("[configure] Setting DungeonManager.runner → deployer")
    _cast_send(manager, "setRunner(address)", DEPLOYER)

    # Mint dungeon NFT #0 (Cave, diff 5, party 2, Common)
    print("[configure] Minting Dungeon NFT #0")
    _cast_send(nft, "mint(address,uint8,uint8,uint8,uint8)", DEPLOYER, "5", "2", "0", "0")

    # Approve + stake dungeon
    print("[configure] Staking dungeon #0")
    _cast_send(nft, "approve(address,uint256)", manager, "0")
    _cast_send(manager, "stakeDungeon(uint256)", "0")

    # Register agents and mint tickets
    for i, agent in enumerate(AGENTS):
        addr = agent["address"]
        print(f"[configure] Registering agent #{i+1}: {addr[:10]}...")
        _cast_send(manager, "registerAgent(address)", addr)
        _cast_send(tickets, "mint(address,uint256)", addr, "10")

    # Also register deployer as agent (for runner calls)
    _cast_send(manager, "registerAgent(address)", DEPLOYER)
    _cast_send(tickets, "mint(address,uint256)", DEPLOYER, "10")

    # Start epoch
    print("[configure] Starting epoch...")
    _cast_send(manager, "startEpoch()")

    print("[configure] ✓ Configuration complete")
    return addresses


def deploy_and_configure() -> dict:
    """Full deployment + configuration. Returns addresses."""
    addresses = deploy()
    configure(addresses)

    # Save to local-deployment.json
    manifest = {
        "environment": "local",
        "chainId": CHAIN_ID,
        "rpc": RPC_URL,
        "deployer": DEPLOYER,
        "contracts": addresses,
        "runner": DEPLOYER,
    }
    manifest_path = os.path.join(PROJECT_DIR, "local-deployment.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[deploy] Manifest saved to {manifest_path}")

    return addresses


if __name__ == "__main__":
    deploy_and_configure()
