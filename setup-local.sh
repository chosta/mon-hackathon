#!/bin/bash
# Complete Local Anvil Test Environment Setup for mon-hackathon
# This script sets up a fully functional local environment mirroring testnet
#
# Features:
# - Deterministic deployer (Anvil account #0) for stable addresses
# - Idempotency guards (skip if already done)
# - Deploy all contracts
# - Upload skills from skills/ folder
# - Mint 5 diverse dungeons
# - Register test wallets as agents
# - Mint tickets to test wallets
# - Configure roles (setMinter, setBurner)
# - Stake dungeon #0 for test runs
# - State persistence to .anvil-state.json
# - Comprehensive logging with JSON receipts

set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

CHAIN_ID=31337
RPC_URL="http://127.0.0.1:8545"

# Anvil deterministic accounts (from "test test test..." mnemonic)
DEPLOYER="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Test agents (accounts #1-5)
AGENT1="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
AGENT1_KEY="0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

AGENT2="0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
AGENT2_KEY="0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"

AGENT3="0x90F79bf6EB2c4f870365E785982E1f101E93b906"
AGENT3_KEY="0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"

AGENT4="0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
AGENT4_KEY="0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"

AGENT5="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"
AGENT5_KEY="0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"

AGENTS=("$AGENT1" "$AGENT2" "$AGENT3" "$AGENT4" "$AGENT5")
AGENT_KEYS=("$AGENT1_KEY" "$AGENT2_KEY" "$AGENT3_KEY" "$AGENT4_KEY" "$AGENT5_KEY")

# Output files
DEPLOYMENT_FILE="$PROJECT_DIR/local-deployment.json"
STATE_FILE="$PROJECT_DIR/.anvil-state.json"

# Add foundry to PATH
export PATH="$HOME/.foundry/bin:$PATH"

# ═══════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════

log_info() {
  echo "[INFO] $1"
}

log_success() {
  echo "[✓] $1"
}

log_warn() {
  echo "[WARN] $1"
}

log_error() {
  echo "[ERROR] $1" >&2
}

log_step() {
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "═══════════════════════════════════════════════════════════════"
}

# Execute cast send and verify success
# Args: description, to, sig, [args...]
safe_send() {
  local desc="$1"
  shift
  local to="$1"
  shift
  local sig="$1"
  shift

  log_info "$desc"
  
  local result
  local exit_code=0
  result=$(cast send --json \
    --rpc-url "$RPC_URL" \
    --private-key "$DEPLOYER_KEY" \
    "$to" "$sig" "$@" 2>&1) || exit_code=$?
  
  # Check for successful status in JSON output
  local status
  status=$(echo "$result" | jq -r '.status // empty' 2>/dev/null)
  
  if [ "$status" = "0x1" ] || [ "$status" = "1" ]; then
    local tx_hash
    tx_hash=$(echo "$result" | jq -r '.transactionHash // "unknown"')
    log_success "$desc (tx: ${tx_hash:0:18}...)"
    return 0
  elif [ $exit_code -eq 0 ]; then
    # No JSON status but command succeeded - try to extract hash
    local tx_hash
    tx_hash=$(echo "$result" | jq -r '.transactionHash // empty' 2>/dev/null)
    if [ -n "$tx_hash" ]; then
      log_success "$desc (tx: ${tx_hash:0:18}...)"
      return 0
    fi
    log_success "$desc"
    return 0
  else
    log_error "Transaction failed: $desc"
    echo "$result" | jq . 2>/dev/null || echo "$result"
    return 1
  fi
}

# Check if Anvil is running
check_anvil() {
  if ! curl -s -X POST "$RPC_URL" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"eth_chainId","id":1}' \
    >/dev/null 2>&1; then
    log_error "Anvil is not running!"
    echo "Start it with: ./start-anvil.sh"
    exit 1
  fi
  
  local actual_chain
  actual_chain=$(cast chain-id --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$actual_chain" != "$CHAIN_ID" ]; then
    log_error "Wrong chain! Expected $CHAIN_ID, got $actual_chain"
    exit 1
  fi
  
  log_success "Anvil running on chain $CHAIN_ID"
}

# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Check Prerequisites
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 1: Prerequisites Check"
check_anvil

