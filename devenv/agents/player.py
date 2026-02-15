"""Simple rule-based player agent."""
import random
from .base import BaseAgent


class PlayerAgent(BaseAgent):
    """Player that picks actions based on simple rules."""

    COMBAT_ACTIONS = [
        "I swing my sword at the nearest enemy!",
        "I cast fireball at the group of enemies!",
        "I shoot an arrow at the strongest foe!",
        "I charge forward with my shield raised!",
        "I use my dagger for a quick backstab!",
    ]

    EXPLORE_ACTIONS = [
        "I search the room for traps and treasure.",
        "I cautiously move forward, checking corners.",
        "I listen at the door for sounds.",
        "I examine the walls for hidden passages.",
        "I light a torch and look around carefully.",
    ]

    SOCIAL_ACTIONS = [
        "I try to negotiate with the creatures.",
        "I intimidate them with a fierce war cry!",
        "I offer gold in exchange for safe passage.",
    ]

    def decide_action(self, session_state: dict, narrative: str = "") -> str:
        """Pick an action based on context."""
        narrative_lower = narrative.lower() if narrative else ""

        # Combat keywords
        if any(w in narrative_lower for w in ["attack", "enemy", "goblin", "chief", "combat", "spots you", "charges"]):
            return random.choice(self.COMBAT_ACTIONS)

        # Trap/danger keywords
        if any(w in narrative_lower for w in ["trap", "pit", "danger", "click"]):
            return random.choice([
                "I try to dodge the trap!",
                "I brace myself and push through!",
                "I use my agility to leap aside!",
            ])

        # Treasure keywords
        if any(w in narrative_lower for w in ["treasure", "chest", "loot", "gold"]):
            return random.choice([
                "I carefully open the chest.",
                "I check for traps before grabbing the treasure.",
                "I collect the loot and look for more.",
            ])

        # Default: explore
        return random.choice(self.EXPLORE_ACTIONS)
