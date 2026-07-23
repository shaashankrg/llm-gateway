import json
import os

import httpx

from app.models import StandardRequest, StandardResponse
from app.providers.mock_control import consume_forced_failure
from app.providers.mock_stream import FakeStreamedResponse, anthropic_sse_lines

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def to_anthropic_request(std_req: StandardRequest) -> dict:
    return {
        "model": std_req.model,
        "messages": [{"role": "user", "content": std_req.prompt}],
        "max_tokens": std_req.max_tokens,  # required, unlike OpenAI
        "stream": std_req.stream,
    }


def from_anthropic_response(raw: dict) -> StandardResponse:
    # claude-sonnet-5 uses adaptive thinking by default, which puts a "thinking"
    # block before the "text" block — content[0] isn't reliably the text block.
    text_block = next((block for block in raw["content"] if block["type"] == "text"), None)
    if text_block is None:
        raise ValueError(f"No text block in Anthropic response (stop_reason={raw.get('stop_reason')})")

    return StandardResponse(
        text=text_block["text"],
        model=raw["model"],
        input_tokens=raw["usage"]["input_tokens"],
        output_tokens=raw["usage"]["output_tokens"],
    )


async def call_anthropic(payload: dict) -> dict:
    if os.environ.get("MOCK_PROVIDERS") == "1":
        # Tests can pre-set mock:fail_count:anthropic in Redis to force this
        # many consecutive calls to fail with a retryable 503, before mock
        # responses resume as normal — used to force retry-exhaustion /
        # same-request fallback without touching real provider credentials.
        # consume_forced_failure only touches the counter if a test actually
        # set it, so unrelated tests' normal mock traffic can't drift it.
        if await consume_forced_failure("anthropic"):
            request = httpx.Request("POST", ANTHROPIC_URL)
            response = httpx.Response(503, request=request, text="mock forced failure")
            raise httpx.HTTPStatusError("mock forced failure", request=request, response=response)

        return {
            "model": payload["model"],
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "mock anthropic response"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(ANTHROPIC_URL, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        return response.json()


async def _consume_anthropic_stream(response, usage_holder: dict):
    # Anthropic splits usage across two events instead of one final chunk like
    # OpenAI: input_tokens arrives up front in message_start, output_tokens
    # only shows up later in message_delta once generation is complete.
    input_tokens = 0
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data = json.loads(line[len("data: "):])
        event_type = data.get("type")
        if event_type == "message_start":
            input_tokens = data["message"]["usage"]["input_tokens"]
        elif event_type == "message_delta":
            usage_holder["usage"] = {
                "input_tokens": input_tokens,
                "output_tokens": data["usage"]["output_tokens"],
            }
        elif event_type == "content_block_delta":
            yield data["delta"].get("text", "")


async def stream_anthropic(payload: dict, usage_holder: dict):
    if os.environ.get("MOCK_PROVIDERS") == "1":
        # Same forced-failure mechanism as call_anthropic, mirrored here so
        # tests can simulate a provider dying mid-stream (after the response
        # starts but before usage_holder gets populated).
        if await consume_forced_failure("anthropic"):
            request = httpx.Request("POST", ANTHROPIC_URL)
            response = httpx.Response(503, request=request, text="mock forced failure")
            raise httpx.HTTPStatusError("mock forced failure", request=request, response=response)

        lines = anthropic_sse_lines(
            ["mock ", "anthropic ", "stream"], input_tokens=1, output_tokens=1
        )
        async with FakeStreamedResponse(lines) as response:
            response.raise_for_status()
            async for chunk in _consume_anthropic_stream(response, usage_holder):
                yield chunk
        return

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
    }
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", ANTHROPIC_URL, json=payload, headers=headers, timeout=60.0
        ) as response:
            response.raise_for_status()
            async for chunk in _consume_anthropic_stream(response, usage_holder):
                yield chunk
