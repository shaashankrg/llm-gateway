import pytest

TEAM_ID = "team-a"
API_KEY = "team-a-key"
DAILY_BUDGET = 5.00  # must match TEAM_BUDGETS["team-a"] in app/budget.py

# MOCK_PROVIDERS makes call_openai return a fixed 1 input / 1 output token
# response, so a single gpt-4 request costs a fixed, known amount:
# calculate_cost("gpt-4", 1, 1) = 1*0.00003 + 1*0.00006 = 0.00009
MOCK_REQUEST_COST = 0.00009


@pytest.mark.asyncio
async def test_request_rejected_with_402_once_budget_is_exceeded(test_client, redis_client):
    """
    Seed spend:team-a:daily to just under the $5.00 daily budget — close enough
    that one more (mock, fixed-cost) request pushes it over — then fire that
    one request and confirm it's rejected with 402, not silently allowed
    through or rejected with the wrong status code.
    """
    seeded_spend = DAILY_BUDGET - MOCK_REQUEST_COST + 0.00001  # just enough to tip over 100%
    redis_client.set(f"spend:{TEAM_ID}:daily", seeded_spend)

    response = await test_client.post(
        "/generate",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    )

    assert response.status_code == 402, (
        f"expected 402 once spend crosses the daily budget, got {response.status_code}: "
        f"{response.text}"
    )


@pytest.mark.asyncio
async def test_request_succeeds_when_under_budget(test_client, redis_client):
    """
    Companion to the over-budget test above. Seeds spend at 79% of budget —
    comfortably under the 80% warning line and the 100% rejection line — and
    confirms the request succeeds normally.

    Without this, the over-budget test alone could pass even if the threshold
    comparison were backwards (e.g. always rejecting regardless of spend) —
    same reasoning as asserting an exact count in the rate-limit test rather
    than just "some rejection happened."
    """
    seeded_spend = DAILY_BUDGET * 0.79
    redis_client.set(f"spend:{TEAM_ID}:daily", seeded_spend)

    response = await test_client.post(
        "/generate",
        json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
        headers={"x-api-key": API_KEY},
    )

    assert response.status_code == 200, (
        f"expected 200 when comfortably under budget, got {response.status_code}: "
        f"{response.text}"
    )
