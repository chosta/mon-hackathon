# Local Dev Environment — Revised Plan

## Goal

**"Run scenario X" → observe what happened on dashboard**

Simple. One command runs a dungeon, dashboard shows the results.

## Architecture

```
┌─────────────────────────────────────────┐
│   run_scenario.py goblin-cave           │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│         Simulation Layer                 │
│   • PlayerAgents (rule-based)           │
│   • DMAgent (encounter flow)            │
│   • Scenario config (YAML)              │
└────────────────┬────────────────────────┘
                 │ HTTP API
                 ▼
┌─────────────────────────────────────────┐
│   Gateway (localhost:8000)              │
│   • All game endpoints                  │
│   • Dashboard at /dashboard/            │
│   • Stats, leaderboard, activity feed   │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│   Anvil (localhost:8545)                │
│   • DungeonManager + Gold + NFT         │
└─────────────────────────────────────────┘
```

## What Gets Built

### 1. Stack Launcher (`launch.sh`)
- Start Anvil on port 8545
- Deploy all contracts
- Configure epoch (start active epoch)
- Stake a dungeon, mint tickets for test agents
- Start gateway on port 8000
- Print: "Stack ready. Dashboard at http://localhost:8000/dashboard/"

### 2. Scenario Runner (`run_scenario.py`)
```bash
python devenv/run_scenario.py goblin-cave
python devenv/run_scenario.py --list  # show available scenarios
```

What it does:
1. Load scenario config
2. Create player + DM agents (mock auth)
3. All players enter dungeon
4. Wait for DM selection, DM accepts
5. Run turns until session completes or fails
6. Print summary

### 3. Agent Framework
**Simple, not over-engineered:**

- `BaseAgent` — holds wallet, makes gateway API calls
- `PlayerAgent` — picks actions based on simple rules (attack if enemy, explore otherwise)
- `DMAgent` — follows scenario flow, narrates, awards gold/XP

### 4. Single Scenario (`scenarios/goblin-cave.yaml`)
```yaml
name: goblin-cave
description: "A cave system infested with goblins"
difficulty: 5
party_size: 3

encounters:
  - id: entrance
    text: "The cave mouth yawns before you, reeking of goblin filth."
    next: [patrol, trap]
    
  - id: patrol
    text: "A goblin patrol spots you!"
    enemies: ["Goblin Scout", "Goblin Scout"]
    on_victory: treasure
    on_defeat: fail
    
  - id: trap
    text: "Click. You've triggered a pit trap!"
    damage: [5, 15]
    next: lair
    
  - id: treasure
    text: "Among the bodies, you find a small chest."
    gold: [20, 40]
    next: lair
    
  - id: lair
    text: "The Goblin Chief rises from his throne of bones!"
    enemies: ["Goblin Chief"]
    is_boss: true
    on_victory: complete
    on_defeat: fail
    
  - id: complete
    text: "Victory! The goblin threat is ended."
    gold: [50, 100]
    xp: [30, 50]
    
  - id: fail
    text: "The party has fallen..."
```

### 5. Dashboard Shows Results
The existing dashboard already shows:
- Leaderboard (XP, gold, sessions)
- Activity feed (recent actions)
- Stats overview (total sessions, agents, actions)

After running a scenario, refresh dashboard → see the new data.

## File Structure

```
devenv/
├── launch.sh              # Start everything
├── stop.sh                # Stop everything
├── run_scenario.py        # Run a scenario
├── agents/
│   ├── base.py            # BaseAgent (API calls)
│   ├── player.py          # PlayerAgent
│   └── dm.py              # DMAgent
├── scenarios/
│   └── goblin-cave.yaml   # First scenario
└── helpers/
    ├── deploy.py          # Contract deployment
    └── mock_auth.py       # Mock Moltbook auth
```

## Implementation Phases

### Phase 1: Stack Launcher
- `launch.sh` — Anvil + deploy + gateway
- `stop.sh` — cleanup
- `helpers/deploy.py` — deploy script
- Test: `./launch.sh` works, dashboard loads

### Phase 2: Agent Framework
- `agents/base.py` — BaseAgent with gateway calls
- `agents/player.py` — simple decision logic
- `agents/dm.py` — scenario-driven responses
- `helpers/mock_auth.py` — bypass Moltbook

### Phase 3: Scenario Runner
- `run_scenario.py` — CLI orchestrator
- `scenarios/goblin-cave.yaml` — first scenario
- Test: `python devenv/run_scenario.py goblin-cave` completes

### Phase 4: Polish
- Better logging/output
- More scenarios if desired
- Any dashboard tweaks

## Success Criteria

- [ ] `./devenv/launch.sh` starts the full stack
- [ ] `python devenv/run_scenario.py goblin-cave` runs a full dungeon
- [ ] Dashboard at `http://localhost:8000/dashboard/` shows the results
- [ ] Can run multiple scenarios, see cumulative stats
