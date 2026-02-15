"""Smart preflight checks for running scenarios.

Verifies the entire stack is ready before running a scenario:
- Anvil running and responding
- Contracts deployed and valid
- Gateway healthy
- Epoch active
- Agents registered with tickets
- Dungeon available for the scenario

Can auto-fix some issues (--fix mode).
"""
import json
import os
import subprocess
import sys

import httpx

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEVENV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure devenv is in path for sibling imports
if DEVENV_DIR not in sys.path:
    sys.path.insert(0, DEVENV_DIR)
FOUNDRY_BIN = os.path.expanduser("~/.foundry/bin")
RPC_URL = "http://127.0.0.1:8545"
GATEWAY_URL = "http://127.0.0.1:8000"
DEPLOYMENT_FILE = os.path.join(PROJECT_DIR, "local-deployment.json")

# Anvil account #0
DEPLOYER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class PreflightResult:
    """Result of a single preflight check."""
    def __init__(self, name: str, ok: bool, message: str = "", fixable: bool = False, data: dict = None):
        self.name = name
        self.ok = ok
        self.message = message
        self.fixable = fixable
        self.data = data or {}

    def __repr__(self):
        icon = "✅" if self.ok else ("🔧" if self.fixable else "❌")
        return f"{icon} {self.name}: {self.message}"


def _cast_call(to: str, sig: str, *args) -> str:
    """Run cast call, return stdout or None on failure."""
    cmd = [f"{FOUNDRY_BIN}/cast", "call", "--rpc-url", RPC_URL, to, sig] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _cast_send(to: str, sig: str, *args, private_key: str = DEPLOYER_KEY) -> bool:
    """Run cast send, return success bool."""
    cmd = [f"{FOUNDRY_BIN}/cast", "send", "--json", "--rpc-url", RPC_URL,
           "--private-key", private_key, to, sig] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    return data.get("status") in ("0x1", "1")


