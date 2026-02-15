# Phase 3: Epoch System — Execution Plan

**Status:** REVIEWED  
**Date:** 2026-02-14  
**Estimated effort:** 3-4 hours (code + tests)  
**Deadline context:** ~38h remaining

---

## Overview

Add an epoch system to DungeonManager that allows the owner to cycle between Active and Grace periods. During Active epochs, players can enter dungeons. During Grace periods, dungeon owners can stake/unstake NFTs and parameters can be updated.

## Current State Analysis

**DungeonManager.sol** (~550 lines) already has:
- ✅ ReentrancyGuard, Ownable
- ✅ Bonds, DM selection, turn binding, session timeouts
- ✅ `activeSessionCount` tracking
- ✅ Skill management (stored on-chain, locked during active sessions)
- ❌ No epoch concept — skills locked by `activeSessionCount > 0` (crude)
- ❌ Stake/unstake available anytime (no grace gating)
- ❌ No session-to-epoch pinning

## Changes Required

### Step 1: Add Epoch State Variables

```solidity
// After existing constants
enum EpochState { Active, Grace }

uint256 public currentEpoch;
EpochState public epochState = EpochState.Active;
uint256 public graceStartTime;
uint256 public constant EPOCH_DURATION = 7 days;       // informational only (owner triggers)
uint256 public constant MAX_GRACE_PERIOD = 48 hours;

// Epoch-pinned config snapshots
mapping(uint256 => bytes32) public epochSkillHash;      // hash of skills at epoch start
mapping(uint256 => uint256) public epochDmFee;           // DM fee % at epoch start
```

**Events:**
```solidity
event EpochEnded(uint256 indexed epoch);
event EpochStarted(uint256 indexed epoch, bytes32 skillHash, uint256 dmFee);
```

**Errors:**
```solidity
error EpochNotActive();
error EpochNotGrace();
error GracePeriodActive();      // sessions still active and <48h
```

### Step 2: Add Epoch Transition Functions

```solidity
function endEpoch() external onlyOwner {
    if (epochState != EpochState.Active) revert EpochNotActive();
    epochState = EpochState.Grace;
    graceStartTime = block.timestamp;
    emit EpochEnded(currentEpoch);
}

function startEpoch() external onlyOwner {
    if (epochState != EpochState.Grace) revert EpochNotGrace();
    // Either all sessions drained, or 48h hard timeout
    if (activeSessionCount > 0 && block.timestamp <= graceStartTime + MAX_GRACE_PERIOD) {
        revert GracePeriodActive();
    }
    
    currentEpoch++;
    epochState = EpochState.Active;
    epochSkillHash[currentEpoch] = _computeSkillHash();
    epochDmFee[currentEpoch] = DM_FEE_PERCENT;  // snapshot current fee
    emit EpochStarted(currentEpoch, epochSkillHash[currentEpoch], DM_FEE_PERCENT);
}

function _computeSkillHash() internal view returns (bytes32) {
    // Hash all skill contents for integrity pinning
    bytes memory packed;
    for (uint256 i = 0; i < skills.length; i++) {
        packed = abi.encodePacked(packed, skills[i].content);
    }
    return keccak256(packed);
}
```

**Design decision:** `DM_FEE_PERCENT` is currently a constant (15). For epoch-variable fees, we'd need to make it a state variable. For hackathon scope, we snapshot the constant — this is forward-compatible.

### Step 3: Modify Session Struct

Add `epochId` to Session:

```solidity
struct Session {
    // ... existing fields ...
    uint256 epochId;        // NEW: which epoch this session belongs to
}
```

### Step 4: Gate `enterDungeon` to Active Epoch

In `enterDungeon()`, add at the top:
```solidity
if (epochState != EpochState.Active) revert EpochNotActive();
```

When creating a new session, pin the epoch:
```solidity
sessions[sessionId].epochId = currentEpoch;
```

### Step 5: Gate `stakeDungeon` / `unstakeDungeon` to Grace Period

In `stakeDungeon()`:
```solidity
if (epochState != EpochState.Grace) revert EpochNotGrace();
```

In `unstakeDungeon()`:
```solidity
if (epochState != EpochState.Grace) revert EpochNotGrace();
```

### Step 6: Update Skill Management

Replace the crude `activeSessionCount > 0` lock with epoch gating:
```solidity
function updateSkill(...) external onlyOwner {
    // ...
    if (epochState != EpochState.Grace) revert EpochNotGrace();  // replaces: if (activeSessionCount > 0) revert SkillLocked();
    // ...
}
```

### Step 7: Initialize Epoch 0

