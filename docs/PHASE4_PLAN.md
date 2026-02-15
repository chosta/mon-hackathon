# Phase 4: Security Fixes — Execution Plan

**Status:** DRAFT
**Date:** 2026-02-14

## Pre-Analysis: What Already Exists

| Requirement | Status | Notes |
|---|---|---|
| ReentrancyGuard | ✅ DONE | Applied to 6 functions (stake/unstake/enter/withdraw/flee/claimRoyalties) |
| MAX_GOLD_PER_ACTION | ✅ DONE | = 100, enforced in `_processDMAction` |
| MAX_ACTION_LENGTH | ⚠️ PARTIAL | = 1000 (plan says 500). Keep 1000 — more practical for D&D actions |
| Turn binding | ✅ DONE | turnIndex checks in submitAction + submitDMResponse |
| onlyRunner | ❌ TODO | No runner concept exists yet |
| MAX_GOLD_PER_SESSION | ⚠️ PARTIAL | `session.maxGold` exists (trait-based) but no global hard cap constant |
| Pausable | ❌ TODO | Not imported |

## Changes Required

### 4.1 onlyRunner Modifier (~15 lines added)

```solidity
// Add state variable
address public runner;

// Add event
event RunnerUpdated(address indexed newRunner);

// Add modifier
modifier onlyRunner() {
    require(msg.sender == runner, "Not authorized runner");
    _;
}

// Add setter
function setRunner(address _runner) external onlyOwner {
    runner = _runner;
    emit RunnerUpdated(_runner);
}
```

**Apply to these functions:**
- `submitAction` — currently checks `msg.sender == currentActor`. Change: runner calls on behalf of player, pass `player` param
- `submitDMResponse` — currently checks `msg.sender == dm`. Change: runner calls on behalf of DM

**DESIGN DECISION:** Two approaches:
1. **Runner-only**: submitAction/submitDMResponse become `onlyRunner`, runner passes player address as param
2. **Runner-or-self**: Allow both direct player calls AND runner relay

**Recommendation:** Option 1 (runner-only) for gateway functions. Simpler, matches the relay architecture. The gateway IS the runner.

**Signature changes:**
```solidity
// Before:
function submitAction(uint256 sessionId, uint256 turnIndex, string calldata action) external
// After:
function submitAction(uint256 sessionId, address player, uint256 turnIndex, string calldata action) external onlyRunner

// Before:
function submitDMResponse(uint256 sessionId, uint256 turnIndex, ...) external
// After:
function submitDMResponse(uint256 sessionId, address dm, uint256 turnIndex, ...) external onlyRunner
```

Then replace `msg.sender` with `player`/`dm` param inside those functions.

### 4.2 Hard Caps — Minimal Changes

- `MAX_GOLD_PER_SESSION`: Add `uint256 public constant MAX_GOLD_PER_SESSION = 500 ether;`
- Enforce: In `_processDMAction` REWARD_GOLD branch, add check against MAX_GOLD_PER_SESSION as a ceiling on top of the trait-based `maxGold`
- Keep MAX_ACTION_LENGTH at 1000 (already enforced)

Actually wait — the existing `maxGold` per session is `traits.difficulty * BASE_GOLD_RATE` where BASE_GOLD_RATE = 100. Difficulty is a uint8 (max 255), so max possible is 25,500. The plan says 500 ether = 500e18. These are on totally different scales (raw uint vs ether). The existing gold amounts are NOT in ether — they're raw amounts. So MAX_GOLD_PER_SESSION = 500 (not 500 ether) makes more sense as a hard ceiling. But the plan says "ether"... 

**Recommendation:** Use `500` (raw, no ether suffix) to match existing scale. MAX_GOLD_PER_ACTION is already `100` without ether. Add a `require(session.maxGold <= MAX_GOLD_PER_SESSION)` check in enterDungeon or clamp it.

### 4.3 Pausable (~10 lines added)

```solidity
import "@openzeppelin/contracts/utils/Pausable.sol";

contract DungeonManager is Ownable, ReentrancyGuard, Pausable {
```

Add `whenNotPaused` to:
- `enterDungeon` — prevent new entries
- `stakeDungeon` — prevent new stakes
- `submitAction` — prevent gameplay during emergency
- `submitDMResponse` — same

Add pause/unpause:
```solidity
function pause() external onlyOwner { _pause(); }
function unpause() external onlyOwner { _unpause(); }
```

**NOT paused:** `withdrawBond`, `claimRoyalties`, `flee` — users should always be able to withdraw funds.

### 4.4 ReentrancyGuard — Already Done ✅

No changes needed. Already applied to all ETH-transferring functions.

## Execution Steps

1. **Import Pausable**, add to inheritance chain
2. **Add `runner` state + modifier + setter + event**
3. **Modify `submitAction` signature** — add `player` param, add `onlyRunner`, replace `msg.sender`
4. **Modify `submitDMResponse` signature** — add `dm` param, add `onlyRunner`, replace `msg.sender`
5. **Add `MAX_GOLD_PER_SESSION = 500`**, enforce in `_processDMAction` or `enterDungeon`
6. **Add `whenNotPaused`** to enterDungeon, stakeDungeon, submitAction, submitDMResponse
7. **Add `pause()`/`unpause()`** functions
8. **Compile check** — `forge build`

## Size Impact Estimate

- Pausable inheritance: ~200 bytes
- Runner modifier + setter: ~150 bytes  
- Signature changes: ~100 bytes (adding address params)
- MAX_GOLD_PER_SESSION constant + check: ~50 bytes
- **Total: ~500 bytes** — well within 4KB margin

## Risk Assessment

- **Low risk:** Pausable, hard caps, ReentrancyGuard (already done)
- **Medium risk:** onlyRunner signature changes — breaks existing test calls, gateway integration must update
- **Mitigation:** Update test scripts and gateway relay to pass player address
