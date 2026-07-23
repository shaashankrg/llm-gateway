import asyncio

import httpx
import pytest

API_KEY = "team-a-key"
TEAM_ID = "team-a"

# calculate_cost("gpt-4", 1, 1) with the mock's fixed 1 input / 1 output tokens
MOCK_STREAM_COST = 1 * 0.00003 + 1 * 0.00006


async def _poll_spend(redis_client, timeout_seconds: float = 5.0) -> float:
    # Budget reconciliation runs in the `finally` block of _tracked_stream,
    # which fires right as the generator finishes closing out — there's a
    # small window between "client finished reading the stream" and "server
    # finished reconciling the reservation," so poll rather than reading once.
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        value = redis_client.get(f"spend:{TEAM_ID}:daily")
        if value is not None:
            return float(value)
        await asyncio.sleep(0.1)
    return 0.0


@pytest.mark.asyncio
async def test_stream_chunks_reconstruct_full_text_in_order(test_client, redis_client):
    """
    Confirms the streamed chunks, concatenated in the order received, produce
    the exact mock text with nothing dropped, duplicated, or reordered.
    """
    async with test_client.stream(
        "POST",
        "/generate/stream",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    ) as response:
        assert response.status_code == 200
        chunks = [chunk async for chunk in response.aiter_text()]

    assert "".join(chunks) == "mock openai stream", (
        f"expected chunks to reconstruct 'mock openai stream' in order, got chunks={chunks!r}"
    )


@pytest.mark.asyncio
async def test_stream_usage_holder_populates_and_charges_budget(test_client, redis_client):
    """
    Confirms the usage_holder side-channel actually gets filled in by the real
    SSE parsing logic (not just that the endpoint returns 200), and that
    reconciliation reaches the *same* spend:<team>:daily INCRBYFLOAT path
    test_budget.py exercises via the non-streaming endpoint — proving the
    two code paths (streaming vs. non-streaming) don't silently diverge on
    whether spend actually gets recorded.
    """
    redis_client.delete(f"spend:{TEAM_ID}:daily")
    redis_client.delete(f"reserved:{TEAM_ID}:daily")

    async with test_client.stream(
        "POST",
        "/generate/stream",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_text():
            pass  # drain the stream fully — reconciliation fires as the generator closes

    spend = await _poll_spend(redis_client)
    assert spend > 0, (
        "expected spend:team-a:daily to be incremented by the streaming background task, "
        "but it never appeared — usage_holder likely wasn't populated by the SSE parsing"
    )
    assert spend == pytest.approx(MOCK_STREAM_COST), (
        f"expected spend to equal the mock's fixed cost ({MOCK_STREAM_COST}), got {spend} — "
        f"usage_holder's token counts may not match what the mock actually sent"
    )


@pytest.mark.asyncio
async def test_stream_rejected_with_402_once_budget_is_exceeded(test_client, redis_client):
    """
    Pre-flight reservation must reject a stream before any provider call is
    made once spend + this request's estimated cost would exceed budget —
    confirms the gap where a team could previously start (and fully receive)
    a stream despite being over budget, since /generate/stream only
    reconciled cost *after* the fact, is actually closed.
    """
    redis_client.set(f"spend:{TEAM_ID}:daily", 5.00)  # == TEAM_BUDGETS["team-a"]
    redis_client.delete(f"reserved:{TEAM_ID}:daily")

    async with test_client.stream(
        "POST",
        "/generate/stream",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    ) as response:
        assert response.status_code == 402, (
            f"expected 402 before any bytes stream when already at budget, got {response.status_code}"
        )


@pytest.mark.asyncio
async def test_stream_failure_releases_reservation_without_charging_spend(test_client, redis_client):
    """
    Forces stream_openai's mock to raise before yielding any chunks (a
    provider dying mid-stream). Confirms the reservation taken at the start
    of generate_stream is fully released — not left stuck consuming budget
    headroom — and that no spend is recorded for a stream that never
    produced usable output, since _tracked_stream's finally block is what
    has to do this (Starlette's `background=` would silently skip it, as it
    only runs after the generator returns normally).
    """
    redis_client.delete(f"spend:{TEAM_ID}:daily")
    redis_client.delete(f"reserved:{TEAM_ID}:daily")
    redis_client.set("mock:fail_count:openai", 1)

    async with test_client.stream(
        "POST",
        "/generate/stream",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    ) as response:
        # The failure happens inside the generator, after headers are already
        # sent (StreamingResponse starts the response before pulling the
        # first chunk) — so the client sees a 200 with an empty/broken body,
        # not an HTTP error status.
        with pytest.raises(httpx.HTTPError):
            async for _ in response.aiter_text():
                pass

    reserved = await _poll_reserved_release(redis_client)
    assert reserved == 0.0, (
        f"expected reserved:{TEAM_ID}:daily to be fully released back to 0 after a mid-stream "
        f"failure, got {reserved} — the reservation is stuck consuming budget headroom"
    )

    spend = redis_client.get(f"spend:{TEAM_ID}:daily")
    assert spend is None or float(spend) == 0.0, (
        f"expected no spend to be recorded for a stream that failed before producing usage, "
        f"got spend:{TEAM_ID}:daily={spend}"
    )


async def _poll_reserved_release(redis_client, timeout_seconds: float = 5.0) -> float:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        value = redis_client.get(f"reserved:{TEAM_ID}:daily")
        if value is not None and float(value) == 0.0:
            return 0.0
        await asyncio.sleep(0.1)
    value = redis_client.get(f"reserved:{TEAM_ID}:daily")
    return float(value) if value is not None else -1.0  # sentinel: key never appeared
