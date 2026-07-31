"""
Send steady traffic to the deployed gateway so the showcase site's live feed
has something to show.

    python demo_traffic.py                      # default: 60s against Render
    python demo_traffic.py --seconds 120
    python demo_traffic.py --url http://localhost:8000
    python demo_traffic.py --chaos              # trip an outage midway

Aimed at recording the demo: --chaos fires a 30s outage a few seconds in, so a
capture shows normal traffic, the breaker opening, failover rows, and recovery.
"""

import argparse
import asyncio
import random
import time

import httpx

GATEWAY = "https://llm-gateway-x18y.onrender.com"

# Keys from app/auth.py. team-a and team-b are the two the panel charts.
TEAMS = ["team-a-key", "team-b-key", "team-c-key"]
PROMPTS = [
    "Summarize circuit breakers in one line.",
    "What is a token bucket?",
    "Explain exponential backoff.",
    "Why use a priority queue here?",
]


async def one_request(client: httpx.AsyncClient, url: str) -> None:
    """Fire a single request; never raise, since a failed call is itself a
    valid thing for the feed to display."""
    try:
        await client.post(
            f"{url}/generate",
            headers={
                "X-API-Key": random.choice(TEAMS),
                "X-Priority": random.choice(["realtime", "batch"]),
                "Content-Type": "application/json",
            },
            json={
                "prompt": random.choice(PROMPTS),
                "model": random.choice(["gpt-4", "claude-sonnet-5"]),
                "max_tokens": 30,
            },
            timeout=30.0,
        )
    except Exception:
        pass


async def trigger_chaos(client: httpx.AsyncClient, url: str, seconds: int = 30) -> None:
    try:
        await client.post(
            f"{url}/demo/chaos",
            json={"provider": "openai", "duration_seconds": seconds},
            timeout=30.0,
        )
        print(f"  >> outage triggered ({seconds}s) — watch the panel")
    except Exception as e:
        print(f"  !! chaos failed: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=GATEWAY, help="gateway base URL")
    parser.add_argument("--seconds", type=int, default=60, help="how long to run")
    parser.add_argument("--rate", type=float, default=2.0, help="requests per second")
    parser.add_argument("--chaos", action="store_true", help="trip an outage after 8s")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    print(f"sending ~{args.rate}/s to {url} for {args.seconds}s")
    print("open your site's live panel to watch\n")

    async with httpx.AsyncClient() as client:
        # First request on a sleeping free-tier host can take ~50s to wake it.
        print("waking the gateway…", end=" ", flush=True)
        try:
            r = await client.get(f"{url}/healthz", timeout=90.0)
            print("awake" if r.status_code == 200 else f"got {r.status_code}")
        except Exception as e:
            print(f"failed: {e}")
            return

        started = time.monotonic()
        fired_chaos = False
        sent = 0

        while time.monotonic() - started < args.seconds:
            elapsed = time.monotonic() - started

            if args.chaos and not fired_chaos and elapsed >= 8:
                await trigger_chaos(client, url)
                fired_chaos = True

            # Small bursts read more naturally in the feed than a metronome.
            burst = [one_request(client, url) for _ in range(random.randint(1, 3))]
            await asyncio.gather(*burst)
            sent += len(burst)

            print(f"\r  sent {sent} requests… ({int(elapsed)}s)", end="", flush=True)
            await asyncio.sleep(1.0 / args.rate)

    print(f"\ndone — {sent} requests sent")


if __name__ == "__main__":
    asyncio.run(main())
