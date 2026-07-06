from pydantic import BaseModel

class StandardRequest(BaseModel):
    prompt: str
    model: str
    max_tokens: int = 512
    stream: bool = False

class StandardResponse(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int