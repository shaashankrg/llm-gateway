import pytest
import httpx

API_KEY = "team-a-key"

METRICS_URL = "http://llm-gateway:8000/metrics/"


async def _fallback_triggered_count(anthropic_to_openai: bool = True) -> float:
    async with httpx.AsyncClient() as client:
        text = (await client.get(METRICS_URL)).text
    from_provider, to_provider = ("anthropic", "openai") if anthropic_to_openai else ("openai", "anthropic")
    for line in text.splitlines():
        if (
            line.startswith("gateway_fallback_triggered_total{")
            and f'from_provider="{from_provider}"' in line
            and f'to_provider="{to_provider}"' in line
        ):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


@pytest.mark.asyncio
async def test_primary_retry_exhaustion_falls_back_to_second_provider_same_request(test_client, redis_client):
    """
    Forces call_anthropic to fail (a retryable 503) for all 3 attempts inside
    call_with_retry, so the primary provider's retries are genuinely exhausted
    within this one request — not just "the circuit was already open from a
    prior request." Confirms the request still succeeds end-to-end via the
    same-request fallback added in queue.py's attempt_provider/worker fix,
    using two independent signals: the response's own `model` field (which
    attempt_provider swaps to the fallback model before calling it) and the
    gateway_fallback_triggered_total counter in /metrics.

    Note: circuit_breakers state is in-memory per gateway process, not reset
    by redis_client's flushdb. This test assumes anthropic's circuit starts
    CLOSED (true for a freshly-built gateway, or after any prior successful
    anthropic call) — if a prior test in the same run left it OPEN, this test
    would still pass (fallback still fires) but wouldn't be exercising retry
    exhaustion specifically, just the already-open-circuit path.
    """
    before = await _fallback_triggered_count(anthropic_to_openai=True)

    redis_client.set("mock:fail_count:anthropic", 3)  # exhausts all 3 call_with_retry attempts

    response = await test_client.post(
        "/generate",
        json={"model": "claude-sonnet-5", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    )

    assert response.status_code == 200, (
        f"expected the request to succeed via same-request fallback, got {response.status_code}: "
        f"{response.text}"
    )

    body = response.json()
    assert body["model"] == "gpt-4o-mini", (
        f"expected the response to reflect the fallback model (gpt-4o-mini), got {body['model']!r} — "
        f"this would mean the request was served by anthropic, not the fallback"
    )

    after = await _fallback_triggered_count(anthropic_to_openai=True)
    assert after == before + 1, (
        f"expected gateway_fallback_triggered_total{{from=anthropic,to=openai}} to increment by exactly 1, "
        f"went from {before} to {after}"
    )
