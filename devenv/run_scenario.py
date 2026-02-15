#!/usr/bin/env python3
"""Run a dungeon scenario end-to-end.

Usage:
    python run_scenario.py goblin-cave
    python run_scenario.py --list
    python run_scenario.py goblin-cave --preflight      # check + auto-fix before run
    python run_scenario.py goblin-cave --no-preflight   # skip checks entirely
    python run_scenario.py --dungeons                   # show available dungeons
"""
import argparse
import os
import sys
import time
import yaml

# Add devenv to path
DEVENV_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DEVENV_DIR)

import httpx
import uuid

from agents.player import PlayerAgent
from agents.dm import DMAgent
from helpers.deploy import AGENTS
from helpers.mock_auth import create_mock_jwt, get_moltbook_id
from helpers.preflight import run_preflight
from helpers.dungeon_selector import select_dungeon, print_dungeon_table

SCENARIOS_DIR = os.path.join(DEVENV_DIR, "scenarios")
GATEWAY_URL = "http://127.0.0.1:8000"


def _notify_gateway(address: str, action_type: str, session_id: int = 0,
                     xp: int = 0, gold: int = 0, event_type: str = None,
                     action_text: str = None, dm_actions_json: str = None):
    """Notify gateway about an action for stats/dashboard tracking."""
    moltbook_id = get_moltbook_id(address)
    jwt_token = create_mock_jwt(address)
    headers = {"Authorization": f"Bearer {jwt_token}"}
    action_id = str(uuid.uuid4())[:16]

    try:
        with httpx.Client(timeout=10) as client:
            # Log the action
            payload = {
                "action_id": action_id,
                "session_id": session_id,
                "moltbook_id": moltbook_id,
                "action_type": action_type,
            }
            if action_text:
                payload["action_text"] = action_text
            if dm_actions_json:
                payload["dm_actions_json"] = dm_actions_json
            client.post(f"{GATEWAY_URL}/internal/log-action", headers=headers, json=payload)

            # Award XP/gold if applicable
            if xp > 0 or gold > 0:
                client.post(f"{GATEWAY_URL}/internal/award-xp", headers=headers, json={
                    "idempotency_key": f"{action_type}:{session_id}:{moltbook_id}:{action_id}",
                    "moltbook_id": moltbook_id,
                    "session_id": session_id,
                    "event_type": event_type or action_type,
                    "xp_amount": xp,
                    "gold_amount": gold,
                })
    except Exception as e:
        print(f"    [warn] Gateway notify failed (non-fatal): {e}")


def list_scenarios():
    """List available scenarios."""
    print("Available scenarios:")
    for f in os.listdir(SCENARIOS_DIR):
        if f.endswith(".yaml"):
            with open(os.path.join(SCENARIOS_DIR, f)) as fh:
                data = yaml.safe_load(fh)
            print(f"  {f[:-5]:20s} — {data.get('description', 'No description')}")