# Check for required tools
for cmd in forge cast jq; do
  if ! command -v "$cmd" &>/dev/null; then
    log_error "Required command not found: $cmd"
    exit 1
  fi
done
log_success "All tools available (forge, cast, jq)"

# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Deploy Contracts (with idempotency)
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 2: Deploy Contracts"

# Check if already deployed by looking for local-deployment.json with valid addresses
ALREADY_DEPLOYED=false
if [ -f "$DEPLOYMENT_FILE" ]; then
  SAVED_MANAGER=$(jq -r '.contracts.DungeonManager // empty' "$DEPLOYMENT_FILE")
  if [ -n "$SAVED_MANAGER" ]; then
    # Verify contract exists
    CODE=$(cast code "$SAVED_MANAGER" --rpc-url "$RPC_URL" 2>/dev/null || echo "0x")
    if [ "$CODE" != "0x" ] && [ -n "$CODE" ]; then
      ALREADY_DEPLOYED=true
      log_info "Contracts already deployed, loading from $DEPLOYMENT_FILE"
      GOLD=$(jq -r '.contracts.Gold' "$DEPLOYMENT_FILE")
      NFT=$(jq -r '.contracts.DungeonNFT' "$DEPLOYMENT_FILE")
      TICKETS=$(jq -r '.contracts.DungeonTickets' "$DEPLOYMENT_FILE")
      MANAGER=$(jq -r '.contracts.DungeonManager' "$DEPLOYMENT_FILE")
    fi
  fi
fi

if [ "$ALREADY_DEPLOYED" = false ]; then
  log_info "Deploying contracts via forge script..."
  
  PRIVATE_KEY=$DEPLOYER_KEY forge script script/Deploy.s.sol \
    --rpc-url "$RPC_URL" \
    --broadcast \
    --slow
  
  # Parse deployed addresses from broadcast
  BROADCAST_FILE="$PROJECT_DIR/broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json"
  if [ ! -f "$BROADCAST_FILE" ]; then
    log_error "Broadcast file not found: $BROADCAST_FILE"
    exit 1
  fi
  
  GOLD=$(jq -r '.transactions[] | select(.contractName=="Gold") | .contractAddress' "$BROADCAST_FILE" | head -1)
  NFT=$(jq -r '.transactions[] | select(.contractName=="DungeonNFT") | .contractAddress' "$BROADCAST_FILE" | head -1)
  TICKETS=$(jq -r '.transactions[] | select(.contractName=="DungeonTickets") | .contractAddress' "$BROADCAST_FILE" | head -1)
  MANAGER=$(jq -r '.transactions[] | select(.contractName=="DungeonManager") | .contractAddress' "$BROADCAST_FILE" | head -1)
  
  log_success "Contracts deployed"
fi

echo "  Gold:           $GOLD"
echo "  DungeonNFT:     $NFT"
echo "  DungeonTickets: $TICKETS"
echo "  DungeonManager: $MANAGER"

# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Configure Roles (setMinter, setBurner)
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 3: Configure Roles"

# Check if already configured (case-insensitive comparison)
CURRENT_MINTER=$(cast call "$GOLD" "minter()(address)" --rpc-url "$RPC_URL" 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "0x0")
MANAGER_LOWER=$(echo "$MANAGER" | tr '[:upper:]' '[:lower:]')

if [ "$CURRENT_MINTER" = "$MANAGER_LOWER" ]; then
  log_success "Gold.minter already set to DungeonManager"
else
  safe_send "Setting Gold.minter to DungeonManager" "$GOLD" "setMinter(address)" "$MANAGER"
fi

CURRENT_BURNER=$(cast call "$TICKETS" "burner()(address)" --rpc-url "$RPC_URL" 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "0x0")
if [ "$CURRENT_BURNER" = "$MANAGER_LOWER" ]; then
  log_success "Tickets.burner already set to DungeonManager"
else
  safe_send "Setting Tickets.burner to DungeonManager" "$TICKETS" "setBurner(address)" "$MANAGER"
fi

# Verify configuration
IS_CONFIGURED=$(cast call "$MANAGER" "isConfigured()(bool)" --rpc-url "$RPC_URL" 2>/dev/null || echo "false")
if [ "$IS_CONFIGURED" = "true" ]; then
  log_success "DungeonManager.isConfigured() = true"
