#!/bin/bash
# Launch the full local dev stack: Anvil + Contracts + Gateway
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
export PATH="$HOME/.foundry/bin:$PATH"

# Parse flags
NO_DEPLOY=false
for arg in "$@"; do
  case $arg in
    --no-deploy) NO_DEPLOY=true ;;
  esac
done

echo "🚀 Starting local dev stack..."

if [ "$NO_DEPLOY" = true ] && pgrep -f "anvil" > /dev/null 2>&1 && [ -f "$PROJECT_DIR/local-deployment.json" ]; then
  echo "--- Skipping Anvil restart & deploy (--no-deploy) ---"
  ANVIL_PID=$(pgrep -f "anvil" | head -1)
  echo "Using existing Anvil (PID: $ANVIL_PID)"
else
  # --- 1. Start Anvil ---
  echo ""
  echo "--- Starting Anvil on port 8545 ---"

  # Kill existing anvil
  pkill -f "anvil" 2>/dev/null && sleep 1 || true

  # Use --state flag for persistence between restarts
  anvil --host 0.0.0.0 --port 8545 --chain-id 31337 --block-time 1 \
    --accounts 10 --balance 10000 \
    --dump-state "$PROJECT_DIR/anvil-state.json" \
    ${ANVIL_STATE_FILE:+--load-state "$ANVIL_STATE_FILE"} \
    > "$PROJECT_DIR/anvil.log" 2>&1 &
  ANVIL_PID=$!
  echo "Anvil started (PID: $ANVIL_PID)"

  # Wait for Anvil to be ready
  for i in $(seq 1 15); do
    if cast chain-id --rpc-url http://127.0.0.1:8545 2>/dev/null; then
      echo "✓ Anvil is ready"
      break
    fi
    sleep 1
  done

  # --- 2. Deploy & Configure Contracts ---
  echo ""
  echo "--- Deploying contracts ---"
  cd "$PROJECT_DIR"
  python3 "$SCRIPT_DIR/helpers/deploy.py"
fi

# --- 3. Start Gateway ---
echo ""
echo "--- Starting Gateway on port 8000 ---"

# Only restart gateway if not already healthy
if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
  echo "Gateway already running, skipping restart..."
  GW_PID=$(pgrep -f "uvicorn.*main:app" | head -1)
else
  # Kill any zombie processes
  pkill -f "uvicorn.*main:app" 2>/dev/null && sleep 1 || true

  # Configure gateway for local
  export GW_RPC_URL="http://127.0.0.1:8545"
  export GW_CHAIN_ID=31337
  export GW_RUNNER_PRIVATE_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
  export GW_DB_PATH="$PROJECT_DIR/gateway/devenv.db"
  export GW_JWT_SECRET="local-dev-secret"

  # Read contract addresses from deployment
  DEPLOYMENT="$PROJECT_DIR/local-deployment.json"
  export GW_DUNGEON_MANAGER=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT'))['contracts']['DungeonManager'])")
  export GW_GOLD_CONTRACT=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT'))['contracts']['Gold'])")
  export GW_DUNGEON_NFT=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT'))['contracts']['DungeonNFT'])")
  export GW_DUNGEON_TICKETS=$(python3 -c "import json; print(json.load(open('$DEPLOYMENT'))['contracts']['DungeonTickets'])")

  cd "$PROJECT_DIR/gateway"
  python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 \
    > "$PROJECT_DIR/gateway.log" 2>&1 &
  GW_PID=$!
  echo "Gateway started (PID: $GW_PID)"

  # Wait for gateway
  for i in $(seq 1 10); do
    if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
      echo "✓ Gateway is ready"
      break
    fi
    sleep 1
  done
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║           🎮 Stack Ready!                                ║"
echo "║                                                          ║"
echo "║   Anvil:     http://127.0.0.1:8545  (PID: $ANVIL_PID)       ║"
echo "║   Gateway:   http://127.0.0.1:8000  (PID: $GW_PID)       ║"
echo "║   Dashboard: http://127.0.0.1:8000/dashboard/           ║"
echo "║                                                          ║"
echo "║   Run: python devenv/run_scenario.py goblin-cave         ║"
echo "║   Stop: ./devenv/stop.sh                                 ║"
echo "╚══════════════════════════════════════════════════════════╝"
