import time

import redis
import pytest
import pytest_asyncio
import httpx

GATEWAY_URL = "http://llm-gateway:8000"
REDIS_HOST = "redis"


def _wait_for_gateway(timeout_seconds: float = 30.0) -> None:
    # `depends_on` in compose only waits for the container to start, not for
    # uvicorn/redis inside it to actually accept connections — poll /healthz
    # for real readiness instead of racing a fresh container on test startup.
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{GATEWAY_URL}/healthz", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_error = e
        time.sleep(0.5)
    raise RuntimeError(f"gateway never became ready at {GATEWAY_URL}/healthz: {last_error}")


@pytest.fixture
def redis_client():
    client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    yield client
    client.flushdb()  # clean slate — don't let one test's tokens bleed into the next
    client.close()


@pytest_asyncio.fixture
async def test_client():
    _wait_for_gateway()
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30) as client:
        yield client
