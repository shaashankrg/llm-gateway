import json
import os

import httpx

from app.models import StandardRequest, StandardResponse

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def to_openai_request(std_req: StandardRequest) -> dict:
    return {
        "model": std_req.model,
        "messages": [{"role": "user", "content": std_req.prompt}],
        "max_tokens": std_req.max_tokens,
        "stream": std_req.stream,
    }


def from_openai_response(raw: dict) -> StandardResponse:
    return StandardResponse(
        text=raw["choices"][0]["message"]["content"],
        model=raw["model"],
        input_tokens=raw["usage"]["prompt_tokens"],
        output_tokens=raw["usage"]["completion_tokens"],
    )


async def call_openai(payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    async with httpx.AsyncClient() as client:
        response = await client.post(OPENAI_URL, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        return response.json()


async def stream_openai(payload: dict, usage_holder: dict):
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", OPENAI_URL, json=payload, headers=headers, timeout=60.0
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                chunk = line[len("data: "):]
                if chunk.strip() == "[DONE]":
                    break
                data = json.loads(chunk)
                # Only present when the request sets stream_options.include_usage,
                # and arrives as its own final chunk with an EMPTY choices list —
                # must check before indexing choices[0] or that chunk raises IndexError.
                if data.get("usage"):
                    usage_holder["usage"] = {
                        "input_tokens": data["usage"]["prompt_tokens"],
                        "output_tokens": data["usage"]["completion_tokens"],
                    }
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0]["delta"].get("content", "")
                if delta:
                    yield delta
