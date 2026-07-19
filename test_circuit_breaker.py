import asyncio

import httpx


async def hit():
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "http://localhost:8000/generate",
            headers={"x-api-key": "team-a-key"},
            json={"prompt": "hi", "model": "gpt-4"},
        )
        return response.status_code, response.json()


async def main():
    for i in range(8):
        status, body = await hit()
        print(f"Request {i+1}: status={status}, body={body}")
        await asyncio.sleep(1)


asyncio.run(main())
