"""JWT session management and wallet linking."""
import time
import secrets
import jwt
import structlog
from eth_account.messages import encode_defunct
from web3 import Web3
from fastapi import HTTPException, Header
from config import settings

logger = structlog.get_logger()


def create_jwt(moltbook_id: str, agent_name: str = None) -> str:
    """Issue a short-lived JWT after Moltbook verification."""
    payload = {
        "sub": moltbook_id,
        "name": agent_name,
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.jwt_expiry_seconds,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_jwt(token: str) -> dict:
    """Decode and validate JWT."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def require_auth(authorization: str = Header(...)) -> dict:
    """FastAPI dependency: extract and validate JWT from Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    return decode_jwt(token)


def generate_nonce() -> str:
    """Generate a random nonce for wallet linking."""
    return secrets.token_hex(32)


def verify_signature(wallet_address: str, nonce: str, signature: str) -> bool:
    """Verify an EIP-191 signature."""
    try:
        message = encode_defunct(text=f"Link wallet to Dungeons Gateway\nNonce: {nonce}")
        w3 = Web3()
        recovered = w3.eth.account.recover_message(message, signature=signature)
        return recovered.lower() == wallet_address.lower()
    except Exception as e:
        logger.error("signature_verification_failed", error=str(e))
        return False
