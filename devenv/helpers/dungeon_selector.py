"""Smart dungeon selection — match scenarios to available dungeons.

Selects the best dungeon for a scenario based on:
1. Party size (must match exactly — contract enforced)
2. Difficulty (prefer closest match)
3. Theme (prefer matching if scenario specifies)
4. Activity (prefer dungeons with no active sessions)
"""
import os
import json
import subprocess

FOUNDRY_BIN = os.path.expanduser("~/.foundry/bin")
RPC_URL = "http://127.0.0.1:8545"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cast_call(to: str, sig: str, *args) -> str:
    cmd = [f"{FOUNDRY_BIN}/cast", "call", "--rpc-url", RPC_URL, to, sig] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def get_all_dungeons(contracts: dict) -> list[dict]:
    """Fetch all staked dungeons with their traits."""
    manager = contracts["DungeonManager"]
    nft = contracts["DungeonNFT"]

    count_str = _cast_call(manager, "dungeonCount()(uint256)")
    if not count_str:
        return []

    dungeons = []
    for i in range(int(count_str)):
        raw = _cast_call(manager, "dungeons(uint256)(uint256,address,bool,uint256,uint256)", str(i))
        if not raw:
            continue
        lines = raw.strip().split("\n")
        nft_id = int(lines[0])
        is_active = lines[2].strip().lower() == "true" if len(lines) > 2 else False

        traits_raw = _cast_call(nft, "getTraits(uint256)(uint8,uint8,uint8,uint8)", str(nft_id))
        if not traits_raw:
            continue
        tl = traits_raw.strip().split("\n")

        dungeons.append({
            "id": i,
            "nft_id": nft_id,
            "active": is_active,
            "difficulty": int(tl[0]) if tl else 0,
            "party_size": int(tl[1]) if len(tl) > 1 else 0,
            "theme": int(tl[2]) if len(tl) > 2 else 0,
            "rarity": int(tl[3]) if len(tl) > 3 else 0,
        })

    return dungeons


THEME_MAP = {
    "cave": 0, "forest": 1, "crypt": 2, "volcano": 3,
    "abyss": 4, "shadow": 5, "void": 6,
}


def select_dungeon(scenario: dict, contracts: dict) -> dict | None:
    """Select the best dungeon for a scenario.

    Returns dungeon dict or None if no match.
    Priority:
    1. Explicit dungeon_id in scenario (if valid)
    2. Party size match (required)
    3. Best score: difficulty closeness + theme match + rarity match
    """
    dungeons = get_all_dungeons(contracts)
    if not dungeons:
        return None

    party_size = scenario.get("party_size", 2)
    target_diff = scenario.get("difficulty", 5)
    target_theme = THEME_MAP.get(scenario.get("theme", ""), -1)
    target_rarity_str = scenario.get("rarity", "")
    rarity_map = {"common": 0, "rare": 1, "epic": 2, "legendary": 3}
    target_rarity = rarity_map.get(target_rarity_str, -1)

    # Check explicit dungeon_id first
    explicit_id = scenario.get("dungeon_id")
    if explicit_id is not None:
        for d in dungeons:
            if d["id"] == explicit_id and d["active"] and d["party_size"] == party_size:
                return d

    # Filter by party size (hard requirement)
    candidates = [d for d in dungeons if d["party_size"] == party_size and d["active"]]
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Score candidates
    def score(d):
        s = 0
        # Difficulty closeness (0-10 range, closer = better)
        s += 10 - abs(d["difficulty"] - target_diff)
        # Theme match bonus
        if target_theme >= 0 and d["theme"] == target_theme:
            s += 5
        # Rarity match bonus
        if target_rarity >= 0 and d["rarity"] == target_rarity:
            s += 3
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def print_dungeon_table(contracts: dict):
    """Print a nice table of all available dungeons."""
    dungeons = get_all_dungeons(contracts)
    themes = {v: k for k, v in THEME_MAP.items()}
    rarities = {0: "Common", 1: "Rare", 2: "Epic", 3: "Legendary"}

    print(f"\n{'ID':>3} {'NFT':>4} {'Active':>6} {'Diff':>4} {'Party':>5} {'Theme':>8} {'Rarity':>10}")
    print("─" * 48)
    for d in dungeons:
        theme_name = themes.get(d["theme"], f"?{d['theme']}")
        rarity_name = rarities.get(d["rarity"], f"?{d['rarity']}")
        active = "✓" if d["active"] else "✗"
        print(f"{d['id']:>3} {d['nft_id']:>4} {active:>6} {d['difficulty']:>4} {d['party_size']:>5} {theme_name:>8} {rarity_name:>10}")
    print()
