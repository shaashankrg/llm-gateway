import asyncio
import time

from app.models import StandardRequest
from app.providers.openai_provider import call_openai, to_openai_request
from app.providers.anthropic_provider import call_anthropic, to_anthropic_request


class HealthTracker:
    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self.results = []
        self.latencies = []

    def record(self, success: bool, latency: float):
        self.results.append(success)
        self.latencies.append(latency)
        if len(self.results) > self.window_size:
            self.results.pop(0)
            self.latencies.pop(0)

    def error_rate(self) -> float:
        if not self.results:
            return 0.0
        failures = self.results.count(False)
        return failures / len(self.results)

    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        index = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[min(index, len(sorted_latencies) - 1)]


health_trackers = {
    "openai": HealthTracker(),
    "anthropic": HealthTracker(),
}


async def ping_openai() -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        health_req = StandardRequest(prompt="ping", model="gpt-4o-mini", max_tokens=10)
        await call_openai(to_openai_request(health_req))
        success = True
    except Exception:
        success = False
    elapsed = time.perf_counter() - start
    return success, elapsed


async def ping_anthropic() -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        health_req = StandardRequest(prompt="ping", model="claude-sonnet-5", max_tokens=10)
        await call_anthropic(to_anthropic_request(health_req))
        success = True
    except Exception:
        success = False
    elapsed = time.perf_counter() - start
    return success, elapsed


async def health_check_loop():
    while True:
        success, elapsed = await ping_openai()
        health_trackers["openai"].record(success, elapsed)
        print(f"Health check: openai={success}, {elapsed:.2f}s")

        success, elapsed = await ping_anthropic()
        health_trackers["anthropic"].record(success, elapsed)
        print(f"Health check: anthropic={success}, {elapsed:.2f}s")

        await asyncio.sleep(30)
