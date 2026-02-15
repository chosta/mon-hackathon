#!/bin/bash
# Full setup script for mon-hackathon local development
# Safe to re-run - idempotent operations
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$PROJECT_DIR/anvil-state.json"
DEPLOYMENT_FILE="$PROJECT_DIR/local-deployment.json"
export PATH="$HOME/.foundry/bin:$PATH"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[setup]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
error() { echo -e "${RED}[error]${NC} $1"; }

# Parse flags
FRESH=false
SKIP_GATEWAY=false
for arg in "$@"; do
  case $arg in
    --fresh) FRESH=true ;;
    --skip-gateway) SKIP_GATEWAY=true ;;
  esac
done

echo "╔══════════════════════════════════════════════════════════╗"
echo "║         🏰 Mon-Hackathon Local Setup                     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# =============================================================================
# 1. ANVIL SETUP
# =============================================================================
log "Step 1: Anvil blockchain"

# Check if Anvil is running
ANVIL_RUNNING=false
if pgrep -f "anvil.*--port 8545" > /dev/null 2>&1; then
  ANVIL_RUNNING=true
  ANVIL_PID=$(pgrep -f "anvil.*--port 8545" | head -1)
  log "Anvil already running (PID: $ANVIL_PID)"
fi

if [ "$FRESH" = true ]; then
  log "Fresh mode: stopping Anvil and removing state..."
  pkill -f "anvil" 2>/dev/null || true
  sleep 1
  rm -f "$STATE_FILE" "$DEPLOYMENT_FILE"
  ANVIL_RUNNING=false
fi

if [ "$ANVIL_RUNNING" = false ]; then
  log "Starting Anvil with persistence..."
  
  # Build Anvil command
  ANVIL_CMD="anvil --host 0.0.0.0 --port 8545 --chain-id 31337 --block-time 1 --accounts 10 --balance 10000"
  
  # Add persistence flags
  ANVIL_CMD="$ANVIL_CMD --dump-state $STATE_FILE"
  
  # Load existing state if available
  if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
    log "Loading existing state from anvil-state.json"
    ANVIL_CMD="$ANVIL_CMD --load-state $STATE_FILE"
  else
    log "Starting fresh (no state file)"
  fi
  
  # Start Anvil
  $ANVIL_CMD > "$PROJECT_DIR/anvil.log" 2>&1 &
  ANVIL_PID=$!
  echo "$ANVIL_PID" > "$PROJECT_DIR/.anvil.pid"
  
  # Wait for ready
  for i in $(seq 1 30); do
    if cast chain-id --rpc-url http://127.0.0.1:8545 2>/dev/null; then
      log "✓ Anvil ready (PID: $ANVIL_PID)"
      break
    fi
    if [ $i -eq 30 ]; then
      error "Anvil failed to start!"
      exit 1
    fi
    sleep 0.5
  done
fi

# =============================================================================
# 2. CONTRACT DEPLOYMENT (if needed)
# =============================================================================
log "Step 2: Contract deployment"

# Check if contracts are deployed
NEED_DEPLOY=true
if [ -f "$DEPLOYMENT_FILE" ]; then
  MANAGER_ADDR=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT_FILE'))['contracts']['DungeonManager'])" 2>/dev/null || echo "")
  if [ -n "$MANAGER_ADDR" ]; then
    # Check if contract exists
    CODE=$(cast code "$MANAGER_ADDR" --rpc-url http://127.0.0.1:8545 2>/dev/null | head -c 10)
    if [ "$CODE" != "0x" ] && [ -n "$CODE" ]; then
      log "Contracts already deployed at $MANAGER_ADDR"
      NEED_DEPLOY=false
    fi
  fi
fi

if [ "$NEED_DEPLOY" = true ]; then
  log "Deploying contracts..."
  cd "$PROJECT_DIR"
  python3 "$SCRIPT_DIR/helpers/deploy.py"
  log "✓ Contracts deployed"
fi

# Re-read contract addresses
MANAGER_ADDR=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT_FILE'))['contracts']['DungeonManager'])")
GOLD_ADDR=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT_FILE'))['contracts']['Gold'])")
NFT_ADDR=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT_FILE'))['contracts']['DungeonNFT'])")
TICKETS_ADDR=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT_FILE'))['contracts']['DungeonTickets'])")

# Deployer key (Anvil account #0)
DEPLOYER="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
RPC="http://127.0.0.1:8545"

# =============================================================================
# 3. DUNGEON CONFIGURATION
# =============================================================================
log "Step 3: Dungeon configuration"

# Check current dungeon count
DUNGEON_COUNT=$(cast call "$MANAGER_ADDR" "dungeonCount()(uint256)" --rpc-url "$RPC")
log "Current dungeon count: $DUNGEON_COUNT"

# Check epoch state
EPOCH=$(cast call "$MANAGER_ADDR" "currentEpoch()(uint256)" --rpc-url "$RPC")
EPOCH_STATE=$(cast call "$MANAGER_ADDR" "epochState()(uint8)" --rpc-url "$RPC")
log "Current epoch: $EPOCH, state: $EPOCH_STATE (0=Active, 1=Grace)"

# We need dungeons with party sizes 2, 3, 5 (contract enforces min=2, max=6)
# Check what we have
check_dungeon_setup() {
  local has_duo=false      # party_size=2
  local has_trio=false     # party_size=3
  local has_party=false    # party_size=5
  
  for i in $(seq 0 $((DUNGEON_COUNT - 1))); do
    # Get NFT ID for this dungeon
    NFT_ID=$(cast call "$MANAGER_ADDR" "dungeons(uint256)(uint256,address,bool,uint256,uint256)" "$i" --rpc-url "$RPC" | head -1)
    # Get party size from NFT
    PARTY_SIZE=$(cast call "$NFT_ADDR" "getTraits(uint256)(uint8,uint8,uint8,uint8)" "$NFT_ID" --rpc-url "$RPC" | sed -n '2p')
    
    case $PARTY_SIZE in
      2) has_duo=true ;;
      3) has_trio=true ;;
      5) has_party=true ;;
    esac
  done
  
  echo "$has_duo:$has_trio:$has_party"
}

