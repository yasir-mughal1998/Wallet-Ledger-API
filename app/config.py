"""Runtime configuration, read from environment variables only."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_token: str
    database_url: str
    db_pool_size: int


def load_settings() -> Settings:
    api_token = os.environ.get("API_TOKEN", "")
    if not api_token:
        # Refuse to start rather than run with no (or an empty) token.
        raise RuntimeError("API_TOKEN environment variable must be set")

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable must be set")

    return Settings(
        api_token=api_token,
        database_url=database_url,
        db_pool_size=int(os.environ.get("DB_POOL_SIZE", "12")),
    )
