import asyncio

import pytest

API_KEY = "team-a-key"
TEAM_ID = "team-a"

# calculate_cost("gpt-4", 1, 1) with the mock's fixed 1 input / 1 output tokens
MOCK_STREAM_COST = 1 * 0.00003 + 1 * 0.00006


async def _poll_spend(redis_client, timeout_seconds: float = 5.0) -> float:
    # _finalize_stream_cost runs as a Starlette BackgroundTask, which fires
    # after the full streamed body has been sent to the client — there's a
    # small window between "client finished reading the stream" and "server
    # finished running the background task," so poll rather than reading once.
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
    SSE parsing logic (not just that the endpoint returns 200), and that the
    background task reaches the *same* record_spend_and_check_budget/INCRBYFLOAT
    path test_budget.py exercises via the non-streaming endpoint — proving the
    two code paths (streaming vs. non-streaming) don't silently diverge on
    whether spend actually gets recorded.
    """
    redis_client.delete(f"spend:{TEAM_ID}:daily")

    async with test_client.stream(
        "POST",
        "/generate/stream",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_text():
            pass  # drain the stream fully — the background task fires after this completes

    spend = await _poll_spend(redis_client)
    assert spend > 0, (
        "expected spend:team-a:daily to be incremented by the streaming background task, "
        "but it never appeared — usage_holder likely wasn't populated by the SSE parsing"
    )
    assert spend == pytest.approx(MOCK_STREAM_COST), (
        f"expected spend to equal the mock's fixed cost ({MOCK_STREAM_COST}), got {spend} — "
        f"usage_holder's token counts may not match what the mock actually sent"
    )