SETUP=$(check_dungeon_setup)
HAS_DUO=$(echo "$SETUP" | cut -d: -f1)
HAS_TRIO=$(echo "$SETUP" | cut -d: -f2)
HAS_PARTY=$(echo "$SETUP" | cut -d: -f3)

log "Dungeon setup: duo=$HAS_DUO, trio=$HAS_TRIO, party=$HAS_PARTY"

# Need to be in Grace to stake dungeons
if [ "$EPOCH_STATE" = "0" ]; then
  warn "Epoch is Active. To add new dungeons, need to endEpoch first."
  
  if [ "$HAS_DUO" = "false" ] || [ "$HAS_TRIO" = "false" ] || [ "$HAS_PARTY" = "false" ]; then
    log "Ending epoch to configure dungeons..."
    cast send "$MANAGER_ADDR" "endEpoch()" --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
    EPOCH_STATE=1
    log "✓ Epoch ended (now in Grace)"
  fi
fi

# Create missing dungeons
mint_and_stake() {
  local DIFF=$1
  local PARTY=$2
  local THEME=$3
  local RARITY=$4
  local DESC=$5
  
  # Get next NFT ID
  NFT_ID=$(cast call "$NFT_ADDR" "nextTokenId()(uint256)" --rpc-url "$RPC" 2>/dev/null || echo "0")
  
  log "Minting NFT #$NFT_ID: $DESC (diff=$DIFF, party=$PARTY, theme=$THEME, rarity=$RARITY)"
  cast send "$NFT_ADDR" "mint(address,uint8,uint8,uint8,uint8)" \
    "$DEPLOYER" "$DIFF" "$PARTY" "$THEME" "$RARITY" \
    --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  
  log "Approving NFT #$NFT_ID for staking..."
  cast send "$NFT_ADDR" "approve(address,uint256)" "$MANAGER_ADDR" "$NFT_ID" \
    --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  
  log "Staking NFT #$NFT_ID..."
  cast send "$MANAGER_ADDR" "stakeDungeon(uint256)" "$NFT_ID" \
    --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  
  log "✓ Dungeon staked"
}

# Create missing dungeon types (party sizes 2, 3, 5)
if [ "$HAS_DUO" = "false" ]; then
  log "Creating duo dungeon (party size 2)..."
  mint_and_stake 5 2 1 1 "Duo Forest Rare"
fi

if [ "$HAS_TRIO" = "false" ]; then
  log "Creating trio dungeon (party size 3)..."
  mint_and_stake 6 3 2 1 "Crypt Rare"
fi

if [ "$HAS_PARTY" = "false" ]; then
  log "Creating party dungeon (party size 5)..."
  mint_and_stake 8 5 4 2 "Abyss Epic"
fi

# =============================================================================
# 4. AGENT REGISTRATION & TICKETS
# =============================================================================
log "Step 4: Agent registration & tickets"

# Test agents (Anvil accounts #1-5)
AGENTS=(
  "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
  "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
  "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
  "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
  "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"
)

for AGENT in "${AGENTS[@]}"; do
  # Check if registered
  IS_REG=$(cast call "$MANAGER_ADDR" "registeredAgents(address)(bool)" "$AGENT" --rpc-url "$RPC")
  if [ "$IS_REG" = "false" ]; then
    log "Registering agent $AGENT..."
    cast send "$MANAGER_ADDR" "registerAgent(address)" "$AGENT" \
      --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  fi
  
  # Check ticket balance
  TICKETS=$(cast call "$TICKETS_ADDR" "balanceOf(address,uint256)(uint256)" "$AGENT" "0" --rpc-url "$RPC")
  if [ "$TICKETS" -lt 5 ]; then
    log "Minting tickets for $AGENT..."
    cast send "$TICKETS_ADDR" "mint(address,uint256)" "$AGENT" "20" \
      --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  fi
