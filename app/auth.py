from fastapi import Header, HTTPException

# Stand-in for a real database — a hardcoded config for now
TEAM_CONFIG = {
    "team-a-key": {"team_id": "team-a", "allowed_models": ["gpt-4", "claude-3-5-sonnet-20241022"]},
    "team-b-key": {"team_id": "team-b", "allowed_models": ["gpt-4"]},
}


def get_team(x_api_key: str = Header(...)) -> dict:
    team = TEAM_CONFIG.get(x_api_key)
    if not team:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return team

def get_priority(x_priority: str = Header("realtime")):
    if x_priority not in ("realtime", "batch"):
        raise HTTPException(status_code=400, detail="x_priority must be 'realtime' or 'batch'")
    return x_priority