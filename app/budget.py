from pathlib import Path

import redis.asyncio as redis
from fastapi import HTTPException

r = redis.Redis(host="redis", port=6379, decode_responses=True)

# Same pattern as rate_limit.py: load Lua relative to this file, not CWD.
_RESERVE_SCRIPT = r.register_script((Path(__file__).parent / "budget_reserve.lua").read_text())
_RECONCILE_SCRIPT = r.register_script((Path(__file__).parent / "budget_reconcile.lua").read_text())

# --- Piece 1: Pricing table ---
MODEL_PRICING = {
    "gpt-4": {
        "input_price": 0.00003,
        "output_price": 0.00006,
    },
    "claude-sonnet-5": {
        "input_price": 0.00002,
        "output_price": 0.00005,
    },
}

TEAM_BUDGETS = {
    "team-a": 5.00,  # $5/day
    "team-b": 1.00,  # $1/day
}


# --- Piece 2: Cost calculation ---
def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in MODEL_PRICING:
        return 0.0
    pricing = MODEL_PRICING[model]
    return (input_tokens * pricing["input_price"]) + (output_tokens * pricing["output_price"])


# --- Piece 3: Record spend atomically, and check budget ---
async def record_spend_and_check_budget(team_id: str, cost: float, daily_budget: float) -> float:
    key = f"spend:{team_id}:daily"

    # Atomic increment — same reasoning as the rate limiter's Lua script,
    # but here INCRBYFLOAT alone is enough since it's a single operation,
    # not a multi-step calculation like refill math was.
    new_total = await r.incrbyfloat(key, cost)
    await r.expire(key, 86400)  # resets roughly daily

    percent_used = new_total / daily_budget

    if percent_used >= 1.0:
        raise HTTPException(
            status_code=402,  # "Payment Required" — a reasonable fit for budget exceeded
            detail=f"Budget exceeded: ${new_total:.4f} / ${daily_budget:.2f}",
        )
    elif percent_used >= 0.8:
        print(f"WARNING: team {team_id} at {percent_used*100:.1f}% of daily budget")
        # In a real system: send this to Slack/logging instead of print()

    return new_total


# --- Piece 4: Pre-flight reservation for streaming requests ---
# Streaming can't know the real cost until the stream ends, but a team
# already at/near budget shouldn't be allowed to start one. Reserve an
# estimated max cost up front (checked atomically against spend + any
# other in-flight reservations for the team), then reconcile against the
# real usage once the stream closes (or fails/is cancelled).
async def reserve_budget(team_id: str, estimated_cost: float, daily_budget: float) -> bool:
    allowed = await _RESERVE_SCRIPT(
        keys=[f"spend:{team_id}:daily", f"reserved:{team_id}:daily"],
        args=[estimated_cost, daily_budget],
    )
    return bool(allowed)


async def reconcile_budget(team_id: str, reserved_amount: float, actual_cost: float) -> float:
    new_total = await _RECONCILE_SCRIPT(
        keys=[f"spend:{team_id}:daily", f"reserved:{team_id}:daily"],
        args=[reserved_amount, actual_cost],
    )
    return float(new_total)
