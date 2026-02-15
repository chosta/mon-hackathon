# Replay/Reordering Protection — Design Decision

**Status:** APPROVED for v0  
**Date:** 2026-02-13  
**Reviewed by:** Dredd

---

## Problem

- **Replay attack:** Valid tx submitted multiple times → double rewards
- **Reordering attack:** Txs processed out of intended order → state manipulation

---

## Chosen Solution: B3-A (Session + Turn Binding)

**Rating:** SOLID for v0 (with authorization)

### Implementation

```solidity
// In DungeonManager.sol

modifier onlyRunner() {
    require(msg.sender == runner, "Not authorized runner");
    _;
}

function submitDMResponse(
    uint256 sessionId,
    uint256 turnIndex,
    // ... other params
) external onlyRunner {
    require(turnIndex == sessions[sessionId].currentTurn, "Wrong turn");
    
    // ... process response ...
    
    sessions[sessionId].currentTurn++;
}

function submitAction(
    uint256 sessionId,
    uint256 turnIndex,
    string calldata action
) external {
    require(turnIndex == sessions[sessionId].currentTurn, "Wrong turn");
    
    // ... process action ...
    
    sessions[sessionId].currentTurn++;
}
```

### Why This Works

1. **Prevents replay:** Same turn can't be processed twice (turn increments after use)
2. **Prevents reordering:** Future turns can't be processed early (must match currentTurn)
3. **Simple:** 1 day implementation effort
4. **Authorization:** `onlyRunner` prevents front-running/griefing

---

## Alternatives Considered

### B3-B: Nonce Per Account
```solidity
mapping(address => uint256) public nonces;
require(nonce == nonces[msg.sender], "Invalid nonce");
```
**Rating:** WEAK  
**Why not:** Doesn't enforce session order, awkward with single runner

### B3-C: State Hash Binding
```solidity
require(action.priorStateHash == session.currentStateHash, "State mismatch");
session.currentStateHash = keccak256(abi.encode(...));
```
**Rating:** SOLID but overkill  
**Why not:** Extra complexity, easy to implement incorrectly, not needed for v0 with trusted runner

---

## Remaining Considerations

1. **Runner compromise:** All schemes assume runner is honest. Key custody (B2) addresses this.
2. **Multiple actions per turn:** If needed later, add `turnPhase` or sub-step tracking.
3. **Idempotency:** If runner retries after revert, B3-A handles it (turn didn't increment).

---

## Future Upgrade Path

If we move to multi-writer / trust-minimized sequencing:
- Add B3-C (state hash binding)
- Consider commit-reveal schemes
- Add per-action uniqueness tracking
