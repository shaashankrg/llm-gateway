import asyncio

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
        print(f"Worker {worker_id} processing priority {priority_value}: {request_data}")

async def start_workers(num_workers: int):
    await asyncio.gather(*[worker(i) for i in range(num_workers)])