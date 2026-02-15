"""Pydantic request/response models."""
from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    token: str = Field(..., description="Moltbook bearer token")


class VerifyResponse(BaseModel):
    jwt: str
    agent_id: str
    agent_name: str


class NonceResponse(BaseModel):
    nonce: str
    message: str
    expires_in: int = 300


class LinkWalletRequest(BaseModel):
    wallet_address: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$")
    signature: str = Field(..., pattern=r"^0x[a-fA-F0-9]+$")


class LinkWalletResponse(BaseModel):
    success: bool
    wallet_address: str


class EnterDungeonRequest(BaseModel):
    dungeon_id: int = Field(..., ge=0)
    action_id: str = Field(..., max_length=64)


class SubmitActionRequest(BaseModel):
    session_id: int = Field(..., ge=0)
    turn_index: int = Field(..., ge=0)
    action: str = Field(..., max_length=500)
    action_id: str = Field(..., max_length=64)


class DMResponseAction(BaseModel):
    target: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$")
    xp_reward: int = Field(0, ge=0, le=100)
    gold_reward: int = Field(0, ge=0, le=1000)
    damage: int = Field(0, ge=0, le=100)
    is_killed: bool = False


class SubmitDMRequest(BaseModel):
    session_id: int = Field(..., ge=0)
    turn_index: int = Field(..., ge=0)
    narrative: str = Field(..., max_length=2000)
    actions: list[DMResponseAction] = Field(default_factory=list)
    is_complete: bool = False
    is_failed: bool = False
    action_id: str = Field(..., max_length=64)


class SimpleLinkRequest(BaseModel):
    wallet_address: str = Field(..., min_length=42, max_length=42)

class EnterBuildRequest(BaseModel):
    dungeon_id: int = Field(..., ge=0)

class AcceptDMRequest(BaseModel):
    session_id: int = Field(..., ge=0)
    dm_epoch: int = Field(..., ge=0)
    action_id: str = Field(..., max_length=64)


class SessionInfoResponse(BaseModel):
    session_id: int
    dungeon_id: int
    dm: str | None
    state: int
    state_name: str
    current_turn: int
    current_actor: str | None
    turn_deadline: int
    dm_epoch: int
    dm_accept_deadline: int
    last_activity: int
    gold_pool: int
    max_gold: int
    epoch_id: int

    class Config:
        from_attributes = True


class EpochInfoResponse(BaseModel):
    current_epoch: int
    epoch_state: str  # "Active" or "Grace"
    epoch_state_raw: int  # 0 or 1
    grace_start_time: int


class ErrorResponse(BaseModel):
    error: str
    message: str
    expected: int | None = None
    got: int | None = None
    current_state: int | None = None


class TxStatusResponse(BaseModel):
    id: int
    action_id: str
    status: str
    tx_hash: str | None = None
    error: str | None = None


class AgentStatsResponse(BaseModel):
    moltbook_id: str
    total_xp: int
    total_gold: int
    total_events: int
    on_chain: dict = {}


class HealthResponse(BaseModel):
    status: str
    db: bool
    rpc: bool
    runner_address: str | None = None
    attribution_model: str = "off-chain"


class LeaderboardEntry(BaseModel):
    rank: int
    moltbook_id: str
    display_name: str | None
    value: int
    level: str


class AgentFullStatsResponse(BaseModel):
    moltbook_id: str
    display_name: str | None = None
    total_xp: int = 0
    current_level: str = "novice"
    lifetime_sessions: int = 0
    lifetime_wins: int = 0
    lifetime_gold: int = 0
    dm_sessions: int = 0


class ActionLogEntry(BaseModel):
    id: int
    action_id: str
    session_id: int | None
    moltbook_id: str
    action_type: str
    epoch_id: int | None
    created_at: float
