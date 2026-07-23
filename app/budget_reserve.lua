-- budget_reserve.lua
-- Atomically checks whether (actual spend + already-reserved + this
-- estimate) would exceed the daily budget, and if not, reserves the
-- estimate. Mirrors rate_limiter.lua's check-then-act pattern so a
-- concurrent burst of streaming requests from the same team can't all
-- pass the check against the same stale spend/reserved totals.
local spend_key = KEYS[1]
local reserved_key = KEYS[2]
local estimated_cost = tonumber(ARGV[1])
local daily_budget = tonumber(ARGV[2])

local spend = tonumber(redis.call("GET", spend_key)) or 0
local reserved = tonumber(redis.call("GET", reserved_key)) or 0

if spend + reserved + estimated_cost > daily_budget then
    return 0
end

local new_reserved = redis.call("INCRBYFLOAT", reserved_key, estimated_cost)
redis.call("EXPIRE", reserved_key, 86400)

return 1
