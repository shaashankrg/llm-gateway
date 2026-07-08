import asyncio
import time

import httpx


async def hit(priority: str):
    async with httpx.AsyncClient(timeout=30) as client:
        start = time.perf_counter()
        response = await client.post(
            "http://localhost:8000/generate",
            headers={"x-api-key": "team-a-key", "X-Priority": priority},
            json={"prompt": "hi", "model": "claude-sonnet-5"},
        )
        elapsed = time.perf_counter() - start
        return {"priority": priority, "elapsed": round(elapsed, 2), "status": response.status_code}


async def main():
    tasks = [hit("batch") for _ in range(6)] + [hit("realtime")]
    results = await asyncio.gather(*tasks)

    results_sorted = sorted(results, key=lambda r: r["elapsed"])
    for r in results_sorted:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
