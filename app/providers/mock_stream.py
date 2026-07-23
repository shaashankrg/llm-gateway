class FakeStreamedResponse:
    """Mimics the subset of httpx's streamed Response used by stream_anthropic/
    stream_openai: an async context manager whose aiter_lines() yields canned
    SSE "data: ..." lines. Lets the real parsing loop (json.loads, event
    dispatch, usage_holder population) run unchanged against fake input,
    instead of the mock skipping that logic entirely.
    """

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def anthropic_sse_lines(text_chunks: list[str], input_tokens: int, output_tokens: int) -> list[str]:
    import json

    lines = [
        "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}}),
    ]
    for chunk in text_chunks:
        lines.append(
            "data: " + json.dumps({"type": "content_block_delta", "delta": {"text": chunk}})
        )
    lines.append(
        "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": output_tokens}})
    )
    return lines


def openai_sse_lines(text_chunks: list[str], input_tokens: int, output_tokens: int) -> list[str]:
    import json

    lines = []
    for chunk in text_chunks:
        lines.append(
            "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]})
        )
    # OpenAI's usage arrives as its own final chunk with an EMPTY choices list.
    lines.append(
        "data: "
        + json.dumps({"choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}})
    )
    lines.append("data: [DONE]")
    return lines
