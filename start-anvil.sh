#!/bin/bash
# Start Anvil with correct configuration for mon-hackathon local development
# Usage: ./start-anvil.sh [--fresh]

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$PROJECT_DIR/.anvil-state.json"
PID_FILE="$PROJECT_DIR/.anvil.pid"

# Configuration - MUST match setup-local.sh
CHAIN_ID=31337
MNEMONIC="test test test test test test test test test test test junk"
PORT=8545
BLOCK_TIME=1  # 1 second blocks, set to 0 for instant mining
BALANCE=1000  # ETH per account

export PATH="$HOME/.foundry/bin:$PATH"

# Handle --fresh flag
FRESH=false
for arg in "$@"; do
  if [ "$arg" == "--fresh" ]; then
    FRESH=true
  fi
done

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║             🔨 Starting Anvil (Local EVM)                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║ Chain ID:     $CHAIN_ID                                          ║"
echo "║ RPC:          http://127.0.0.1:$PORT                          ║"
echo "║ Block time:   ${BLOCK_TIME}s                                            ║"
echo "║ Balance:      ${BALANCE} ETH per account                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# Fresh mode
if [ "$FRESH" == "true" ]; then
  echo "🧹 Fresh mode: removing saved state"
  rm -f "$STATE_FILE"
fi

# Kill existing Anvil on this port
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "⏹  Stopping existing Anvil (PID: $OLD_PID)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

# Also try pkill as backup
pkill -f "anvil.*--port $PORT" 2>/dev/null || true
sleep 1

# Build Anvil command
ANVIL_CMD="anvil --chain-id $CHAIN_ID --mnemonic \"$MNEMONIC\" --port $PORT --balance $BALANCE"

if [ "$BLOCK_TIME" != "0" ]; then
  ANVIL_CMD="$ANVIL_CMD --block-time $BLOCK_TIME"
fi

# Load saved state if exists
if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
  echo "📂 Loading saved state from .anvil-state.json"
  ANVIL_CMD="$ANVIL_CMD --load-state $STATE_FILE"
else
  echo "🆕 Starting fresh (no saved state found)"
fi

# Start Anvil in background
eval "$ANVIL_CMD" &
ANVIL_PID=$!
echo "$ANVIL_PID" > "$PID_FILE"

# Wait for Anvil to be ready
echo -n "⏳ Waiting for Anvil to start..."
for i in {1..30}; do
  if curl -s -X POST http://127.0.0.1:$PORT \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"eth_chainId","id":1}' \
    >/dev/null 2>&1; then
    echo " ready!"
    break
  fi
  if [ $i -eq 30 ]; then
    echo " TIMEOUT!"
    echo "❌ Anvil failed to start"
    exit 1
  fi
  echo -n "."
  sleep 0.5
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "✅ Anvil running on http://127.0.0.1:$PORT (PID: $ANVIL_PID)"
echo ""
echo "📋 Available accounts:"
echo "   #0 (Deployer): 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
echo "   #1 (Agent 1):  0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
echo "   #2 (Agent 2):  0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
echo "   #3 (Agent 3):  0x90F79bf6EB2c4f870365E785982E1f101E93b906"
echo "   #4 (Agent 4):  0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
echo "   #5 (Agent 5):  0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"
echo ""
echo "🔧 Commands:"
echo "   Stop:    kill $ANVIL_PID (or pkill -f anvil)"
echo "   Setup:   ./setup-local.sh"
echo "═══════════════════════════════════════════════════════════════"