def check_anvil() -> PreflightResult:
    """Check if Anvil is running."""
    try:
        result = subprocess.run(
            [f"{FOUNDRY_BIN}/cast", "chain-id", "--rpc-url", RPC_URL],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            chain_id = result.stdout.strip()
            return PreflightResult("Anvil", True, f"Running (chain {chain_id})")
        return PreflightResult("Anvil", False, "Not responding", fixable=True)
    except Exception as e:
        return PreflightResult("Anvil", False, f"Error: {e}", fixable=True)


def check_contracts() -> PreflightResult:
    """Check if contracts are deployed."""
    if not os.path.exists(DEPLOYMENT_FILE):
        return PreflightResult("Contracts", False, "No deployment file", fixable=True)

    try:
        with open(DEPLOYMENT_FILE) as f:
            data = json.load(f)
        contracts = data.get("contracts", {})

        required = ["DungeonManager", "Gold", "DungeonNFT", "DungeonTickets"]
        missing = [c for c in required if c not in contracts]
        if missing:
            return PreflightResult("Contracts", False, f"Missing: {missing}", fixable=True)

        # Verify code exists on-chain
        manager = contracts["DungeonManager"]
        code = _cast_call(manager, "dungeonCount()(uint256)")
        if code is None:
            return PreflightResult("Contracts", False, "Deployed but not responding (stale state?)", fixable=True)

        return PreflightResult("Contracts", True, f"All deployed (Manager: {manager[:10]}...)",
                               data={"contracts": contracts})
    except Exception as e:
        return PreflightResult("Contracts", False, f"Error: {e}", fixable=True)


def check_gateway() -> PreflightResult:
    """Check if gateway is healthy."""
    try:
        resp = httpx.get(f"{GATEWAY_URL}/health", timeout=5)
        if resp.status_code == 200:
            return PreflightResult("Gateway", True, "Healthy")
        return PreflightResult("Gateway", False, f"Status {resp.status_code}", fixable=True)
    except Exception:
        return PreflightResult("Gateway", False, "Not running", fixable=True)


def check_epoch(contracts: dict) -> PreflightResult:
    """Check epoch is active."""
    manager = contracts.get("DungeonManager", "")
    if not manager:
        return PreflightResult("Epoch", False, "No manager address")

    epoch = _cast_call(manager, "currentEpoch()(uint256)")
    state = _cast_call(manager, "epochState()(uint8)")
    if epoch is None or state is None:
        return PreflightResult("Epoch", False, "Cannot read epoch state")

    state_names = {0: "Active", 1: "Grace"}
    state_name = state_names.get(int(state), f"Unknown({state})")

    if int(state) == 0:
        return PreflightResult("Epoch", True, f"Epoch {epoch} ({state_name})",
                               data={"epoch": int(epoch), "state": int(state)})
    return PreflightResult("Epoch", False, f"Epoch {epoch} in {state_name} (need Active)",
                           fixable=True, data={"epoch": int(epoch), "state": int(state)})


def check_dungeons(contracts: dict, required_party_size: int = None) -> PreflightResult:
    """Check available dungeons. Optionally check for specific party size."""
    manager = contracts.get("DungeonManager", "")
    nft = contracts.get("DungeonNFT", "")
    if not manager or not nft:
        return PreflightResult("Dungeons", False, "No contract addresses")

    count_str = _cast_call(manager, "dungeonCount()(uint256)")
    if count_str is None:
        return PreflightResult("Dungeons", False, "Cannot read dungeon count")

    count = int(count_str)
    if count == 0:
        return PreflightResult("Dungeons", False, "No dungeons staked", fixable=True)

    dungeons = []
    for i in range(count):
        # Read dungeon struct
        raw = _cast_call(manager, "dungeons(uint256)(uint256,address,bool,uint256,uint256)", str(i))
        if raw is None:
            continue
        lines = raw.strip().split("\n")
        nft_id = int(lines[0]) if lines else 0
        is_active = lines[2].strip().lower() == "true" if len(lines) > 2 else False

        # Read traits from NFT
        traits_raw = _cast_call(nft, "getTraits(uint256)(uint8,uint8,uint8,uint8)", str(nft_id))
        if traits_raw is None:
            continue
        trait_lines = traits_raw.strip().split("\n")
        difficulty = int(trait_lines[0]) if trait_lines else 0
        party_size = int(trait_lines[1]) if len(trait_lines) > 1 else 0
        theme = int(trait_lines[2]) if len(trait_lines) > 2 else 0
        rarity = int(trait_lines[3]) if len(trait_lines) > 3 else 0

        dungeons.append({
            "id": i,
            "nft_id": nft_id,
            "active": is_active,
            "difficulty": difficulty,
            "party_size": party_size,
            "theme": theme,
            "rarity": rarity,
        })

    if required_party_size:
        matching = [d for d in dungeons if d["party_size"] == required_party_size and d["active"]]
        if not matching:
            all_sizes = sorted(set(d["party_size"] for d in dungeons))
            return PreflightResult(
                "Dungeons", False,
                f"No dungeon with party_size={required_party_size} (available: {all_sizes})",
                fixable=True,
                data={"dungeons": dungeons, "matching": []}
            )
        return PreflightResult(
            "Dungeons", True,
            f"{len(dungeons)} dungeons, {len(matching)} match party_size={required_party_size}",
            data={"dungeons": dungeons, "matching": matching}
        )

    return PreflightResult("Dungeons", True, f"{len(dungeons)} dungeons available",
                           data={"dungeons": dungeons})


def check_agents(contracts: dict, count: int = 5) -> PreflightResult:
    """Check test agents are registered with tickets."""
    from helpers.deploy import AGENTS as AGENT_LIST
    manager = contracts.get("DungeonManager", "")
    tickets = contracts.get("DungeonTickets", "")
    if not manager or not tickets:
        return PreflightResult("Agents", False, "No contract addresses")

    registered = 0
    with_tickets = 0
    issues = []

    for i, agent in enumerate(AGENT_LIST[:count]):
        addr = agent["address"]
        is_reg = _cast_call(manager, "registeredAgents(address)(bool)", addr)
        if is_reg and is_reg.strip().lower() == "true":
            registered += 1
        else:
            issues.append(f"Agent#{i+1} not registered")

        ticket_bal = _cast_call(tickets, "balanceOf(address,uint256)(uint256)", addr, "0")
        if ticket_bal and int(ticket_bal) > 0:
            with_tickets += 1
        else:
            issues.append(f"Agent#{i+1} has no tickets")

    if registered == count and with_tickets == count:
        return PreflightResult("Agents", True, f"{count} registered, all have tickets")
    return PreflightResult("Agents", False,
                           f"{registered}/{count} registered, {with_tickets}/{count} with tickets",
                           fixable=True, data={"issues": issues})


def fix_anvil() -> bool:
    """Start Anvil."""
    print("  🔧 Starting Anvil...")
    state_file = os.path.join(PROJECT_DIR, "anvil-state.json")
    cmd = f"anvil --host 0.0.0.0 --port 8545 --chain-id 31337 --block-time 1 --accounts 10 --balance 10000 --dump-state {state_file}"
    if os.path.exists(state_file):
        cmd += f" --load-state {state_file}"
    subprocess.Popen(cmd.split(), stdout=open(os.path.join(PROJECT_DIR, "anvil.log"), "w"),
                     stderr=subprocess.STDOUT, env={**os.environ, "PATH": f"{FOUNDRY_BIN}:{os.environ.get('PATH', '')}"})
    import time
    for _ in range(15):
        time.sleep(1)
        r = check_anvil()
        if r.ok:
            print("  ✅ Anvil started")
            return True
    print("  ❌ Anvil failed to start")
    return False


def fix_contracts() -> bool:
    """Deploy contracts."""
    print("  🔧 Deploying contracts...")
    try:
        sys.path.insert(0, DEVENV_DIR)
        from helpers.deploy import deploy_and_configure
        deploy_and_configure()
        print("  ✅ Contracts deployed")
        return True
    except Exception as e:
        print(f"  ❌ Deploy failed: {e}")
        return False


def fix_epoch(contracts: dict) -> bool:
    """Start epoch if in grace."""
    manager = contracts["DungeonManager"]
    state = _cast_call(manager, "epochState()(uint8)")
    if state and int(state) == 1:
        print("  🔧 Starting epoch...")
        if _cast_send(manager, "startEpoch()"):
            print("  ✅ Epoch started")
            return True
    print("  ❌ Could not start epoch")
    return False


def fix_agents(contracts: dict, count: int = 5) -> bool:
    """Register agents and mint tickets."""
    from helpers.deploy import AGENTS as AGENT_LIST
    manager = contracts["DungeonManager"]
    tickets = contracts["DungeonTickets"]

    print("  🔧 Fixing agent registration...")
    for i, agent in enumerate(AGENT_LIST[:count]):
        addr = agent["address"]
        is_reg = _cast_call(manager, "registeredAgents(address)(bool)", addr)
        if not is_reg or is_reg.strip().lower() != "true":
            _cast_send(manager, "registerAgent(address)", addr)

        ticket_bal = _cast_call(tickets, "balanceOf(address,uint256)(uint256)", addr, "0")
        if not ticket_bal or int(ticket_bal) < 5:
            _cast_send(tickets, "mint(address,uint256)", addr, "20")

    print("  ✅ Agents fixed")
    return True


def fix_gateway(contracts: dict) -> bool:
    """Start gateway."""
    print("  🔧 Starting gateway...")
    gw_dir = os.path.join(PROJECT_DIR, "gateway")

    env = os.environ.copy()
    env.update({
        "GW_RPC_URL": RPC_URL,
        "GW_CHAIN_ID": "31337",
        "GW_RUNNER_PRIVATE_KEY": DEPLOYER_KEY,
        "GW_DB_PATH": os.path.join(gw_dir, "devenv.db"),
        "GW_JWT_SECRET": "local-dev-secret",
        "GW_DUNGEON_MANAGER": contracts["DungeonManager"],
        "GW_GOLD_CONTRACT": contracts["Gold"],
        "GW_DUNGEON_NFT": contracts["DungeonNFT"],
        "GW_DUNGEON_TICKETS": contracts["DungeonTickets"],
    })

    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=gw_dir, stdout=open(os.path.join(PROJECT_DIR, "gateway.log"), "w"),
        stderr=subprocess.STDOUT, env=env,
    )

    import time
    for _ in range(10):
        time.sleep(1)
        r = check_gateway()
        if r.ok:
            print("  ✅ Gateway started")
            return True
    print("  ❌ Gateway failed to start")
    return False


