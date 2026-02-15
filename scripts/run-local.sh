#!/bin/bash
set -e

# Local EVM Test Environment for Mon-Hackathon
# Uses Anvil with reproducible settings

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Anvil settings
CHAIN_ID=31337
MNEMONIC="test test test test test test test test test test test junk"
BLOCK_TIME=1  # Set to 0 for instant mining
PORT=8545

# Wallet balance (1000 ETH - anvil takes ETH directly)
BALANCE="1000"

# Anvil account #0 (derived from mnemonic above)
DEPLOYER="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# State file for persistence
STATE_FILE="$PROJECT_DIR/.anvil-state.json"

export PATH="$HOME/.foundry/bin:$PATH"

# Parse arguments
ACTION="${1:-start}"
shift || true

case "$ACTION" in
  start)
    # Check for --fresh flag
    FRESH=false
    for arg in "$@"; do
      if [ "$arg" == "--fresh" ]; then
        FRESH=true
      fi
    done
    
    echo "=========================================="
    echo "🔨 Starting Local EVM (Anvil)"
    echo "=========================================="
    echo "Chain ID:    $CHAIN_ID"
    echo "RPC:         http://127.0.0.1:$PORT"
    echo "Deployer:    $DEPLOYER"
    echo "Balance:     1000 ETH per account"
    echo "Block time:  ${BLOCK_TIME}s"
    if [ "$FRESH" == "true" ]; then
      echo "Mode:        🧹 FRESH (no saved state)"
    fi
    echo "=========================================="
    
    # Handle fresh mode
    if [ "$FRESH" == "true" ] && [ -f "$STATE_FILE" ]; then
      echo "🧹 Fresh mode: removing saved state"
      rm -f "$STATE_FILE"
    fi
    
    # Kill existing anvil
    pkill -f "anvil.*--port $PORT" 2>/dev/null || true
    sleep 1
    
    # Start anvil with state loading if exists
    ANVIL_ARGS="--chain-id $CHAIN_ID --mnemonic \"$MNEMONIC\" --port $PORT --balance $BALANCE"
    if [ "$BLOCK_TIME" != "0" ]; then
      ANVIL_ARGS="$ANVIL_ARGS --block-time $BLOCK_TIME"
    fi
    if [ -f "$STATE_FILE" ]; then
      echo "📂 Loading saved state from $STATE_FILE"
      ANVIL_ARGS="$ANVIL_ARGS --load-state $STATE_FILE"
    fi
    
    eval "anvil $ANVIL_ARGS" &
    ANVIL_PID=$!
    echo $ANVIL_PID > "$PROJECT_DIR/.anvil.pid"
    sleep 2
    
    echo "✅ Anvil running (PID: $ANVIL_PID)"
    ;;
    
  deploy)
    echo "=========================================="
    echo "🚀 Deploying Contracts to Local EVM"
    echo "=========================================="
    echo "⚠️  SAFETY CHECK:"
    echo "    Chain ID: $CHAIN_ID"
    echo "    RPC:      http://127.0.0.1:$PORT"
    echo "    Deployer: $DEPLOYER"
    echo "=========================================="
    
    # Verify we're on local chain
    ACTUAL_CHAIN=$(cast chain-id --rpc-url http://127.0.0.1:$PORT 2>/dev/null || echo "failed")
    if [ "$ACTUAL_CHAIN" != "$CHAIN_ID" ]; then
      echo "❌ ERROR: Expected chain $CHAIN_ID, got $ACTUAL_CHAIN"
      echo "   Is Anvil running? Run: ./scripts/run-local.sh start"
      exit 1
    fi
    
    PRIVATE_KEY=$DEPLOYER_KEY forge script script/Deploy.s.sol \
      --rpc-url http://127.0.0.1:$PORT \
      --broadcast
    
    echo "✅ Deployment complete"
    ;;
    
  setup)
    echo "=========================================="
    echo "🎮 Full Game Setup (deploy + mint + skills)"
    echo "=========================================="
    
    # Deploy contracts
    $0 deploy
    
    # Get deployed addresses from broadcast
    BROADCAST_FILE=$(ls -t broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json 2>/dev/null | head -1)
    if [ -z "$BROADCAST_FILE" ]; then
      echo "❌ No broadcast file found"
      exit 1
    fi
    
    echo "📄 Reading addresses from $BROADCAST_FILE"
    GOLD=$(jq -r '.transactions[] | select(.contractName=="Gold") | .contractAddress' "$BROADCAST_FILE")
    NFT=$(jq -r '.transactions[] | select(.contractName=="DungeonNFT") | .contractAddress' "$BROADCAST_FILE")
    TICKETS=$(jq -r '.transactions[] | select(.contractName=="DungeonTickets") | .contractAddress' "$BROADCAST_FILE")
    MANAGER=$(jq -r '.transactions[] | select(.contractName=="DungeonManager") | .contractAddress' "$BROADCAST_FILE")
    
    echo "Gold:           $GOLD"
    echo "DungeonNFT:     $NFT"
    echo "DungeonTickets: $TICKETS"
    echo "DungeonManager: $MANAGER"
    
    # Save to local deployment file
    cat > "$PROJECT_DIR/deployments/local.json" << DEPLOY_EOF
{
  "schemaVersion": 1,
  "environment": "local",
  "network": "anvil",
  "chainId": $CHAIN_ID,
  "rpc": "http://127.0.0.1:$PORT",
  "explorerUrl": null,
  "faucet": null,
  "deployer": "$DEPLOYER",
  "expectedDeployer": "$DEPLOYER",
  "deployedAt": "$(date -Iseconds)",
  "contracts": {
    "Gold": "$GOLD",
    "DungeonNFT": "$NFT",
    "DungeonTickets": "$TICKETS",
    "DungeonManager": "$MANAGER"
  },
  "notes": "Local Anvil environment. Contracts deployed on setup."
}
DEPLOY_EOF
    
    echo "✅ Saved to deployments/local.json"
    ;;
    
  save)
    echo "💾 Saving Anvil state..."
    cast rpc anvil_dumpState --rpc-url http://127.0.0.1:$PORT > "$STATE_FILE"
    echo "✅ State saved to $STATE_FILE"
    ;;
    
  stop)
    echo "🛑 Stopping Anvil..."
    # Auto-save before stopping
    if curl -s http://127.0.0.1:$PORT >/dev/null 2>&1; then
      echo "💾 Auto-saving state..."
      cast rpc anvil_dumpState --rpc-url http://127.0.0.1:$PORT > "$STATE_FILE" 2>/dev/null || true
      if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
        echo "✅ State saved to $STATE_FILE"
      fi
    fi
    if [ -f "$PROJECT_DIR/.anvil.pid" ]; then
      kill $(cat "$PROJECT_DIR/.anvil.pid") 2>/dev/null || true
      rm "$PROJECT_DIR/.anvil.pid"
    fi
    pkill -f "anvil.*--port $PORT" 2>/dev/null || true
    echo "✅ Anvil stopped"
    ;;
    
  reset)
    echo "🗑️  Resetting local state..."
    rm -f "$STATE_FILE"
    rm -f "$PROJECT_DIR/.anvil.pid"
    echo "✅ State cleared"
    ;;
    
  fork)
    FORK_URL="${1:-https://testnet-rpc.monad.xyz}"
    echo "=========================================="
    echo "🍴 Starting Anvil in FORK MODE"
    echo "=========================================="
    echo "Forking:     $FORK_URL"
    echo "Local RPC:   http://127.0.0.1:$PORT"
    echo "=========================================="
    
    pkill -f "anvil.*--port $PORT" 2>/dev/null || true
    sleep 1
    
    anvil --fork-url "$FORK_URL" --port $PORT --chain-id $CHAIN_ID &
    ANVIL_PID=$!
    echo $ANVIL_PID > "$PROJECT_DIR/.anvil.pid"
    sleep 3
    
    echo "✅ Anvil forking Monad testnet (PID: $ANVIL_PID)"
    ;;
    
  *)
    echo "Usage: $0 {start|deploy|setup|save|stop|reset|fork [rpc_url]}"
    echo ""
    echo "Commands:"
    echo "  start          - Start local Anvil EVM (loads saved state if exists)"
    echo "  start --fresh  - Start fresh (wipes saved state)"
    echo "  deploy         - Deploy contracts to local EVM"
    echo "  setup          - Full setup (deploy + save addresses)"
    echo "  save           - Save current Anvil state"
    echo "  stop           - Stop Anvil (auto-saves state)"
    echo "  reset          - Delete all saved state"
    echo "  fork           - Start Anvil forking Monad testnet"
    exit 1
    ;;
esac
