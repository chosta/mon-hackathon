"""Moltbook API client for agent verification."""
import time
import httpx
import structlog
from config import settings

logger = structlog.get_logger()

# Simple in-memory cache: {token: (agent_profile, expires_at)}
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutes


class MoltbookError(Exception):
    pass


async def verify_agent(token: str) -> dict:
    """Verify a Moltbook bearer token, return agent profile."""
    # Check cache
    if token in _cache:
        profile, expires = _cache[token]
        if time.time() < expires:
            return profile
        del _cache[token]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.moltbook_base_url}/agents/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as e:
        logger.error("moltbook_request_failed", error=str(e))
        raise MoltbookError(f"Failed to reach Moltbook API: {e}")

    if resp.status_code == 401:
        raise MoltbookError("Invalid Moltbook token")
    if resp.status_code != 200:
        raise MoltbookError(f"Moltbook API returned {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise MoltbookError("Moltbook verification failed")
    
    # Extract agent from nested response
    profile = data.get("agent", {})
    _cache[token] = (profile, time.time() + CACHE_TTL)
    logger.info("moltbook_verified", agent_id=profile.get("id"), name=profile.get("name"))
    return profile
