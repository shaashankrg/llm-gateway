# llm-gateway

A small LLM gateway built incrementally as a learning project: request routing to
OpenAI/Anthropic, per-team auth and rate limiting, budget tracking, and a
priority queue for request handling.

## Run

```bash
docker-compose up --build
```

Requires `GATEWAY_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` set in the environment (see `docker-compose.yml`).

## Endpoints

- `POST /generate` — non-streaming completion
- `POST /generate/stream` — streaming completion
- `GET /healthz` — health check
