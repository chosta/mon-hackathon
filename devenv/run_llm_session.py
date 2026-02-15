#!/usr/bin/env python3
"""Run a dungeon session end-to-end using REAL LLM agents via OpenClaw.

This replaces the rule-based PlayerAgent/DMAgent logic with OpenClaw model calls,
while keeping the existing local devenv + contract interaction flow.

By default this targets the local Anvil devenv (same as run_scenario.py).

Usage:
  python devenv/run_llm_session.py --scenario goblin-cave --dungeon 0 --party-size 2

Notes:
- We keep things intentionally simple for the hackathon deadline.
- The DM and each Player are separate OpenClaw *sessions* (session-id isolation).
- The LLMs are instructed using the existing skill files:
    projects/mon-hackathon/skills/dungeon-master.md
    projects/mon-hackathon/skills/player.md
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import yaml
import requests
import jwt as pyjwt  # PyJWT package, imported as pyjwt to avoid conflicts
import random as _random
import google.generativeai as genai
import anthropic

DEVENV_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(DEVENV_DIR)
sys.path.insert(0, DEVENV_DIR)


def _ensure_gateway():
    """Start gateway if not running on port 8000."""
    import socket

    def _port_open():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(("127.0.0.1", 8000))
            sock.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    if _port_open():
        print("[gateway] Already running on :8000")
        return True

    print("[gateway] Not running — starting...")
    gw_dir = os.path.join(PROJECT_DIR, "gateway")
    env = os.environ.copy()
    env["GW_DB_PATH"] = os.path.join(gw_dir, "devenv.db")

    log_file = open(os.path.join(PROJECT_DIR, "gateway.log"), "a")
    subprocess.Popen(
        ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=gw_dir,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    for i in range(15):
        time.sleep(1)
        if _port_open():
            print("[gateway] Started successfully")
            return True

    print("[gateway] WARNING: Failed to start within 15s — continuing anyway")
    return False

# # Feature flag: use SessionManager for persistent, reusable LLM sessions
# USE_PERSISTENT_SESSIONS = os.environ.get("USE_PERSISTENT_SESSIONS", "0") == "1"
USE_PERSISTENT_SESSIONS = False  # Disabled: using direct Anthropic SDK now

from helpers.deploy import AGENTS
from agents.dm import DMAgent  # wallet + accept_dm + get_session; logic ignored
from agents.base import BaseAgent

# if USE_PERSISTENT_SESSIONS:
#     from session_manager import SessionManager

SCENARIOS_DIR = os.path.join(DEVENV_DIR, "scenarios")


def check_anvil_health() -> bool:
    """Check if Anvil is responsive before proceeding."""
    try:
        cast_path = os.path.expanduser("~/.foundry/bin/cast")
        subprocess.check_output(
            [cast_path, "chain-id", "--rpc-url", "http://127.0.0.1:8545"],
            timeout=5, stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False

# Gateway logging for dashboard visibility
GATEWAY_URL = "http://localhost:8000"
_JWT_SECRET = os.environ.get("GW_JWT_SECRET", "local-dev-secret")  # matches gateway dev config


def _make_dev_jwt(moltbook_id: str) -> str:
    """Create a dev JWT for internal gateway calls."""
    return pyjwt.encode(
        {"sub": moltbook_id, "name": moltbook_id, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        _JWT_SECRET, algorithm="HS256",
    )


def _log_to_gateway(moltbook_id: str, action_type: str, session_id: int = 0,
                     action_text: str = None, dm_actions: list = None):
    """Log action to gateway for dashboard visibility."""
    try:
        token = _make_dev_jwt(moltbook_id)
        payload = {
            "action_id": uuid.uuid4().hex,
            "session_id": session_id,
            "moltbook_id": moltbook_id,
            "action_type": action_type,
        }
        if action_text is not None:
            payload["action_text"] = action_text
        if dm_actions is not None:
            payload["dm_actions_json"] = json.dumps(dm_actions)

        requests.post(
            f"{GATEWAY_URL}/internal/log-action",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=2,
        )
    except Exception as e:
        print(f"[LOG] Gateway log error: {e}")  # Don't fail the run if logging fails

def _award_xp_to_gateway(session_id: int, turn: int, actions: list):
    """Award XP/gold to gateway for leaderboard tracking."""
    for action in actions:
        target = action.get("target", "").lower()
        gold = action.get("gold_reward", 0)
        xp = action.get("xp_reward", 0)
        if gold <= 0 and xp <= 0:
            continue

        # Idempotent event ID
        event_id = f"turn:{session_id}:{turn}:{target}"

        try:
            token = _make_dev_jwt(target)
            requests.post(
                f"{GATEWAY_URL}/internal/award-xp",
                json={
                    "idempotency_key": event_id,
                    "moltbook_id": target,
                    "session_id": session_id,
                    "xp_amount": xp,
                    "gold_amount": gold,
                    "event_type": "game_reward",
                    "source": f"turn_{turn}",
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=2,
            )
        except Exception as e:
            print(f"[XP] Award error: {e}")


SKILLS_DIR = os.path.join(PROJECT_DIR, "skills")
DM_SKILL_PATH = os.path.join(SKILLS_DIR, "dungeon-master.md")
PLAYER_SKILL_PATH = os.path.join(SKILLS_DIR, "player.md")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --- Direct Gemini SDK (stateless, no session accumulation) ---
_gemini_models = {}

def _get_gemini_model(model_name: str = "gemini-2.0-flash"):
    if model_name not in _gemini_models:
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY env var required")
        genai.configure(api_key=gemini_key)
        _gemini_models[model_name] = genai.GenerativeModel(model_name)
    return _gemini_models[model_name]

def _gemini_turn(system_prompt: str, user_message: str, model_name: str = "gemini-2.0-flash", max_retries: int = 3) -> str:
    """Stateless Gemini call. No session, no accumulation."""
    model = _get_gemini_model(model_name)
    full_prompt = f"{system_prompt}\n\n---\n\n{user_message}"
    for attempt in range(max_retries):
        try:
            response = model.generate_content(full_prompt)
            return response.text.strip()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"[LLM] Gemini attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

# Keep old name as alias for compatibility
def _direct_llm_turn(system_prompt: str, user_message: str, max_retries: int = 3) -> str:
    return _gemini_turn(system_prompt, user_message, "gemini-2.0-flash", max_retries)

# --- Direct Anthropic SDK (stateless) ---
_anthropic_client = None

# Set Anthropic API key if not already set
if not os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError("ANTHROPIC_API_KEY env var required")

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client

def _anthropic_turn(system_prompt: str, user_message: str, model: str = "claude-sonnet-4-20250514", max_retries: int = 3) -> str:
    """Stateless Anthropic call."""
    client = _get_anthropic_client()
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text.strip()
        except (anthropic.RateLimitError, anthropic.APITimeoutError) as e:
            if attempt == max_retries - 1:
                raise
            print(f"[LLM] Anthropic retry {attempt+1}: {e}")
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"[LLM] Anthropic error {attempt+1}: {e}")
            time.sleep(1)

# --- Provider registry ---
PROVIDERS = {
    "gemini-flash": lambda sys, msg: _gemini_turn(sys, msg, "gemini-2.0-flash"),
    "gemini-pro": lambda sys, msg: _gemini_turn(sys, msg, "gemini-2.5-pro-preview-05-06"),
    "sonnet": lambda sys, msg: _anthropic_turn(sys, msg, "claude-sonnet-4-20250514"),
    "haiku": lambda sys, msg: _anthropic_turn(sys, msg, "claude-haiku-4-20250414"),
}
ALL_MODELS = list(PROVIDERS.keys())

def _resolve_model(flag_value: str) -> str:
    if flag_value == "random":
        return _random.choice(ALL_MODELS)
    return flag_value

# def _openclaw_turn(session_id: str, message: str, thinking: str = "minimal", timeout_s: int = 180, use_clean_agent: bool = True) -> str:
#     """Run one OpenClaw agent turn and return the text payload.
#     
#     Args:
#         use_clean_agent: If True, use the 'mon-game' agent (no Clawdbot identity).
#                         If False, use default agent (has identity bleed issues).
#     """
#     cmd = [
#         os.path.expanduser("~/.npm-global/bin/openclaw"),
#         "agent",
#         "--json",
#         "--thinking",
#         thinking,
#         "--timeout",
#         str(timeout_s),
#         "--session-id",
#         session_id,
#     ]
#     # Use mon-game agent (configured for Gemini in openclaw.json)
#     if use_clean_agent:
#         cmd.extend(["--agent", "mon-game"])
#     cmd.extend(["-m", message])
#     out = subprocess.check_output(cmd, text=True)
#     data = json.loads(out)
#     payloads = data.get("payloads") or data.get("result", {}).get("payloads")  # compat
#     if payloads:
#         text = payloads[0].get("text")
#         if isinstance(text, str):
#             return text.strip()
#     # Fallback: agent may have used tools or returned empty payloads
#     # Try to extract any text from the result
#     result = data.get("result", {})
#     if isinstance(result, dict):
#         for p in result.get("payloads", []):
#             if isinstance(p.get("text"), str):
#                 return p["text"].strip()
#     raise RuntimeError(f"OpenClaw returned no text payloads: session_id={session_id}, "
#                        f"status={data.get('status')}, summary={data.get('summary')}")


