"""Gateway configuration from environment variables."""
from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import os

# Load .env file from same directory
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


class Settings(BaseSettings):
    # Monad
    rpc_url: str = "https://testnet-rpc.monad.xyz"
    chain_id: int = 10143
    runner_private_key: str = ""  # Gateway's hot wallet

    # Contract addresses
    dungeon_manager: str = "0xabe034855504c12D0145234dC17816b02E5447d5"
    gold_contract: str = "0x3b7829Ae6dAc9765cf3951511256d302df8769AF"
    dungeon_nft: str = "0x2983C6A920BEF86b87edC08b10AaaAd35FB2Ac9e"
    dungeon_tickets: str = "0x90BEE1bC382243102366132B314efB0e2AF1CC03"

    # Moltbook
    moltbook_base_url: str = "https://www.moltbook.com/api/v1"

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_seconds: int = 3600  # 1 hour

    # Rate limits
    max_tx_per_hour: int = 10

    # DB
    db_path: str = "gateway.db"
    database_url: str = ""  # e.g. postgresql://user:pass@host/db — empty = use sqlite with db_path

    model_config = {"env_prefix": "GW_"}


settings = Settings()
