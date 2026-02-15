# Dungeon Test Run Skill

Run local dungeon game sessions for testing the mon-hackathon contracts.

---

## Quick Start (Local Mode)

### 1. Start Local Anvil

```bash
cd ~/clawd/projects/mon-hackathon

# Start fresh (first time)
./start-anvil.sh --fresh

# Or start with saved state (subsequent runs)
./start-anvil.sh
```

### 2. Setup Environment (First Time Only)

```bash
./setup-local.sh
```

This will:
- Deploy all contracts (Gold, DungeonNFT, DungeonTickets, DungeonManager)
- Configure roles (minter, burner)
- Upload skills from `skills/` folder
- Mint 5 dungeons with varied difficulty
- Register 5 test agents
- Mint 10 tickets to each agent
- Stake dungeon #0 (ready for test runs)
- Save state for persistence

### 3. Load Contract Addresses

Read from `local-deployment.json`:

```bash
cd ~/clawd/projects/mon-hackathon
DEPLOYMENT=$(cat local-deployment.json)
MANAGER=$(echo $DEPLOYMENT | jq -r '.contracts.DungeonManager')
NFT=$(echo $DEPLOYMENT | jq -r '.contracts.DungeonNFT')
TICKETS=$(echo $DEPLOYMENT | jq -r '.contracts.DungeonTickets')
GOLD=$(echo $DEPLOYMENT | jq -r '.contracts.Gold')
```

---

## Available Dungeons (Local)

| ID | Theme | Difficulty | Party Size | Rarity | Staked |
|----|-------|------------|------------|--------|--------|
| 0 | Cave | 5 | 2 | Common | ✅ Yes |
| 1 | Crypt | 8 | 3 | Rare | No |
| 2 | Abyss | 10 | 2 | Legendary | No |
| 3 | Forest | 3 | 2 | Common | No |
| 4 | Volcano | 7 | 3 | Rare | No |

---

## Test Accounts

All accounts have 1000 ETH and 10 tickets.

| # | Address | Role |
|---|---------|------|
| 0 | 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 | Deployer/Owner |
| 1 | 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 | Agent 1 |
| 2 | 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC | Agent 2 |
| 3 | 0x90F79bf6EB2c4f870365E785982E1f101E93b906 | Agent 3 |
| 4 | 0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65 | Agent 4 |
| 5 | 0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc | Agent 5 |

Private keys are in `local-deployment.json` under `anvilAccounts`.

---

## Running a Test Session

### Enter Dungeon (as Agent 1)

```bash
AGENT1_KEY="0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
RPC_URL="http://127.0.0.1:8545"

# Enter dungeon #0 (staked)
cast send --private-key $AGENT1_KEY --rpc-url $RPC_URL \
  $MANAGER "enterDungeon(uint256)" 0
```

### Check Session State

```bash
# Get current session ID for dungeon 0
SESSION_ID=$(cast call --rpc-url $RPC_URL $MANAGER "dungeons(uint256)(uint256,address,bool,uint256,uint256)" 0 | head -5 | tail -1)

# Get session details
cast call --rpc-url $RPC_URL $MANAGER "sessions(uint256)" $SESSION_ID
```

### Submit Action

```bash
cast send --private-key $AGENT1_KEY --rpc-url $RPC_URL \
  $MANAGER "submitAction(uint256,string)" $SESSION_ID "I attack the goblin!"
```

### Submit DM Response

```solidity
// DMAction struct: (uint8 actionType, address target, uint256 value, string narrative)
// actionType: 0=NARRATE, 1=REWARD_GOLD, 2=REWARD_XP, 3=DAMAGE, 4=KILL_PLAYER, 5=COMPLETE, 6=FAIL
```

```bash
# Example: Award 50 gold to Agent1
cast send --private-key $AGENT1_KEY --rpc-url $RPC_URL \
  $MANAGER "submitDMResponse(uint256,string,(uint8,address,uint256,string)[])" \
  $SESSION_ID \
  "The goblin falls! You find 50 gold pieces." \
  "[(1,0x70997970C51812dc3A010C7d01b50e0d17dc79C8,50,'')]"
```

---

## State Persistence

State is automatically saved when you run `./setup-local.sh`.

To manually save state:
```bash
cast rpc anvil_dumpState --rpc-url http://127.0.0.1:8545 > .anvil-state.json
```

State auto-loads on `./start-anvil.sh` (unless `--fresh` is used).

---

## Troubleshooting

### "NotConfigured" Error
The DungeonManager requires Gold.minter and Tickets.burner to be set.
```bash
./setup-local.sh  # Re-run setup
```

### "InsufficientTickets" Error
Mint more tickets:
```bash
cast send --private-key $DEPLOYER_KEY --rpc-url $RPC_URL \
  $TICKETS "mint(address,uint256)" $AGENT1 10
```

### "DungeonNotActive" Error
Stake the dungeon first:
```bash
# Approve and stake
cast send --private-key $DEPLOYER_KEY --rpc-url $RPC_URL \
  $NFT "approve(address,uint256)" $MANAGER 1
cast send --private-key $DEPLOYER_KEY --rpc-url $RPC_URL \
  $MANAGER "stakeDungeon(uint256)" 1
```

### Reset Everything
```bash
./start-anvil.sh --fresh
./setup-local.sh
```

---

## Network Comparison

| Setting | Local (Anvil) | Testnet (Monad) |
|---------|---------------|-----------------|
| Chain ID | 31337 | 10143 |
| RPC | http://127.0.0.1:8545 | https://monad-testnet.drpc.org |
| Block time | 1s | ~1s |
| Gas | Free | Testnet MON |
| Deployment | `local-deployment.json` | `deployments/testnet.json` |

---

## Files Reference

| File | Purpose |
|------|---------|
| `start-anvil.sh` | Start local Anvil with correct config |
| `setup-local.sh` | Deploy & configure everything |
| `local-deployment.json` | Contract addresses & test data |
| `.anvil-state.json` | Persistent blockchain state |
| `skills/*.md` | Game skill documents (uploaded on-chain) |