def load_scenario(name: str) -> dict:
    path = os.path.join(SCENARIOS_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        raise SystemExit(f"Scenario not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _submit_action_direct(agent: BaseAgent, session_id: int, turn_index: int, action: str):
    """Submit player action directly via cast (runner calls submitAction)."""
    from helpers.deploy import DEPLOYER_KEY, FOUNDRY_BIN, RPC_URL

    manager = agent.manager_address
    # Strip any GAMESTATE blocks the player might have accidentally included
    import re
    action = re.sub(r'---GAMESTATE---.*?---END---', '', action, flags=re.DOTALL).strip()
    # Sanitize: cast parser chokes on quotes and special chars
    safe_action = action.replace('\\', '').replace('"', '').replace("'", '').strip()
    if not safe_action:
        safe_action = "I take action."
    cmd = [
        f"{FOUNDRY_BIN}/cast",
        "send",
        "--json",
        "--rpc-url",
        RPC_URL,
        "--private-key",
        DEPLOYER_KEY,  # Runner key (local)
        manager,
        "submitAction(uint256,uint256,string,address)",
        str(session_id),
        str(turn_index),
        safe_action,
        agent.address,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"submitAction failed: {result.stderr[:300]}")
    data = json.loads(result.stdout)
    if data.get("status") not in ("0x1", "1"):
        raise RuntimeError(f"submitAction reverted: {data}")


def _submit_dm_direct(dm_agent: BaseAgent, session_id: int, turn_index: int, narrative: str, actions: list[dict], is_complete: bool = False, is_failed: bool = False):
    """Submit DM response directly via cast (runner calls submitDMResponse)."""
    from helpers.deploy import DEPLOYER_KEY, FOUNDRY_BIN, RPC_URL

    manager = dm_agent.manager_address

    # Convert to the DMAction tuples expected by the on-chain DM handler.
    # DMActionType: NARRATE=0, REWARD_GOLD=1, REWARD_XP=2, DAMAGE=3, KILL_PLAYER=4, COMPLETE=5, FAIL=6
    dm_actions = []
    for a in actions:
        target = a["target"]
        if a.get("gold_reward", 0) > 0:
            dm_actions.append((1, target, int(a["gold_reward"]), ""))
        if a.get("xp_reward", 0) > 0:
            dm_actions.append((2, target, int(a["xp_reward"]), ""))
        if a.get("damage", 0) > 0:
            dm_actions.append((3, target, int(a["damage"]), ""))
        if a.get("is_killed", False):
            dm_actions.append((4, target, 0, ""))
    
    # Add COMPLETE or FAIL action if flagged
    if is_complete:
        # COMPLETE=5, use zero address as target, narrative as recap
        dm_actions.append((5, "0x0000000000000000000000000000000000000000", 0, ""))
    if is_failed:
        # FAIL=6
        dm_actions.append((6, "0x0000000000000000000000000000000000000000", 0, ""))

    # Encode DMAction[] via cast calldata to avoid tuple encoding footguns.
    actions_str = "[" + ",".join(
        f"({t[0]},{t[1]},{t[2]},\"{t[3]}\")" for t in dm_actions
    ) + "]"

    # Sanitize narrative for cast parser
    safe_narrative = narrative.replace('"', "'").replace('\\', '').strip()
    if not safe_narrative:
        safe_narrative = "The adventure continues."

    calldata_cmd = [
        f"{FOUNDRY_BIN}/cast",
        "calldata",
        "submitDMResponse(uint256,uint256,string,(uint8,address,uint256,string)[],address)",
        str(session_id),
        str(turn_index),
        safe_narrative,
        actions_str,
        dm_agent.address,
    ]
    res = subprocess.run(calldata_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"calldata encoding failed: {res.stderr[:300]}")
    calldata = res.stdout.strip()

    send_cmd = [
        f"{FOUNDRY_BIN}/cast",
        "send",
        "--json",
        "--rpc-url",
        RPC_URL,
        "--private-key",
        DEPLOYER_KEY,
        manager,
        calldata,
    ]
    res2 = subprocess.run(send_cmd, capture_output=True, text=True)
    if res2.returncode != 0:
        raise RuntimeError(f"submitDMResponse failed: {res2.stderr[:300]}")
    data = json.loads(res2.stdout)
    if data.get("status") not in ("0x1", "1"):
        raise RuntimeError(f"submitDMResponse reverted: {data}")


# def _mk_session_id(prefix: str) -> str:
#     return f"mon-hack:{prefix}:{uuid.uuid4().hex[:10]}"


def run_llm_session(scenario_name: str, dungeon_id: int = 0, party_size: int = 2, thinking: str = "minimal", existing_session: int = None, model: str = "gemini-flash", dm_model: str = None, player_model: str = None):
    _ensure_gateway()
    scenario = load_scenario(scenario_name)

    # Load skill files. GAMESTATE instructions are critical and appear after 1000 chars.
    # We need to include enough content to cover the GAMESTATE block format (section 3).
    dm_rules_full = _read(DM_SKILL_PATH)
    player_rules_full = _read(PLAYER_SKILL_PATH)
    # Send full rules — stateless LLM calls have no accumulation issues
    dm_rules = dm_rules_full
    player_rules = player_rules_full

    # Create N agents using local anvil keys (same as run_scenario.py).
    all_agents: list[BaseAgent] = []
    for i in range(min(party_size, len(AGENTS))):
        info = AGENTS[i]
        agent = DMAgent(address=info["address"], private_key=info["key"], name=f"Agent-{i+1}")
        agent.auth()
        all_agents.append(agent)

    print(f"[setup] Created {len(all_agents)} wallet-agents")

    if existing_session is not None:
        # Skip enter phase — use an already-populated session
        session_id = existing_session
        print(f"\n--- Phase 1: Using existing session {session_id} ---")
        info = all_agents[0].get_session(session_id)
    else:
        # Phase 1: enter dungeon (determines DM address on-chain)
        session_ids = []
        print("\n--- Phase 1: Entering Dungeon ---")
        for a in all_agents:
            sid = a.enter_dungeon(dungeon_id)
            session_ids.append(sid)
            print(f"  {a.name} ({a.address[:10]}...) → session {sid}")
            _log_to_gateway(a.address.lower(), "enter", sid)

        time.sleep(1)

        # Verify all agents ended up in the same session
        unique_sessions = set(session_ids)
        if len(unique_sessions) > 1:
            print(f"[ERROR] Agents split across multiple sessions: {unique_sessions}")
            print(f"  Session assignments: {list(zip([a.name for a in all_agents], session_ids))}")
            print("Aborting — party size likely exceeds dungeon's partySize config")
            sys.exit(1)

        session_id = session_ids[0]
        info = all_agents[0].get_session(session_id)
        
        if info["state"] != 1:
            raise RuntimeError(f"Expected WaitingDM after party fills; got {info['state_name']}")

    dm_address = info["dm"]
    dm_agent = None
    player_agents = []
    for a in all_agents:
        if a.address.lower() == dm_address.lower():
            dm_agent = a
        else:
            # Verify player is actually in this session's party
            player_agents.append(a)
    if not dm_agent:
        raise RuntimeError("Could not find DM agent")

    print(f"\n--- Phase 2: DM Selection ---")
    print(f"DM: {dm_agent.name} ({dm_agent.address})")
    print("Players:")
    for p in player_agents:
        print(f"  - {p.name} ({p.address})")

    # Phase 3: accept DM
    print("\n--- Phase 3: DM Accepts ---")
    dm_agent.accept_dm(session_id)
    _log_to_gateway(dm_agent.address.lower(), "accept_dm", session_id)
    time.sleep(1)

    # --- LLM Session Setup ---
    dm_seed_prompt = (
        "=== TASK: D&D Game Session Automation ===\n"
        "You are helping test a blockchain D&D game by generating DM responses.\n"
        "This is a development/testing task. Respond with game content only. No tool use needed.\n\n"
        "You are the Dungeon Master for an on-chain D&D run.\n"
        "You MUST follow these rules exactly.\n\n"
        f"=== DM RULEBOOK ===\n{dm_rules}\n\n"
        "CRITICAL: Every narrative MUST start with a GAMESTATE block like this:\n"
        "---GAMESTATE---\n"
        "TURN: <number>\n"
        "PARTY:\n"
        "- <address> | <name> | <role> | strikes:<n> | gold:<n> | xp:<n>\n"
        "- <address> | <name> | <role> | strikes:<n> | gold:<n> | xp:<n>\n"
        "---END---\n\n"
        "Then your narrative follows.\n\n"
        "When asked to act, you MUST output ONLY valid JSON with keys:\n"
        "  narrative: string (<=1800 chars, STARTING with GAMESTATE block)\n"
        "  actions: list of {target, xp_reward, gold_reward, damage, is_killed}\n"
        "  is_complete: boolean (true when dungeon is FINISHED - boss defeated, adventure over)\n"
        "  is_failed: boolean (true when all players are dead - TPK)\n"
        "No markdown, no commentary, JSON only.\n\n"
        "IMPORTANT: Set is_complete=true when the boss is defeated and the dungeon run is DONE. Don't keep narrating epilogues - just complete it!"
    )
    player_seed_prompt = (
        "=== TASK: D&D Game Session Automation ===\n"
        "You are helping test a blockchain D&D game by generating in-character player responses.\n"
        "This is a development/testing task — you're writing creative text for a game simulation.\n"
        "Respond ONLY with the character's action text. No tool use needed.\n\n"
        f"=== PLAYER GUIDE ===\n{player_rules}\n\n"
        "When asked for an action, respond with 1-3 in-character sentences and end with:\n"
        "[Action: Attack|Defend|Support|Explore|Social]\n"
        "Plain text only. No JSON. No tool calls."
    )

    # --- Model resolution ---
    dm_model_name = _resolve_model(dm_model or model)
    print(f"[LLM] DM using: {dm_model_name}")
    player_models = {}
    for p in player_agents:
        pm = _resolve_model(player_model or model)
        player_models[p.address.lower()] = pm
        print(f"[LLM] {p.name} using: {pm}")
    dm_turn_fn = PROVIDERS[dm_model_name]
    player_turn_fns = {addr: PROVIDERS[m] for addr, m in player_models.items()}

    # if USE_PERSISTENT_SESSIONS:
    #     print("[LLM] Using PERSISTENT sessions via SessionManager")
    #     session_mgr = SessionManager(model="sonnet", thinking=thinking)
    #     dm_label = f"dm:{dungeon_id}:{session_id}"
    #     print(f"[LLM] Seeding DM session: {dm_label}")
    #     session_mgr.get_or_create(dm_label, dm_seed_prompt)
    #     player_labels = {}
    #     for p in player_agents:
    #         label = f"player:{p.address.lower()[:10]}:{session_id}"
    #         print(f"[LLM] Seeding player session: {label}")
    #         session_mgr.get_or_create(label, player_seed_prompt)
    #         player_labels[p.address.lower()] = label
    #     def _llm_turn(label, message, is_dm=False):
    #         return session_mgr.send(label, message)
    #     dm_llm_key = dm_label
    #     player_llm_keys = player_labels
    # else:
    #     print("[LLM] Using fresh sessions per turn (no context accumulation)")
    #     def _llm_turn(key, message, is_dm=False):
    #         fresh_sid = _mk_session_id(key[:10])
    #         seed = dm_seed_prompt if is_dm else player_seed_prompt
    #         full_message = f"{seed}\n\n---\n\nCURRENT SITUATION:\n{message}"
    #         return _openclaw_turn(fresh_sid, full_message, thinking=thinking)
    #     dm_llm_key = "dm"
    #     player_llm_keys = {p.address.lower(): f"p{i}" for i, p in enumerate(player_agents, start=1)}

    # Phase 4: turn loop
    print("\n--- Phase 4: Turn Loop ---")
    _log_to_gateway(dm_agent.address.lower(), "session_start", session_id,
                    action_text=f"🏰 Dungeon session started — {scenario.get('name', 'unknown')}, difficulty {scenario.get('difficulty', '?')}, party size {len(player_agents)}")
    last_narrative = scenario["encounters"][0]["text"]
    # Note: Don't log dm_scene separately — Turn 1's dm_response IS the opening scene
    last_player_actions: dict[str, str] = {}  # addr -> last action text
    max_turns = 30  # Support 5-player parties getting 5 full game turns (5 players x 5 turns + DM)

    # Before the turn loop, add:
    session_needs_cleanup = True

    try:
        for _ in range(max_turns):
            if not check_anvil_health():
                print("[ERROR] Anvil is not responding! Aborting session gracefully.")
                break

            info = dm_agent.get_session(session_id)
            if info["state"] != 2:  # Not Active anymore
                print(f"[end] Session ended: {info['state_name']}")
                session_needs_cleanup = False
                break

            turn = info["current_turn"]
            actor = (info["current_actor"] or "").lower()
            print(f"\n--- Turn {turn} ---")
            print(f"Actor: {actor[:10]}...")

            if actor == dm_agent.address.lower():
                # Gather all party addresses (players only)
                party = [p.address for p in player_agents]
                player_actions_text = ""
                if last_player_actions:
                    player_actions_text = "Player actions since last narrative:\n"
                    for addr, act_text in last_player_actions.items():
                        player_actions_text += f"  {addr}: {act_text}\n"
                    player_actions_text += "\n"

                # Fix 4: Pre-generate dice rolls for each player (Turn 2+)
                import random
                dice_rolls = {}
                dice_text = ""
                if turn > 1:
                    for p in player_agents:
                        dice_rolls[p.address.lower()] = random.randint(1, 20)
                    dice_text = "=== PRE-ROLLED DICE (MANDATORY — USE THESE EXACT VALUES) ===\n"
                    for addr, roll in dice_rolls.items():
                        name = next((p.name for p in player_agents if p.address.lower() == addr), addr[:10])
                        dice_text += f"  {name}: d20 = {roll}\n"
                    dice_text += "You MUST use these exact d20 values. Do NOT generate your own rolls.\n"
                    dice_text += "Report as: [d20: X] vs DC Y → TIER (where X is the pre-rolled value above)\n\n"

                # Determine encounter type based on turn number
                if turn == 1:
                    encounter_type = "SCENE SETUP"
                    encounter_desc = "Set the scene. Introduce characters, describe the environment, present 2-3 choices. NO combat, NO dice."
                elif turn == 2:
                    encounter_type = _random.choice(["MINOR ENEMIES", "PUZZLE"])
                    if encounter_type == "MINOR ENEMIES":
                        encounter_desc = "The party encounters minor enemies (scouts, small creatures, weak guards). Easy fight to warm up."
                    else:
                        encounter_desc = "The party faces a puzzle or mystery. A riddle, a locked door, a strange mechanism. Reward clever thinking."
                elif turn == 3:
                    encounter_type = _random.choice(["TRAP", "EXPLORATION"])
                    if encounter_type == "TRAP":
                        encounter_desc = "A dangerous trap! Pit, poison darts, collapsing ceiling, or magical ward. High stakes, requires skill checks."
                    else:
                        encounter_desc = "Exploration and discovery. Hidden rooms, ancient artifacts, environmental storytelling. May find treasure or lore."
                elif turn == 4:
                    encounter_type = _random.choice(["HARD ENCOUNTER", "DEADLY TRAP"])
                    if encounter_type == "HARD ENCOUNTER":
                        encounter_desc = "Elite enemies or a mini-boss. This should be HARD — multiple tough foes, high DCs. Players should fear for their lives."
                    else:
                        encounter_desc = "A deadly trap that could end the journey. Lava, crushing walls, magical curse. Real chance of death if they fail."
                else:  # turn >= 5
                    encounter_type = "BOSS"
                    encounter_desc = "THE FINAL BOSS. The dungeon's ultimate challenge. Epic, dramatic, deadly. This ends the dungeon one way or another."

                print(f"[Turn {turn}] Encounter: {encounter_type}")

                # Build turn-specific enforcement prompts
                turn_enforcement = (
                    f"=== ENCOUNTER: {encounter_type} ===\n"
                    f"{encounter_desc}\n\n"
                )
                if turn == 1:
                    turn_enforcement += (
                        "=== TURN 1 RULES (MANDATORY) ===\n"
                        "THIS IS TURN 1: Pure narrative ONLY. Do NOT roll dice. Set the scene, introduce characters, present 2-3 choices.\n"
                        "No gold or XP awards on Turn 1. actions[] should have gold_reward=0, xp_reward=0 for all players.\n\n"
                    )
                elif turn >= 5:
                    turn_enforcement += (
                        "=== FINAL TURN (MANDATORY) ===\n"
                        "⚠️ THIS IS YOUR FINAL TURN. You MUST resolve the dungeon NOW.\n"
                        "Set is_complete: true (if party survives) or is_failed: true (if TPK/all fled).\n"
                        "Do NOT leave the session open. The adventure ENDS this turn.\n\n"
                        "Write an EPIC closing narrative (target 900-1200 chars):\n"
                        "1. Resolve the final encounter dramatically\n"
                        "2. End with RECAP: 2-4 sentences summarizing the ENTIRE adventure — key moments, deaths, victories, the journey from start to finish.\n"
                        "The RECAP should read like a tale told at a tavern.\n\n"
                    )

                dm_prompt = (
                    f"Session {session_id} — **Turn {turn}/5**\n"
                    f"Dungeon: difficulty={scenario.get('difficulty')} theme={scenario.get('name')}\n\n"
                    "=== REMINDERS ===\n"
                    "Gold ONLY on kills/treasure. Narrative: target 700-900 chars, max 1200. Be vivid and dramatic.\n"
                    "GAMESTATE gold MUST be CUMULATIVE — add gold_reward from this turn to previous gold value. If player had gold:20 and earns 30, write gold:50.\n\n"
                    f"{turn_enforcement}"
                    f"{dice_text}"
                    f"Last narrative:\n{last_narrative[:400]}\n\n"
                    f"{player_actions_text}"
                    f"Party: " + ", ".join(party) + "\n\n"
                    "=== DICE BINDING (MANDATORY) ===\n"
                    "Report rolls as: [d20: X] vs DC Y → TIER\n"
                    "Tiers: 1=crit-fail, 2-7=fail, 8-13=partial, 14-19=success, 20=crit-success\n"
                    "Gold ONLY on kills/treasure: minor=15-30, major=30-50, boss=50-100. XP on EVERY roll: fail=5, partial=10, success=15, crit=25\n\n"
                    "=== TURN BUDGET ===\n"
                    f"This is turn {turn} of 5.\n\n"
                    "=== BREVITY ===\n"
                    "Narrative: target 700-900 chars, max 1200. Be vivid and dramatic. Include roll/DC/tier/gold.\n\n"
                    "⚠️ You MUST include an action entry for EACH player in the party.\n"
                    "Even if no gold, include: {\"target\": \"0xADDR\", \"gold_reward\": 0, \"xp_reward\": 5, ...}\n\n"
                    "Caps: gold_reward <= 100, xp_reward <= 50 per action.\n\n"
                    "Output ONLY valid JSON (gold and xp in GAMESTATE must be CUMULATIVE totals, not this turn only):\n"
                    '{"narrative": "---GAMESTATE---\\nTURN: ' + str(turn) + '\\nPARTY:\\n- 0xABC | Name | Role | strikes:0 | gold:50 | xp:35\\n---END---\\n\\n'
                    '[d20: 14] vs DC 12 → SUCCESS. Hero strikes true...", '
                    '"actions": [{"target": "0xABC", "gold_reward": 30, "xp_reward": 15, "damage": 0, "is_killed": false}], '
                    '"is_complete": false, "is_failed": false}'
                )

                # Try up to 3 times for valid JSON
                dm_out = None
                for attempt in range(3):
                    raw = dm_turn_fn(dm_seed_prompt, dm_prompt if attempt == 0 else
                        "Your previous response was not valid JSON. Output ONLY a JSON object with keys: narrative (string), actions (list). No markdown, no explanation, JUST JSON.")
                    cleaned = raw.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                    cleaned = cleaned.strip()
                    try:
                        dm_out = json.loads(cleaned)
                        break
                    except Exception as e:
                        print(f"[DM] JSON parse attempt {attempt+1} failed: {e}")
                        print(f"[DM] Raw: {raw[:200]}")
                        if attempt == 2:
                            # Fallback: generate a simple narrative
                            print("[DM] Using fallback narrative")
                            dm_out = {"narrative": "The adventure continues deeper into the cave...", "actions": [
                                {"target": p.address, "xp_reward": 5, "gold_reward": 5, "damage": 0, "is_killed": False}
                                for p in player_agents
                            ]}

                narrative = dm_out["narrative"]
                actions = dm_out.get("actions", [])
                is_complete = bool(dm_out.get("is_complete", False))
                is_failed = bool(dm_out.get("is_failed", False))
                last_player_actions.clear()

                # Fix 4: Validate DM used correct dice rolls (audit only, don't block)
                if turn > 1 and dice_rolls:
                    import re
                    reported_rolls = re.findall(r'\[d20:\s*(\d+)\]', narrative)
                    expected_rolls = list(dice_rolls.values())
                    for r in reported_rolls:
                        if int(r) not in expected_rolls:
                            print(f"[DICE AUDIT] ⚠️ DM reported roll {r} but expected one of {expected_rolls}")

                narrative = (narrative or "").strip()[:1800]  # Leave room for encoding overhead
                # Minimal validation/coercion
                fixed_actions = []
                for a in actions:
                    if not isinstance(a, dict) or "target" not in a:
                        continue
                    fixed_actions.append({
                        "target": a["target"],
                        "xp_reward": int(a.get("xp_reward", 0) or 0),
                        "gold_reward": int(a.get("gold_reward", 0) or 0),
                        "damage": int(a.get("damage", 0) or 0),
                        "is_killed": bool(a.get("is_killed", False)),
                    })

                # Force completion on turn 5
                if turn >= 5 and not (is_complete or is_failed):
                    print(f"[FORCE] Turn 5 reached, forcing completion")
                    is_complete = True

                if is_complete:
                    print(f"[DM] 🎉 DUNGEON COMPLETE!")
                if is_failed:
                    print(f"[DM] 💀 DUNGEON FAILED!")
                print(f"[DM] Narrative: {narrative[:120]}{'...' if len(narrative) > 120 else ''}")
                print(f"[DM] Actions ({len(fixed_actions)}): {json.dumps(fixed_actions)}")
                _submit_dm_direct(dm_agent, session_id, turn, narrative, fixed_actions, is_complete=is_complete, is_failed=is_failed)
                _log_to_gateway(dm_agent.address.lower(), "dm_response", session_id,
                                action_text=narrative, dm_actions=fixed_actions)

                if is_complete:
                    _log_to_gateway(dm_agent.address.lower(), "session_complete", session_id,
                                    action_text=f"✅ Dungeon complete! {narrative[-300:]}")
                if is_failed:
                    _log_to_gateway(dm_agent.address.lower(), "session_failed", session_id,
                                    action_text=f"💀 Dungeon failed! {narrative[-300:]}")

                # Fix 3: Award session completion to all participants for leaderboard tracking
                if is_complete or is_failed:
                    for agent in [dm_agent] + player_agents:
                        event_id = f"session_complete:{session_id}:{agent.address.lower()}"
                        try:
                            token = _make_dev_jwt(agent.address.lower())
                            requests.post(
                                f"{GATEWAY_URL}/internal/award-xp",
                                json={
                                    "idempotency_key": event_id,
                                    "moltbook_id": agent.address.lower(),
                                    "session_id": session_id,
                                    "xp_amount": 0,
                                    "gold_amount": 0,
                                    "event_type": "session_complete",
                                    "source": "session_end",
                                },
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=2,
                            )
                        except Exception as e:
                            print(f"[SESSION] Count error: {e}")

                # Award XP/gold to gateway for leaderboard
                _award_xp_to_gateway(session_id, turn, fixed_actions)

                last_narrative = narrative

            else:
                # Player turn
                player = None
                for p in player_agents:
                    if p.address.lower() == actor:
                        player = p
                        break
                if not player:
                    raise RuntimeError(f"No player found for actor {actor}")

                p_prompt = (
                    f"Session {session_id} Turn {turn}.\n"
                    f"DM narrative:\n{last_narrative}\n\n"
                    "Decide your action now. End with an [Action: ...] tag."
                )
                action = player_turn_fns[player.address.lower()](player_seed_prompt, p_prompt)
                if "[Action:" not in action:
                    action = action.strip() + " [Action: Defend]"
                action = action.strip()[:500]
                print(f"[{player.name}] {action}")
                last_player_actions[player.address] = action
                _submit_action_direct(player, session_id, turn, action)
                _log_to_gateway(player.address.lower(), "action", session_id, action_text=action)

            time.sleep(1)

        # If we exit normally after max_turns
        print(f"\n--- Max turns ({max_turns}) reached ---")
        
    finally:
        # Cleanup: if session is still Active, timeout it
        if session_needs_cleanup:
            try:
                info = dm_agent.get_session(session_id)
                if info["state"] == 2:  # Active
                    print(f"[cleanup] Session {session_id} still Active, calling timeoutSession...")
                    # Call timeoutSession via cast
                    from helpers.deploy import DEPLOYER_KEY, FOUNDRY_BIN, RPC_URL
                    cleanup_cmd = [
                        f"{FOUNDRY_BIN}/cast", "send", "--json",
                        "--rpc-url", RPC_URL,
                        "--private-key", DEPLOYER_KEY,
                        dm_agent.manager_address,
                        "timeoutSession(uint256)",
                        str(session_id),
                    ]
                    subprocess.run(cleanup_cmd, capture_output=True)
                    print(f"[cleanup] Session {session_id} timed out")
            except Exception as e:
                print(f"[cleanup] Warning: cleanup failed: {e}")

    # Final state
    info = dm_agent.get_session(session_id)
    print("\n--- Summary ---")
    print(f"Session: {session_id}")
    print(f"State: {info['state_name']}")
    print(f"Turn: {info['current_turn']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="goblin-cave")
    ap.add_argument("--dungeon", type=int, default=0)
    ap.add_argument("--party-size", type=int, default=2)
    ap.add_argument("--thinking", default="off", choices=["off", "minimal", "low", "medium", "high"])
    ap.add_argument("--session", type=int, default=None, help="Existing session ID (skip enter phase)")
    ap.add_argument("--model", default="gemini-flash", choices=ALL_MODELS + ["random"],
                    help="Default model for all agents")
    ap.add_argument("--dm-model", default=None, choices=ALL_MODELS + ["random"],
                    help="Model for DM (overrides --model)")
    ap.add_argument("--player-model", default=None, choices=ALL_MODELS + ["random"],
                    help="Model for players (overrides --model)")
    args = ap.parse_args()

    run_llm_session(args.scenario, args.dungeon, args.party_size, thinking=args.thinking,
                    existing_session=args.session, model=args.model, dm_model=args.dm_model,
                    player_model=args.player_model)