else
  log_warn "Configuration check returned: $IS_CONFIGURED"
  log_warn "Continuing anyway (may need manual verification)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 4: Upload Skills
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 4: Upload Skills"

SKILLS_DIR="$PROJECT_DIR/skills"
SKILL_COUNT=$(cast call "$MANAGER" "getSkillCount()(uint256)" --rpc-url "$RPC_URL" 2>/dev/null || echo "0")

if [ "$SKILL_COUNT" != "0" ]; then
  log_success "Skills already uploaded (count: $SKILL_COUNT)"
else
  if [ -d "$SKILLS_DIR" ]; then
    for skill_file in "$SKILLS_DIR"/*.md; do
      if [ -f "$skill_file" ]; then
        skill_name=$(basename "$skill_file" .md)
        log_info "Uploading skill: $skill_name"
        
        # Read file content safely with proper escaping
        # Use base64 encoding to handle special characters, then decode in cast
        skill_content=$(cat "$skill_file")
        
        # Check size limit (50KB = 50000 bytes)
        skill_size=${#skill_content}
        if [ "$skill_size" -gt 50000 ]; then
          log_warn "Skill '$skill_name' exceeds 50KB limit ($skill_size bytes), truncating..."
          skill_content="${skill_content:0:49900}..."
        fi
        
        # Use cast calldata to properly encode, then send raw
        # This handles all escaping properly
        calldata=$(cast calldata "addSkill(string,string)" "$skill_name" "$skill_content")
        
        result=$(cast send --json \
          --rpc-url "$RPC_URL" \
          --private-key "$DEPLOYER_KEY" \
          "$MANAGER" \
          "$calldata" 2>&1)
        
        if echo "$result" | jq -e '.status == "0x1"' >/dev/null 2>&1; then
          log_success "Uploaded skill: $skill_name (${skill_size} bytes)"
        else
          log_error "Failed to upload skill: $skill_name"
          echo "$result"
        fi
      fi
    done
  else
    log_warn "No skills directory found at $SKILLS_DIR"
  fi
fi

FINAL_SKILL_COUNT=$(cast call "$MANAGER" "getSkillCount()(uint256)" --rpc-url "$RPC_URL")
log_info "Total skills uploaded: $FINAL_SKILL_COUNT"

# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Mint Dungeons
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 5: Mint Dungeons"

# Theme enum: Cave=0, Forest=1, Crypt=2, Ruins=3, Abyss=4, Temple=5, Volcano=6, Glacier=7, Swamp=8, Shadow=9
# Rarity enum: Common=0, Rare=1, Epic=2, Legendary=3

# Dungeons to mint:
#   #0: Cave, Diff 5, Party 2, Common
#   #1: Crypt, Diff 8, Party 3, Rare
#   #2: Abyss (Void), Diff 10, Party 2, Legendary
#   #3: Forest, Diff 3, Party 2, Common
#   #4: Volcano, Diff 7, Party 3, Rare

declare -a DUNGEON_PARAMS=(
  "5 2 0 0"   # #0: Cave, Diff 5, Party 2, Common
  "8 3 2 1"   # #1: Crypt, Diff 8, Party 3, Rare
  "10 2 4 3"  # #2: Abyss, Diff 10, Party 2, Legendary
  "3 2 1 0"   # #3: Forest, Diff 3, Party 2, Common
  "7 3 6 1"   # #4: Volcano, Diff 7, Party 3, Rare
)

declare -a DUNGEON_NAMES=(
  "Cave (Diff 5, Party 2, Common)"
  "Crypt (Diff 8, Party 3, Rare)"
  "Abyss/Void (Diff 10, Party 2, Legendary)"
  "Forest (Diff 3, Party 2, Common)"
  "Volcano (Diff 7, Party 3, Rare)"
)

# Check how many dungeons already exist
NEXT_TOKEN_ID=$(cast call "$NFT" "nextTokenId()(uint256)" --rpc-url "$RPC_URL" 2>/dev/null || echo "0")
log_info "Current nextTokenId: $NEXT_TOKEN_ID"

if [ "$NEXT_TOKEN_ID" -ge 5 ]; then
  log_success "Dungeons already minted (nextTokenId >= 5)"
else
  for i in "${!DUNGEON_PARAMS[@]}"; do
    if [ "$i" -lt "$NEXT_TOKEN_ID" ]; then
      log_info "Dungeon #$i already exists, skipping"
      continue
    fi
    
    read -r diff party theme rarity <<< "${DUNGEON_PARAMS[$i]}"
    safe_send "Minting Dungeon #$i: ${DUNGEON_NAMES[$i]}" \
      "$NFT" \
      "mint(address,uint8,uint8,uint8,uint8)" \
      "$DEPLOYER" "$diff" "$party" "$theme" "$rarity"
  done
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 6: Register Test Wallets as Agents
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 6: Register Test Agents"

for i in "${!AGENTS[@]}"; do
  agent="${AGENTS[$i]}"
  is_registered=$(cast call "$MANAGER" "registeredAgents(address)(bool)" "$agent" --rpc-url "$RPC_URL" 2>/dev/null || echo "false")
  
  if [ "$is_registered" = "true" ]; then
    log_success "Agent #$((i+1)) already registered: ${agent:0:10}..."
  else
    safe_send "Registering Agent #$((i+1)): ${agent:0:10}..." \
      "$MANAGER" \
      "registerAgent(address)" \
      "$agent"
  fi
done

# ═══════════════════════════════════════════════════════════════════════════
# Step 7: Mint Tickets to Test Wallets
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 7: Mint Tickets"

TICKETS_PER_WALLET=10

for i in "${!AGENTS[@]}"; do
  agent="${AGENTS[$i]}"
  current_balance=$(cast call "$TICKETS" "balanceOf(address,uint256)(uint256)" "$agent" "0" --rpc-url "$RPC_URL" 2>/dev/null || echo "0")
  
  if [ "$current_balance" -ge "$TICKETS_PER_WALLET" ]; then
    log_success "Agent #$((i+1)) already has $current_balance tickets"
  else
    tickets_needed=$((TICKETS_PER_WALLET - current_balance))
    safe_send "Minting $tickets_needed tickets to Agent #$((i+1))" \
      "$TICKETS" \
      "mint(address,uint256)" \
      "$agent" "$tickets_needed"
  fi
done

# Also mint tickets to deployer for testing
DEPLOYER_TICKETS=$(cast call "$TICKETS" "balanceOf(address,uint256)(uint256)" "$DEPLOYER" "0" --rpc-url "$RPC_URL" 2>/dev/null || echo "0")
if [ "$DEPLOYER_TICKETS" -lt 10 ]; then
  safe_send "Minting tickets to deployer" "$TICKETS" "mint(address,uint256)" "$DEPLOYER" "10"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 8: Stake Dungeon #0
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 8: Stake Dungeon #0"

# Check if already staked
DUNGEON_COUNT=$(cast call "$MANAGER" "dungeonCount()(uint256)" --rpc-url "$RPC_URL" 2>/dev/null || echo "0")

if [ "$DUNGEON_COUNT" -ge 1 ]; then
  log_success "Dungeon #0 already staked (dungeonCount = $DUNGEON_COUNT)"
else
  # First approve the manager to transfer the NFT
  safe_send "Approving DungeonManager for NFT #0" \
    "$NFT" \
    "approve(address,uint256)" \
    "$MANAGER" "0"
  
  # Then stake
  safe_send "Staking Dungeon NFT #0" \
    "$MANAGER" \
    "stakeDungeon(uint256)" \
    "0"
  
  log_success "Dungeon #0 staked and ready for test runs"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 9: Save Deployment Manifest
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 9: Save Deployment Manifest"

BLOCK_NUMBER=$(cast block-number --rpc-url "$RPC_URL")

cat > "$DEPLOYMENT_FILE" << EOF
{
  "schemaVersion": 2,
  "environment": "local",
  "network": "anvil",
  "chainId": $CHAIN_ID,
  "rpc": "$RPC_URL",
  "deployer": "$DEPLOYER",
  "expectedDeployer": "$DEPLOYER",
  "deployedAt": "$(date -Iseconds)",
  "deploymentBlock": $BLOCK_NUMBER,
  "contracts": {
    "Gold": "$GOLD",
    "DungeonNFT": "$NFT",
    "DungeonTickets": "$TICKETS",
    "DungeonManager": "$MANAGER"
  },
  "configuration": {
    "goldMinter": "$MANAGER",
    "ticketsBurner": "$MANAGER",
    "ticketPrice": "100000000000000000000",
    "isConfigured": true
  },
  "testData": {
    "dungeons": [
      {"tokenId": 0, "difficulty": 5, "partySize": 2, "theme": "Cave", "rarity": "Common", "staked": true},
      {"tokenId": 1, "difficulty": 8, "partySize": 3, "theme": "Crypt", "rarity": "Rare", "staked": false},
      {"tokenId": 2, "difficulty": 10, "partySize": 2, "theme": "Abyss", "rarity": "Legendary", "staked": false},
      {"tokenId": 3, "difficulty": 3, "partySize": 2, "theme": "Forest", "rarity": "Common", "staked": false},
      {"tokenId": 4, "difficulty": 7, "partySize": 3, "theme": "Volcano", "rarity": "Rare", "staked": false}
    ],
    "agents": [
      {"address": "$AGENT1", "name": "Agent1", "tickets": 10},
      {"address": "$AGENT2", "name": "Agent2", "tickets": 10},
      {"address": "$AGENT3", "name": "Agent3", "tickets": 10},
      {"address": "$AGENT4", "name": "Agent4", "tickets": 10},
      {"address": "$AGENT5", "name": "Agent5", "tickets": 10}
    ],
    "stakedDungeons": [0],
    "skillCount": $FINAL_SKILL_COUNT
  },
  "anvilAccounts": {
    "deployer": {
      "address": "$DEPLOYER",
      "privateKey": "$DEPLOYER_KEY"
    },
    "agents": [
      {"address": "$AGENT1", "privateKey": "$AGENT1_KEY"},
      {"address": "$AGENT2", "privateKey": "$AGENT2_KEY"},
      {"address": "$AGENT3", "privateKey": "$AGENT3_KEY"},
      {"address": "$AGENT4", "privateKey": "$AGENT4_KEY"},
      {"address": "$AGENT5", "privateKey": "$AGENT5_KEY"}
    ]
  },
  "notes": "Local Anvil environment with full game setup. Dungeon #0 is staked and ready for test runs."
}
EOF

log_success "Deployment manifest saved to local-deployment.json"

# ═══════════════════════════════════════════════════════════════════════════
# Step 10: Save Anvil State
# ═══════════════════════════════════════════════════════════════════════════

log_step "Step 10: Save Anvil State"

cast rpc anvil_dumpState --rpc-url "$RPC_URL" > "$STATE_FILE" 2>/dev/null || true

if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
  STATE_SIZE=$(du -h "$STATE_FILE" | cut -f1)
  log_success "Anvil state saved to .anvil-state.json ($STATE_SIZE)"
  log_info "State will auto-load on next ./start-anvil.sh"
else
  log_warn "Could not save Anvil state (non-critical)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════

echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                    🎮 LOCAL SETUP COMPLETE 🎮                        ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║                                                                      ║"
echo "║  📜 Contracts:                                                       ║"
echo "║     Gold:           $GOLD                               ║"
echo "║     DungeonNFT:     $NFT                               ║"
echo "║     DungeonTickets: $TICKETS                               ║"
echo "║     DungeonManager: $MANAGER                               ║"
echo "║                                                                      ║"
echo "║  🏰 Dungeons: 5 minted, #0 staked and ready                          ║"
echo "║  🤖 Agents: 5 registered with 10 tickets each                        ║"
echo "║  📚 Skills: $FINAL_SKILL_COUNT uploaded                                               ║"
echo "║                                                                      ║"
echo "║  📄 Files created:                                                   ║"
echo "║     local-deployment.json  - Contract addresses & config             ║"
echo "║     .anvil-state.json      - Persistent state                        ║"
echo "║                                                                      ║"
echo "║  🚀 Ready to test! Dungeon #0 (Cave, Diff 5, Party 2) is staked.     ║"
echo "║                                                                      ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