def run_preflight(scenario: dict = None, auto_fix: bool = False, verbose: bool = True) -> tuple[bool, dict]:
    """Run all preflight checks. Returns (all_ok, context_dict).

    context_dict includes 'dungeon_id' if a matching dungeon was found.
    """
    results = []
    context = {}

    # 1. Anvil
    r = check_anvil()
    if not r.ok and auto_fix:
        if fix_anvil():
            r = check_anvil()
    results.append(r)
    if not r.ok:
        _print_results(results, verbose)
        return False, context

    # 2. Contracts
    r = check_contracts()
    if not r.ok and auto_fix:
        if fix_contracts():
            r = check_contracts()
    results.append(r)
    contracts = r.data.get("contracts", {})
    context["contracts"] = contracts
    if not r.ok:
        _print_results(results, verbose)
        return False, context

    # 3. Epoch
    r = check_epoch(contracts)
    if not r.ok and auto_fix:
        if fix_epoch(contracts):
            r = check_epoch(contracts)
    results.append(r)
    if not r.ok:
        _print_results(results, verbose)
        return False, context

    # 4. Agents
    agent_count = scenario.get("party_size", 5) if scenario else 5
    r = check_agents(contracts, count=agent_count)
    if not r.ok and auto_fix:
        if fix_agents(contracts, count=agent_count):
            r = check_agents(contracts, count=agent_count)
    results.append(r)

    # 5. Dungeons (with party size matching if scenario provided)
    party_size = scenario.get("party_size") if scenario else None
    r = check_dungeons(contracts, required_party_size=party_size)
    if not r.ok and auto_fix and party_size:
        # Try to create a matching dungeon
        print(f"  🔧 Creating dungeon with party_size={party_size}...")
        _create_dungeon(contracts, party_size, scenario.get("difficulty", 5))
        r = check_dungeons(contracts, required_party_size=party_size)
    results.append(r)

    matching = r.data.get("matching", [])
    if matching:
        context["dungeon_id"] = matching[0]["id"]
        context["dungeon"] = matching[0]

    # 6. Gateway
    r = check_gateway()
    if not r.ok and auto_fix:
        if fix_gateway(contracts):
            r = check_gateway()
    results.append(r)

    _print_results(results, verbose)
    all_ok = all(r.ok for r in results)
    return all_ok, context


