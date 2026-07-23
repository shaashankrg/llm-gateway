import redis.asyncio as redis

_client = redis.Redis(host="redis", port=6379, decode_responses=True)

# Only decrements if the key already exists — a test that never sets
# mock:fail_count:<provider> must leave it completely untouched, so unrelated
# tests' normal mock traffic can't silently consume or create a fail-count
# for a provider nobody asked to fail.
_DECR_IF_EXISTS = _client.register_script(
    """
    if redis.call("EXISTS", KEYS[1]) == 1 then
        return redis.call("DECR", KEYS[1])
    end
    return nil
    """
)


async def consume_forced_failure(provider: str) -> bool:
    """Returns True if this call should be mocked as a forced failure.

    Only tests that explicitly SET mock:fail_count:<provider> affect this —
    calls made while the key doesn't exist are always real (mocked-success)
    calls, and never create or touch the key.
    """
    remaining = await _DECR_IF_EXISTS(keys=[f"mock:fail_count:{provider}"])
    return remaining is not None and remaining >= 0