def load_scenario(name: str) -> dict:
    """Load a scenario YAML file."""
    path = os.path.join(SCENARIOS_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        print(f"Scenario not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def wait_for_tx(agent, session_id: int, expected_state: int = None,
                timeout: float = 30, poll: float = 2) -> dict:
    """Poll session until state changes or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        info = agent.get_session(session_id)
        if expected_state is not None and info["state"] == expected_state:
            return info
        time.sleep(poll)
    return agent.get_session(session_id)


def run_scenario(scenario_name: str, dungeon_id: int = None,
                  do_preflight: bool = True, auto_fix: bool = True):
    """Run a full scenario."""
    scenario = load_scenario(scenario_name)

    # --- Smart Preflight ---
    if do_preflight:
        print(f"\n🔍 Running preflight for '{scenario_name}'...")
        ok, ctx = run_preflight(scenario=scenario, auto_fix=auto_fix)
        if not ok:
            print("❌ Preflight failed. Fix issues above or run with --no-preflight to skip.")
            sys.exit(1)

        # Use auto-selected dungeon if none specified
        if dungeon_id is None:
            dungeon_id = ctx.get("dungeon_id")
            if dungeon_id is not None:
                dungeon_info = ctx.get("dungeon", {})
                print(f"🎯 Auto-selected dungeon #{dungeon_id} "
                      f"(party={dungeon_info.get('party_size')}, "
                      f"diff={dungeon_info.get('difficulty')})")
    else:
        # Smart dungeon selection without full preflight
        if dungeon_id is None:
            try:
                import json
                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                with open(os.path.join(project_dir, "local-deployment.json")) as f:
                    contracts = json.load(f)["contracts"]
                match = select_dungeon(scenario, contracts)
                if match:
                    dungeon_id = match["id"]
                    print(f"🎯 Auto-selected dungeon #{dungeon_id} (party={match['party_size']})")
            except Exception:
                pass

    if dungeon_id is None:
        dungeon_id = scenario.get("dungeon_id", 0)
        print(f"⚠️  Using fallback dungeon_id={dungeon_id} from scenario config")

    print(f"\n{'='*60}")
    print(f"  🏰 Running Scenario: {scenario['name']}")
    print(f"  📖 {scenario['description']}")
    print(f"  🚪 Dungeon: #{dungeon_id}")
    print(f"{'='*60}\n")

    party_size = scenario.get("party_size", 2)

    # Create agents (use first N Anvil accounts)
    # We create all as potential DM agents (DMAgent extends BaseAgent which has player methods)
    all_agents = []
    for i in range(min(party_size, len(AGENTS))):
        agent_info = AGENTS[i]
        # Create as DMAgent so any of them can be DM
        agent = DMAgent(
            address=agent_info["address"],
            private_key=agent_info["key"],
            name=f"Agent-{i+1}",
        )
        agent.auth()
        all_agents.append(agent)

    print(f"[setup] Created {len(all_agents)} agents")

    # Phase 1: All agents enter dungeon
    print(f"\n--- Phase 1: Entering Dungeon ---")
    session_id = None
    for agent in all_agents:
        sid = agent.enter_dungeon(dungeon_id)
        session_id = sid
        _notify_gateway(agent.address, "enter", session_id, xp=10, event_type="dungeon_enter")

    # After last agent enters, session should transition to WaitingDM
    time.sleep(1)  # Let Anvil process
    info = all_agents[0].get_session(session_id)
    print(f"\n[status] Session {session_id}: state={info['state_name']}, dm={info['dm'][:10]}...")

    if info["state"] != 1:  # Not WaitingDM
        print(f"[error] Expected WaitingDM state, got {info['state_name']}")
        return

    # Phase 2: Identify DM and players
    print(f"\n--- Phase 2: DM Selection ---")
    dm_address = info["dm"]
    dm_agent = None
    player_agents = []

    for agent in all_agents:
        if agent.address.lower() == dm_address.lower():
            dm_agent = agent
            dm_agent.load_scenario(scenario)
            print(f"  [{agent.name}] Selected as DM 🎭")
        else:
            player_agents.append(agent)
            print(f"  [{agent.name}] Player ⚔️")

    if not dm_agent:
        print("[error] Could not find DM agent!")
        return

    # Phase 3: DM accepts
    print(f"\n--- Phase 3: DM Accepts ---")
    dm_agent.accept_dm(session_id)
    _notify_gateway(dm_agent.address, "accept_dm", session_id, xp=75, event_type="dm_hosted")

    time.sleep(1)
    info = dm_agent.get_session(session_id)
    print(f"[status] Session {session_id}: state={info['state_name']}, turn={info['current_turn']}")

    if info["state"] != 2:  # Not Active
        print(f"[error] Expected Active state, got {info['state_name']}")
        return

    # Phase 4: Turn loop
    print(f"\n--- Phase 4: Turn Loop ---")
    max_turns = 20
    last_narrative = scenario["encounters"][0]["text"]  # Initial context

    party = [a.address for a in player_agents]

    for turn in range(1, max_turns + 1):
        info = dm_agent.get_session(session_id)
        state = info["state"]

        if state != 2:  # Not Active
            print(f"\n[end] Session ended: {info['state_name']}")
            break

        current_turn = info["current_turn"]
        current_actor = info["current_actor"]
        print(f"\n  --- Turn {current_turn} ---")
        print(f"  Current actor: {current_actor[:10]}...")

        # Is it a player's turn or DM's turn?
        if current_actor.lower() == dm_agent.address.lower():
            # DM's turn
            print(f"  [{dm_agent.name}] DM responding...")
            narrative, actions = dm_agent.run_encounter(party)
            print(f"  [{dm_agent.name}] Narrative: {narrative[:80]}...")

            # Submit DM response via direct contract call (onlyRunner)
            # Since deployer is the runner, we need to use cast as deployer
            # But the gateway routes through runner... For simplicity, call directly
            _submit_dm_direct(dm_agent, session_id, current_turn, narrative, actions)
            # Notify gateway for DM response + XP/gold for each player action
            import json as _json
            _notify_gateway(dm_agent.address, "dm_response", session_id, xp=25, event_type="dm_response",
                            action_text=narrative,
                            dm_actions_json=_json.dumps(actions))
            for a in actions:
                if a.get("xp_reward", 0) > 0 or a.get("gold_reward", 0) > 0:
                    _notify_gateway(a["target"], "reward", session_id,
                                    xp=a.get("xp_reward", 0), gold=a.get("gold_reward", 0),
                                    event_type="session_complete")

            last_narrative = narrative

            # Check if scenario complete
            if dm_agent.is_scenario_complete():
                time.sleep(1)
                info = dm_agent.get_session(session_id)
                print(f"\n[end] Scenario complete! Final state: {info['state_name']}")
                break
        else:
            # Player's turn — find which player
            for player in player_agents:
                if player.address.lower() == current_actor.lower():
                    # Create a temporary PlayerAgent wrapper for decision
                    p = PlayerAgent.__new__(PlayerAgent)
                    action = p.decide_action({}, last_narrative)
                    print(f"  [{player.name}] Action: {action}")

                    # Submit action via direct contract call (onlyRunner)
                    _submit_action_direct(player, session_id, current_turn, action)
                    _notify_gateway(player.address, "action", session_id, xp=15, event_type="action",
                                    action_text=action)
                    break
            else:
                print(f"  [warn] No agent found for actor {current_actor}")
                break

        time.sleep(1)  # Let Anvil process

    # Summary
    print(f"\n{'='*60}")
    print(f"  📊 Scenario Summary")
    print(f"{'='*60}")
    info = dm_agent.get_session(session_id)
    print(f"  Session ID: {session_id}")
    print(f"  Final State: {info['state_name']}")
    print(f"  Turns Played: {info['current_turn']}")
    print(f"  Gold Pool: {info['gold_pool']}")
    print(f"  DM: {dm_agent.name} ({dm_agent.address[:10]}...)")
    print(f"  Players: {', '.join(p.name for p in player_agents)}")
    print(f"  Encounters: {' → '.join(dm_agent.encounter_history)}")
    print(f"{'='*60}\n")


def _submit_action_direct(agent, session_id: int, turn_index: int, action: str):
    """Submit player action directly via cast (runner calls submitAction)."""
    from helpers.deploy import DEPLOYER_KEY, FOUNDRY_BIN, RPC_URL
    import json, subprocess

    manager = agent.manager_address
    cmd = [
        f"{FOUNDRY_BIN}/cast", "send", "--json",
        "--rpc-url", RPC_URL,
        "--private-key", DEPLOYER_KEY,  # Runner key
        manager,
        "submitAction(uint256,uint256,string,address)",
        str(session_id), str(turn_index), action, agent.address,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] submitAction failed: {result.stderr[:200]}")
        raise RuntimeError(result.stderr)
    data = json.loads(result.stdout)
    if data.get("status") not in ("0x1", "1"):
        print(f"    [error] submitAction reverted: {data}")
        raise RuntimeError(f"submitAction reverted")


def _submit_dm_direct(dm_agent, session_id: int, turn_index: int,
                       narrative: str, actions: list[dict]):
    """Submit DM response directly via cast (runner calls submitDMResponse)."""
    from helpers.deploy import DEPLOYER_KEY, FOUNDRY_BIN, RPC_URL
    import json, subprocess

    manager = dm_agent.manager_address

    # Build DMAction tuples: (actionType, target, value, narrative)
    # DMActionType: NARRATE=0, REWARD_GOLD=1, REWARD_XP=2, DAMAGE=3, KILL_PLAYER=4, COMPLETE=5, FAIL=6
    dm_actions = []
    has_complete = dm_agent.is_scenario_complete()
    enc = dm_agent.get_current_encounter()
    is_fail = enc.get("id") == "fail"

    for a in actions:
        target = a["target"]
        # Gold reward
        if a.get("gold_reward", 0) > 0:
            dm_actions.append((1, target, a["gold_reward"], ""))  # REWARD_GOLD
        # XP reward
        if a.get("xp_reward", 0) > 0:
            dm_actions.append((2, target, a["xp_reward"], ""))  # REWARD_XP
        # Damage
        if a.get("damage", 0) > 0:
            dm_actions.append((3, target, a["damage"], ""))  # DAMAGE
        # Kill
        if a.get("is_killed", False):
            dm_actions.append((4, target, 0, ""))  # KILL_PLAYER

    # Add COMPLETE or FAIL action if scenario is ending
    if has_complete and not is_fail and actions:
        dm_actions.append((5, actions[0]["target"], 0, ""))  # COMPLETE
    elif is_fail and actions:
        dm_actions.append((6, actions[0]["target"], 0, ""))  # FAIL

    # Encode the DMAction[] as a tuple array for cast
    # Format: [(uint8,address,uint256,string),...]
    actions_str = "[" + ",".join(
        f"({a[0]},{a[1]},{a[2]},\"{a[3]}\")" for a in dm_actions
    ) + "]"

    # Use cast calldata to encode properly
    calldata_cmd = [
        f"{FOUNDRY_BIN}/cast", "calldata",
        "submitDMResponse(uint256,uint256,string,(uint8,address,uint256,string)[],address)",
        str(session_id), str(turn_index), narrative, actions_str, dm_agent.address,
    ]
    result = subprocess.run(calldata_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] calldata encoding failed: {result.stderr[:200]}")
        raise RuntimeError(result.stderr)

    calldata = result.stdout.strip()

    # Send raw calldata
    send_cmd = [
        f"{FOUNDRY_BIN}/cast", "send", "--json",
        "--rpc-url", RPC_URL,
        "--private-key", DEPLOYER_KEY,
        manager,
        calldata,
    ]
    result = subprocess.run(send_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] submitDMResponse failed: {result.stderr[:200]}")
        raise RuntimeError(result.stderr)
    data = json.loads(result.stdout)
    if data.get("status") not in ("0x1", "1"):
        print(f"    [error] submitDMResponse reverted: {data}")
        raise RuntimeError(f"submitDMResponse reverted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a dungeon scenario")
    parser.add_argument("scenario", nargs="?", help="Scenario name (e.g., goblin-cave)")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    parser.add_argument("--dungeons", action="store_true", help="Show available dungeons")
    parser.add_argument("--dungeon", type=int, default=None, help="Dungeon ID (auto-selected if omitted)")
    parser.add_argument("--preflight", action="store_true", default=True, help="Run preflight checks (default)")
    parser.add_argument("--no-preflight", action="store_true", help="Skip preflight checks")
    parser.add_argument("--no-fix", action="store_true", help="Check only, don't auto-fix")
    args = parser.parse_args()

    if args.list:
        list_scenarios()
    elif args.dungeons:
        import json as _json
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_dir, "local-deployment.json")) as f:
            contracts = _json.load(f)["contracts"]
        print_dungeon_table(contracts)
    elif args.scenario:
        run_scenario(
            args.scenario,
            dungeon_id=args.dungeon,
            do_preflight=not args.no_preflight,
            auto_fix=not args.no_fix,
        )
    else:
        parser.print_help()
