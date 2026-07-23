-- budget_reconcile.lua
-- Atomically releases a prior reservation and records the actual cost
-- against real spend. Used both on clean stream completion (actual_cost
-- = real usage) and on mid-stream failure/cancellation (actual_cost =
-- 0 or partial usage, whatever was actually incurred before it broke).
local spend_key = KEYS[1]
local reserved_key = KEYS[2]
local reserved_amount = tonumber(ARGV[1])
local actual_cost = tonumber(ARGV[2])

local reserved = tonumber(redis.call("GET", reserved_key)) or 0
-- Floor at 0 defensively: reserved_amount should never exceed what's
-- outstanding, but never let a reconcile push the counter negative.
local new_reserved = reserved - reserved_amount
if new_reserved < 0 then
    new_reserved = 0
end
redis.call("SET", reserved_key, new_reserved)
redis.call("EXPIRE", reserved_key, 86400)

local new_spend = redis.call("INCRBYFLOAT", spend_key, actual_cost)
redis.call("EXPIRE", spend_key, 86400)

return new_spend