done

# Also register deployer
IS_REG=$(cast call "$MANAGER_ADDR" "registeredAgents(address)(bool)" "$DEPLOYER" --rpc-url "$RPC")
if [ "$IS_REG" = "false" ]; then
  log "Registering deployer as agent..."
  cast send "$MANAGER_ADDR" "registerAgent(address)" "$DEPLOYER" \
    --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
fi

log "✓ Agents configured"

# =============================================================================
# 5. START EPOCH (if in Grace)
# =============================================================================
log "Step 5: Epoch management"

EPOCH_STATE=$(cast call "$MANAGER_ADDR" "epochState()(uint8)" --rpc-url "$RPC")
if [ "$EPOCH_STATE" = "1" ]; then
  log "Starting new epoch..."
  cast send "$MANAGER_ADDR" "startEpoch()" --private-key "$DEPLOYER_KEY" --rpc-url "$RPC" > /dev/null
  log "✓ Epoch started"
else
  log "Epoch already active"
fi

# Final epoch info
EPOCH=$(cast call "$MANAGER_ADDR" "currentEpoch()(uint256)" --rpc-url "$RPC")
log "Current epoch: $EPOCH"

# =============================================================================
# 6. GATEWAY SETUP
# =============================================================================
if [ "$SKIP_GATEWAY" = false ]; then
  log "Step 6: Gateway setup"
  
  # Write local .env for gateway
  cat > "$PROJECT_DIR/gateway/.env" << EOF
# Local development environment (auto-generated by setup_full.sh)
GW_RUNNER_PRIVATE_KEY=$DEPLOYER_KEY
GW_JWT_SECRET=local-dev-secret
GW_RPC_URL=http://127.0.0.1:8545
GW_CHAIN_ID=31337
GW_DUNGEON_MANAGER=$MANAGER_ADDR
GW_GOLD_CONTRACT=$GOLD_ADDR
GW_DUNGEON_NFT=$NFT_ADDR
GW_DUNGEON_TICKETS=$TICKETS_ADDR
GW_MOLTBOOK_BASE_URL=https://www.moltbook.com/api/v1
GW_DB_PATH=devenv.db
GW_MAX_TX_PER_HOUR=100
EOF
  
  # Check if gateway is running
  if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
    log "Gateway running, restarting to pick up config..."
    pkill -f "uvicorn.*main:app" 2>/dev/null || true
    sleep 1
  fi
  
  # Start gateway
  cd "$PROJECT_DIR/gateway"
  python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 > "$PROJECT_DIR/gateway.log" 2>&1 &
  GW_PID=$!
  
  # Wait for ready
  for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
      log "✓ Gateway ready (PID: $GW_PID)"
      break
    fi
    if [ $i -eq 20 ]; then
      error "Gateway failed to start!"
      exit 1
    fi
    sleep 0.5
  done
else
  log "Step 6: Gateway (skipped)"
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║         🎮 Setup Complete!                               ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║   Anvil:     http://127.0.0.1:8545                       ║"
echo "║   Gateway:   http://127.0.0.1:8000                       ║"
echo "║   Dashboard: http://127.0.0.1:8000/dashboard/            ║"
echo "║                                                          ║"
echo "║   Contracts:                                             ║"
echo "║   - DungeonManager: $MANAGER_ADDR   ║"
echo "║   - DungeonNFT:     $NFT_ADDR   ║"
echo "║                                                          ║"
echo "║   Dungeons configured:                                   ║"

# List dungeons
DUNGEON_COUNT=$(cast call "$MANAGER_ADDR" "dungeonCount()(uint256)" --rpc-url "$RPC")
for i in $(seq 0 $((DUNGEON_COUNT - 1))); do
  NFT_ID=$(cast call "$MANAGER_ADDR" "dungeons(uint256)(uint256,address,bool,uint256,uint256)" "$i" --rpc-url "$RPC" | head -1)
  TRAITS=$(cast call "$NFT_ADDR" "getTraits(uint256)(uint8,uint8,uint8,uint8)" "$NFT_ID" --rpc-url "$RPC")
  DIFF=$(echo "$TRAITS" | sed -n '1p')
  PARTY=$(echo "$TRAITS" | sed -n '2p')
  printf "║   - Dungeon %d: NFT#%s (diff=%s, party=%s)             ║\n" "$i" "$NFT_ID" "$DIFF" "$PARTY"
done

echo "║                                                          ║"
echo "║   Epoch: $EPOCH (Active)                                       ║"
echo "║                                                          ║"
echo "║   To run a scenario:                                     ║"
echo "║   python devenv/run_scenario.py goblin-cave              ║"
echo "║                                                          ║"
echo "╚══════════════════════════════════════════════════════════╝"
