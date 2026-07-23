from fastapi import Header, HTTPException

from app.tracing import tracer

# Stand-in for a real database — a hardcoded config for now
TEAM_CONFIG = {
    "team-a-key": {"team_id": "team-a", "allowed_models": ["gpt-4", "claude-sonnet-5", "gpt-4o-mini"]},
}

# Separate keys/team_ids for the load test (load_test.py) — each team_id gets
# its own independent rate-limit bucket (keyed on team_id in rate_limit.py).
# 55 teams * CAPACITY=10 (rate_limit.py) covers 500 concurrent requests with
# headroom, so the rate limiter isn't the bottleneck when the actual goal is
# measuring how throughput scales with worker-pool size (NUM_WORKERS).
LOAD_TEST_TEAM_COUNT = 55
TEAM_CONFIG.update(
    {
        f"load-test-key-{i}": {
            "team_id": f"load-test-{i}",
            "allowed_models": ["gpt-4", "claude-sonnet-5", "gpt-4o-mini"],
        }
        for i in range(1, LOAD_TEST_TEAM_COUNT + 1)
    }
)


def get_team(x_api_key: str = Header(...)) -> dict:
    with tracer.start_as_current_span("auth_check"):
        team = TEAM_CONFIG.get(x_api_key)
        if not team:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return team

def get_priority(x_priority: str = Header("realtime")):
    if x_priority not in ("realtime", "batch"):
        raise HTTPException(status_code=400, detail="x_priority must be 'realtime' or 'batch'")
    return x_priority