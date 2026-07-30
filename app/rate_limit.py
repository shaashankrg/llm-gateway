import os
import time
from pathlib import Path

import redis.asyncio as redis
from fastapi import Depends, HTTPException

from app.auth import get_team

# REDIS_URL lets a hosted deployment point at a managed Redis (which is usually
# a rediss:// URL with credentials). Defaults to the Compose service name so
# local `docker compose up` keeps working with no env var set.
r = redis.from_url(
    os.environ.get("REDIS_URL", "redis://redis:6379"), decode_responses=True
)

# Load the token-bucket Lua script relative to this file, not the process CWD,
# so it's found regardless of where uvicorn is launched from.
_LUA_PATH = Path(__file__).parent / "rate_limiter.lua"
RATE_LIMIT_SCRIPT = r.register_script(_LUA_PATH.read_text())

# Token bucket: burst of up to CAPACITY requests, refilling at REFILL_RATE/sec.
CAPACITY = 10
REFILL_RATE = 10 / 60  # 10 tokens per minute


async def check_rate_limit(team: dict = Depends(get_team)) -> dict:
    key = f"ratelimit:{team['team_id']}"
    allowed = await RATE_LIMIT_SCRIPT(keys=[key], args=[CAPACITY, REFILL_RATE, time.time()])
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": "6"},
        )
    return team
