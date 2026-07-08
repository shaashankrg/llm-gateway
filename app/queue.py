import asyncio

from app.providers.anthropic_provider import call_anthropic, from_anthropic_response, to_anthropic_request
from app.providers.openai_provider import call_openai, from_openai_response, to_openai_request

request_queue = asyncio.PriorityQueue()
_counter = 0

PRIORITY_VALUES = {
    "realtime": 1,
    "batch": 5,
}

async def enqueue_request(priority: str, request_data: dict):
    global _counter
    priority_value = PRIORITY_VALUES[priority]
    _counter += 1
    await request_queue.put((priority_value, _counter, request_data))

async def worker(worker_id: int):
    while True:
        priority_value, counter, request_data = await request_queue.get()
        req = request_data["req"]
        future = request_data["future"]

        try:
            if "claude" in req.model:
                raw = await call_anthropic(to_anthropic_request(req))
                result = from_anthropic_response(raw)
            else:
                raw = await call_openai(to_openai_request(req))
                result = from_openai_response(raw)
        except Exception as e:
            print(f"WORKER {worker_id} ERROR: {e}")
            future.set_exception(e)
        else:
            future.set_result(result)

async def start_workers(num_workers: int):
    await asyncio.gather(*[worker(i) for i in range(num_workers)])
