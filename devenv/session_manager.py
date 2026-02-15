"""Manage persistent OpenClaw sessions for dungeon agents.

Uses openclaw agent --session-id for conversation persistence across turns.
The SessionManager wraps this with:
- Named session tracking (label -> session_id mapping)
- System prompt seeding on first use
- Per-session thread locks to prevent concurrent sends
- Model selection
"""
import json
import os
import subprocess
import threading
import uuid
from typing import Optional

OPENCLAW_BIN = os.path.expanduser("~/.npm-global/bin/openclaw")


class SessionManager:
    """Manages persistent agent sessions with model selection."""

    def __init__(self, model: str = "sonnet", thinking: str = "off"):
        self.model = model
        self.thinking = thinking
        self.sessions: dict[str, str] = {}  # label -> openclaw session-id
        self._seeded: set[str] = set()  # labels that have been seeded
        self._locks: dict[str, threading.Lock] = {}  # per-session locks

    def _get_lock(self, label: str) -> threading.Lock:
        if label not in self._locks:
            self._locks[label] = threading.Lock()
        return self._locks[label]

    def get_or_create(self, label: str, system_prompt: Optional[str] = None) -> str:
        """Get existing session or create new one. Returns session_id.
        
        If system_prompt is provided and session is new, seeds it.
        """
        if label not in self.sessions:
            sid = f"mon-hack:{label}:{uuid.uuid4().hex[:8]}"
            self.sessions[label] = sid
            if system_prompt:
                self.send(label, system_prompt)
                self._seeded.add(label)
        return self.sessions[label]

    def send(self, label: str, message: str, timeout_s: int = 180) -> str:
        """Send message to a session. Returns response text.
        
        Creates the session on first use if it doesn't exist yet.
        """
        if label not in self.sessions:
            raise RuntimeError(f"Session '{label}' not found. Call get_or_create() first.")

        session_id = self.sessions[label]
        lock = self._get_lock(label)

        with lock:
            cmd = [
                OPENCLAW_BIN, "agent",
                "--json",
                "--thinking", self.thinking,
                "--timeout", str(timeout_s),
                "--session-id", session_id,
                "-m", message,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"openclaw agent failed for session '{label}': {result.stderr[:500]}"
                )

            data = json.loads(result.stdout)
            payloads = data.get("payloads") or data.get("result", {}).get("payloads")
            if not payloads:
                raise RuntimeError(f"No payloads from session '{label}': {data}")
            text = payloads[0].get("text")
            if not isinstance(text, str):
                raise RuntimeError(f"Missing text in payload for '{label}': {payloads[0]}")
            return text.strip()

    def is_seeded(self, label: str) -> bool:
        """Check if a session has been seeded with its system prompt."""
        return label in self._seeded

    def list_sessions(self) -> dict[str, str]:
        """Return label -> session_id mapping."""
        return dict(self.sessions)

    def cleanup(self):
        """Clear local state. Sessions persist server-side."""
        self.sessions.clear()
        self._seeded.clear()
        self._locks.clear()