def _create_dungeon(contracts: dict, party_size: int, difficulty: int = 5):
    """Create and stake a new dungeon NFT with the given party size."""
    manager = contracts["DungeonManager"]
    nft = contracts["DungeonNFT"]

    # Check epoch — need Grace to stake
    state = _cast_call(manager, "epochState()(uint8)")
    was_active = state and int(state) == 0

    if was_active:
        print("    Ending epoch for dungeon staking...")
        _cast_send(manager, "endEpoch()")

    # Get next NFT ID
    next_id = _cast_call(nft, "nextTokenId()(uint256)")
    nft_id = int(next_id) if next_id else 0

    # Mint NFT: mint(address, difficulty, partySize, theme, rarity)
    _cast_send(nft, "mint(address,uint8,uint8,uint8,uint8)",
               DEPLOYER, str(difficulty), str(party_size), "0", "0")

    # Approve + stake
    _cast_send(nft, "approve(address,uint256)", manager, str(nft_id))
    _cast_send(manager, "stakeDungeon(uint256)", str(nft_id))

    if was_active:
        print("    Restarting epoch...")
        _cast_send(manager, "startEpoch()")

    print(f"    Created dungeon NFT#{nft_id} (party={party_size}, diff={difficulty})")


def _print_results(results: list[PreflightResult], verbose: bool):
    if not verbose:
        return
    print("\n┌─────────────────────────────────────┐")
    print("│       🔍 Preflight Checks           │")
    print("├─────────────────────────────────────┤")
    for r in results:
        print(f"│ {r}")
    print("└─────────────────────────────────────┘\n")


if __name__ == "__main__":
    ok, ctx = run_preflight(auto_fix="--fix" in sys.argv)
    if ok:
        print("All checks passed! Ready to run scenarios.")
    else:
        print("Some checks failed. Run with --fix to auto-repair.")
    sys.exit(0 if ok else 1)
