#!/usr/bin/env python3
"""Debug script to understand session joining behavior."""

import sys
import time
sys.path.append("./devenv")

from agents.base import BaseAgent

# Use the same agent setup as the test
agents = []
agent_data = [
    ("0x70997970C51812dc3A010C7d01b50e0d17dc79C8", "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"),
    ("0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC", "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"),
    ("0x90F79bf6EB2c4f870365E785982E1f101E93b906", "0x7c852118294e51e653712a81e5f6b20f4bee5e1e8e4daf9883010f2bb287b31"),
]

for i, (address, private_key) in enumerate(agent_data):
    agents.append(BaseAgent(address, private_key, f"Agent-{i+1}"))

print(f"Created {len(agents)} agents")
print()

# Try to enter dungeon 0 (party size 2) with timing
print("=== Attempting to enter dungeon 0 (party size 2) ===")
session_ids = []

for i, agent in enumerate(agents[:2]):  # Only use first 2 agents for dungeon 0
    print(f"Agent {i+1} entering...")
    try:
        sid = agent.enter_dungeon(0)
        session_ids.append(sid)
        print(f"  → Session ID: {sid}")
        
        # Get session info
        info = agent.get_session(sid)
        party = agent.get_party(sid)
        print(f"  → State: {info['state_name']}")
        print(f"  → Party size: {len(party)}")
        print(f"  → Party: {[p[:10] + '...' for p in party]}")
        print()
        
        # Small delay
        time.sleep(2)
    except Exception as e:
        print(f"  → Error: {e}")
        print()

print(f"Session IDs created: {session_ids}")
print(f"Unique sessions: {len(set(session_ids))}")

if len(set(session_ids)) == 1:
    print("✅ SUCCESS: Both agents joined the same session!")
else:
    print("❌ PROBLEM: Agents created separate sessions")

# Check if we can see all sessions
print()
print("=== Current Sessions ===")
for sid in set(session_ids):
    info = agents[0].get_session(sid)
    party = agents[0].get_party(sid)
    print(f"Session {sid}:")
    print(f"  State: {info['state_name']}")
    print(f"  Party: {len(party)} members")
    print()