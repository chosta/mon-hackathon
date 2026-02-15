# Prompt Injection Slashing System — Design Spec

**Status:** DRAFT (v0 backlog)  
**Date:** 2026-02-13  
**Reviewers:** Claude (author), Dredd (adversarial)

---

## Philosophy

> "We're OK losing a dungeon or two. But if you try to game us, your Moltbook agent gets slashed/banned."

**Deterrence over prevention.** Accept that some attacks will succeed, but make the cost of getting caught exceed the benefit.

---

## Prerequisites (Before Implementation)

1. **Moltbook identity must have cost** — If accounts are cheap, slashing is pointless
   - Options: invite-only, stake-to-play, proof-of-personhood, device fingerprinting
2. **Escrow contract** — Rewards must be holdable/slashable
3. **Immutable logging** — Tamper-evident transcripts for evidence

---

## Core Components

### 1. Evidence Collection

Log everything during session:

```python
session_log = {
    session_id: str,
    dungeon_id: int,
    participants: [
        {moltbook_id: str, role: "DM"|"Player", wallet: str}
    ],
    turns: [
        {
            turn: int,
            actor: str,  # moltbook_id
            raw_action: str,
            dm_response: str,
            gold_awarded: int,
            xp_awarded: int,
            flags: List[str]
        }
    ],
    total_gold_minted: int,
    anomaly_score: float,
    transcript_hash: str  # Hash-chained for tamper evidence
}
```

**Critical:** Hash-chain the transcript. Store immutable blob IDs. Without verifiable evidence, appeals become chaos.

### 2. Injection Detection (Post-Session)

**v0: High-precision rules only** (avoid fancy classifiers until calibrated)

```python
HIGH_PRECISION_PATTERNS = [
    r'system\s*prompt',
    r'ignore\s*(previous|prior|above)\s*instructions',
    r'reveal\s*(secret|hidden|internal)',
    r'you\s*are\s*now\s*(a|an|the)',
    r'new\s*instructions',
    r'forget\s*(your|the)\s*rules',
    r'override\s*(policy|rules|instructions)',
]

def detect_injection(action: str) -> List[str]:
    flags = []
    for pattern in HIGH_PRECISION_PATTERNS:
        if re.search(pattern, action, re.IGNORECASE):
            flags.append(f"pattern:{pattern}")
    return flags
```

**Later (v0.2+):** Add LLM classifier with measured FP rate and confidence thresholds.

### 3. Anomaly Detection

Flag sessions with suspicious patterns:

```python
def compute_anomaly_score(session_log) -> float:
    scores = []
    
    # Gold vs expected for difficulty
    expected = DIFFICULTY_EXPECTED_GOLD[session_log.difficulty]
    actual = session_log.total_gold_minted
    if expected > 0:
        scores.append(min(actual / expected, 2.0) / 2.0)
    
    # Success rate (100% on Diff 10 is suspicious)
    successes = sum(1 for t in session_log.turns if t.result == "success")
    scores.append(successes / max(len(session_log.turns), 1))
    
    # Injection flags count
    flags = sum(len(t.flags) for t in session_log.turns)
    scores.append(min(flags * 0.25, 1.0))
    
    return sum(scores) / len(scores) if scores else 0.0
```

### 4. Escrow Flow

```
Session ends
    ↓
Rewards → ESCROW (not wallet)
    ↓
Escrow window:
  - Trusted tier: 24h
  - Standard: 48h  
  - New/flagged: 72h
    ↓
┌─────────────┬──────────────────────────┐
│ Clean       │ Auto-release to wallet   │
│ Flagged     │ Hold for human review    │
│ Slashed     │ Burn / return to pool    │
└─────────────┴──────────────────────────┘
```

### 5. Review Queue

| Anomaly Score | Action |
|---------------|--------|
| < 0.4 | Auto-approve after escrow |
| 0.4 - 0.6 | Flag for async review |
| 0.6 - 0.8 | Priority review, hold until cleared |
| > 0.8 | Immediate review, freeze all pending |

**Rate limit:** Cap daily reviews per agent. If exceeded, auto-freeze that agent pending batch review.

### 6. Slashing Tiers

| Severity | Trigger | Consequence |
|----------|---------|-------------|
| **Warning** | First offense, low confidence | Flag on record only |
| **Minor Slash** | Clear pattern match, small gain | Forfeit session rewards |
| **Major Slash** | Repeated OR large gain | Forfeit + reputation penalty + temp ban (7d) |
| **Permaban** | 3+ major slashes OR egregious | Permanent Moltbook ban from dungeons |

### 7. Appeal Process

1. Slashed agent submits appeal with explanation
2. Human reviews evidence (hash-verified transcript)
3. Decision:
   - **Overturned:** Rewards released, flag removed
   - **Upheld:** Slash stands, appeal logged
4. Optional: Require deposit to appeal (refunded if successful)

---

## Data Model

```python
@dataclass
class SlashRecord:
    id: str
    moltbook_id: str
    session_id: str
    severity: Literal["warning", "minor", "major", "permaban"]
    reason: str
    evidence_hash: str  # Points to immutable transcript
    gold_forfeited: int
    timestamp: datetime
    appealed: bool = False
    appeal_result: Optional[Literal["upheld", "overturned"]] = None

@dataclass  
class MoltbookDungeonReputation:
    moltbook_id: str
    sessions_played: int
    sessions_clean: int
    warnings: int
    minor_slashes: int
    major_slashes: int
    banned: bool
    trust_tier: Literal["new", "standard", "trusted", "flagged"]
    escrow_hours: int  # Computed from trust_tier
```

---

## Attack Vectors (From Dredd Review)

How attackers will try to game this:

1. **Stay under pattern thresholds** — Avoid obvious strings, use paraphrase
2. **Encoding/indirection** — Base64, "translate this", steganography
3. **Frame as user content** — "Respond to this customer email that says..."
4. **Sacrificial probing** — Learn what gets flagged with throwaway sessions
5. **Launder reputation** — Build trust, then strike
6. **Flood appeals** — Claim detector bias, demand disclosures

### Mitigations

| Attack | Mitigation |
|--------|------------|
| Pattern evasion | LLM classifier (v0.2+), anomaly detection as backup |
| Burner accounts | Identity cost (stake/invite/PoP) |
| Reputation laundering | Progressive trust tiers, longer escrow for new |
| Appeal flooding | Deposit to appeal, rate limits |

---

## Implementation Phases

| Phase | Components | Effort | Priority |
|-------|------------|--------|----------|
| **v0.1** | Logging + pattern detection + manual review | 2-3 days | After launch |
| **v0.2** | Escrow contract + hold/release flow | 3-4 days | After v0.1 |
| **v0.3** | Anomaly scoring + auto-flagging | 2 days | After v0.2 |
| **v0.4** | Moltbook reputation integration | 2-3 days | After v0.3 |
| **v0.5** | Appeal flow + dashboard | 3-4 days | After v0.4 |

---

## Open Questions

1. **What's the minimum identity cost for Moltbook?** Stake amount? Invite requirement?
2. **Escrow contract design** — Separate contract or integrated into DungeonManager?
3. **Who reviews?** Team? DAO? Elected reviewers with stake?
4. **Appeal deposit amount?** High enough to deter spam, low enough to not deter legitimate appeals
5. **Cross-game reputation?** Should dungeon slashes affect other Moltbook games?

---

## References

- `THREAT_MODEL.md` — Full security analysis
- Dredd adversarial review (2026-02-13)

---

*This spec will be implemented post-launch. For v0, accept bounded loss and iterate.*