In constructor or via a separate `initializeEpoch()`:
```solidity
// In constructor, after existing init:
currentEpoch = 0;
epochState = EpochState.Active;
// epochSkillHash[0] and epochDmFee[0] set to 0 — first real epoch starts at 1
```

**Alternative:** Start in Grace so owner must explicitly `startEpoch()` to begin. This is cleaner — forces explicit initialization.

**Decision:** Start in Grace. Owner calls `startEpoch()` after staking dungeons and configuring skills. This matches the intended flow.

```solidity
// Constructor change:
epochState = EpochState.Grace;
graceStartTime = block.timestamp;
```

---

## Test Plan

### New Tests (~10-12 tests)

1. **`test_endEpoch_success`** — Owner can end active epoch → state becomes Grace
2. **`test_endEpoch_revert_notActive`** — Reverts if already in Grace
3. **`test_endEpoch_revert_notOwner`** — Only owner
4. **`test_startEpoch_success`** — Owner starts epoch from Grace (no active sessions)
5. **`test_startEpoch_afterTimeout`** — Starts after 48h even with active sessions
6. **`test_startEpoch_revert_sessionsActive`** — Reverts if sessions active and <48h
7. **`test_startEpoch_revert_notGrace`** — Reverts if in Active
8. **`test_enterDungeon_revert_epochNotActive`** — Can't enter during Grace
9. **`test_stakeDungeon_revert_epochNotGrace`** — Can't stake during Active
10. **`test_unstakeDungeon_revert_epochNotGrace`** — Can't unstake during Active
11. **`test_updateSkill_revert_epochNotGrace`** — Skills only updatable in Grace
12. **`test_epochPinning`** — Session records correct epochId
13. **`test_skillHash_computed`** — epochSkillHash set on startEpoch

### Modified Existing Tests

All existing tests that call `enterDungeon` or `stakeDungeon` need epoch state setup:
- Need a helper: `_setupActiveEpoch()` that transitions Grace → Active
- Or modify `setUp()` to start in Active epoch

**Strategy:** Add a test helper that calls `startEpoch()` in setUp. Since constructor starts in Grace, every test setUp must call `startEpoch()` first. This is a **breaking change for all existing tests** — but it's a simple one-liner fix.

---

## Implementation Order

1. **Add state variables + events + errors** (5 min)
2. **Add `endEpoch()` / `startEpoch()` / `_computeSkillHash()`** (15 min)
3. **Add `epochId` to Session struct** (5 min)
4. **Gate `enterDungeon`** (5 min)
5. **Gate `stakeDungeon` / `unstakeDungeon`** (5 min)
6. **Update skill management** (5 min)
7. **Change constructor to start in Grace** (5 min)
8. **Fix existing tests** — add `startEpoch()` to setUp flows (20 min)
9. **Write new epoch tests** (45 min)
10. **Run full test suite, fix any regressions** (15 min)

**Total: ~2 hours**

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Breaking existing tests | Medium | Systematic: add epoch setup to all test setups |
| Constructor starts in Grace = can't enter immediately | Low | Intended — forces explicit epoch start |
| `_computeSkillHash` gas cost with many skills | Low | Hackathon scope = few skills; optimize later |
| 48h timeout allows starting epoch with orphaned sessions | Medium | Acceptable — sessions can still complete/timeout independently |
| `activeSessionCount` accuracy | High | Already tracked correctly; epoch transitions depend on it |

---

## Files Modified

1. `contracts/DungeonManager.sol` — All epoch logic
2. `test/DungeonManager.t.sol` — New tests + fix existing setUp

## Review Notes (Self-Review)

**Edge cases identified:**
1. **`endEpoch` with Waiting/WaitingDM sessions** — Sessions remain in limbo but session timeout (4h) + grace hard timeout (48h) cover cleanup. No code change needed.
2. **Cross-epoch session completion** — Sessions from epoch N can complete during Grace or epoch N+1. `activeSessionCount` tracks correctly regardless. No issue.
3. **Add test: `test_sessionCompletionDuringGrace`** — Verify sessions started in epoch N can complete after `endEpoch()` is called.
4. **`_computeSkillHash` gas** — O(n) over skill contents. Acceptable for hackathon (<10 skills). For production, store incremental hash.

**No blocking issues found. Plan is sound for hackathon scope.**

---

## Files NOT Modified

- Gold.sol, DungeonNFT.sol, DungeonTickets.sol — No changes needed
- Deploy scripts — Can update later if needed
- Gateway — No changes for Phase 3 (epoch transitions are owner-only admin calls)
