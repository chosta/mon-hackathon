"""Scenario-driven DM agent."""
import random
from .base import BaseAgent


class DMAgent(BaseAgent):
    """DM that follows a scenario flow and generates responses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = None
        self.current_encounter_idx = 0
        self.encounter_history = []

    def load_scenario(self, scenario: dict):
        """Load a scenario config."""
        self.scenario = scenario
        self.current_encounter_idx = 0
        self.encounter_history = []

    def get_current_encounter(self) -> dict:
        """Get the current encounter from scenario."""
        if not self.scenario:
            return {"id": "default", "text": "You stand in a dark room.", "next": []}
        encounters = self.scenario.get("encounters", [])
        if self.current_encounter_idx >= len(encounters):
            return encounters[-1] if encounters else {"id": "end", "text": "The adventure concludes."}
        return encounters[self.current_encounter_idx]

    def advance_encounter(self, outcome: str = "next"):
        """Move to next encounter based on outcome."""
        enc = self.get_current_encounter()
        enc_id = enc.get("id", "")

        # Check for outcome-specific routing
        if outcome == "victory" and "on_victory" in enc:
            target = enc["on_victory"]
        elif outcome == "defeat" and "on_defeat" in enc:
            target = enc["on_defeat"]
        elif "next" in enc:
            nexts = enc["next"]
            if isinstance(nexts, list) and nexts:
                target = random.choice(nexts)
            elif isinstance(nexts, str):
                target = nexts
            else:
                self.current_encounter_idx += 1
                return
        else:
            self.current_encounter_idx += 1
            return

        # Find target encounter by id
        encounters = self.scenario.get("encounters", [])
        for i, e in enumerate(encounters):
            if e.get("id") == target:
                self.current_encounter_idx = i
                return
        # Fallback: just advance
        self.current_encounter_idx += 1

    def run_encounter(self, party: list[str]) -> tuple[str, list[dict]]:
        """Generate narrative and DM actions for current encounter.
        
        Returns (narrative, actions) where actions is list of dicts for the gateway.
        """
        enc = self.get_current_encounter()
        enc_id = enc.get("id", "unknown")
        text = enc.get("text", "You see nothing special.")
        actions = []

        self.encounter_history.append(enc_id)

        # Complete/fail encounters end the session
        if enc_id == "complete":
            gold_range = enc.get("gold", [50, 100])
            xp_range = enc.get("xp", [30, 50])
            for player in party:
                actions.append({
                    "target": player,
                    "gold_reward": random.randint(*gold_range),
                    "xp_reward": random.randint(*xp_range),
                    "damage": 0,
                    "is_killed": False,
                })
            narrative = f"🏆 {text} Each hero is rewarded!"
            return narrative, actions

        if enc_id == "fail":
            narrative = f"💀 {text}"
            # Kill remaining players to end session
            for player in party:
                actions.append({
                    "target": player,
                    "gold_reward": 0,
                    "xp_reward": 5,  # consolation XP
                    "damage": 100,
                    "is_killed": True,
                })
            return narrative, actions

        # Combat encounters
        if "enemies" in enc:
            enemies = enc["enemies"]
            enemy_list = ", ".join(enemies)
            narrative = f"⚔️ {text}\n\nEnemies: {enemy_list}\n\nThe party fights bravely!"

            # Players take some damage, get rewards on victory
            is_boss = enc.get("is_boss", False)
            for player in party:
                dmg = random.randint(5, 20) if is_boss else random.randint(1, 10)
                actions.append({
                    "target": player,
                    "gold_reward": random.randint(5, 15),
                    "xp_reward": random.randint(10, 25) if is_boss else random.randint(5, 15),
                    "damage": dmg,
                    "is_killed": False,
                })

            # Victory by default (players win combat encounters)
            self.advance_encounter("victory")
            return narrative, actions

        # Trap encounters
        if "damage" in enc:
            dmg_range = enc["damage"]
            narrative = f"⚠️ {text}"
            for player in party:
                dmg = random.randint(*dmg_range)
                actions.append({
                    "target": player,
                    "gold_reward": 0,
                    "xp_reward": 5,
                    "damage": dmg,
                    "is_killed": False,
                })
            self.advance_encounter("next")
            return narrative, actions

        # Treasure encounters
        if "gold" in enc:
            gold_range = enc["gold"]
            narrative = f"💰 {text}"
            for player in party:
                actions.append({
                    "target": player,
                    "gold_reward": random.randint(*gold_range),
                    "xp_reward": random.randint(5, 10),
                    "damage": 0,
                    "is_killed": False,
                })
            self.advance_encounter("next")
            return narrative, actions

        # Default: narrative-only encounter, advance
        narrative = f"📖 {text}"
        for player in party:
            actions.append({
                "target": player,
                "gold_reward": 0,
                "xp_reward": 5,
                "damage": 0,
                "is_killed": False,
            })
        self.advance_encounter("next")
        return narrative, actions

    def is_scenario_complete(self) -> bool:
        """Check if we've hit a terminal encounter."""
        enc = self.get_current_encounter()
        return enc.get("id") in ("complete", "fail")
