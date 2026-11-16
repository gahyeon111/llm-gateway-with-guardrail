# AIM Guardrail Gateway

OpenAI-compatible LLM gateway that runs every request/response through the internal guardrail **AIM**
(PASS / MASK / BLOCK) before forwarding to Bedrock GPT-5.6 (via Xenon external models or directly).
Primary client: Codex CLI (`wire_api = "responses"`).

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Agent/contributor rules: [AGENTS.md](AGENTS.md)

## Status

| Milestone | State |
|---|---|
| M0 skeleton (`/healthz`, `/readyz`, `/v1/models`, Dockerfile) | done |
| M1 `/v1/responses` passthrough (stream / non-stream) | done — needs a real Codex + upstream run |
| M2 input guard + verdict cache + fake AIM | done |
| M3 output guard (text window, tool buffer, `response.failed`, completed rewrite) | done |
| M4 `/v1/chat/completions` | done |
| M5 Xenon deploy + service router + metrics | `/metrics` done; deploy pending |
| M6 `/v1/messages` | not started |
| M7 Bedrock IAM auth, real AIM spec | interface only (`app/upstream/auth.py`, `app/guard/aim_client.py`) |

## Local development

```bash
pip install -e ".[dev]"
cp .env.example .env          # points at the fakes below

# terminal 1 – fake AIM (BLOCKME → BLOCK, card number → MASK)
uvicorn tests.fake_aim:app --port 9001
# terminal 2 – fake upstream (FAKE_UPSTREAM_EVENT_DELAY_MS=200 to watch streaming)
uvicorn tests.fake_upstream:app --port 9002
# terminal 3 – gateway
uvicorn app.main:app --reload --port 8080

curl -N localhost:8080/v1/responses -H 'content-type: application/json' \
  -d '{"model":"gpt-5.6-terra","input":"hi","stream":true}'
```

Pass-through mode for M1 (no guardrails): `GUARD_INPUT_ENABLED=false GUARD_OUTPUT_ENABLED=false`.

## Tests

```bash
pytest -q
ruff check . && ruff format --check .
```

## Layout

```
app/
  main.py              app factory, lifespan, shared-secret + metrics middleware
  config.py            Settings (env vars, DESIGN 10장)
  errors.py            OpenAI-format errors (DESIGN 9장)
  sse.py               SSE parser / encoder
  routes/              responses, chat, models, ops
  guard/
    aim_client.py      ★ the only module that knows AIM's wire format (assumed spec)
    policy.py          cache, chunking, concurrency, retry, fail mode
    refs.py            text-field pointers + applying verdicts
    extract_*.py       what to check per API format
    text_buffer.py     format-independent hold-back window
    stream_*.py        SSE event adapters (Responses / Chat)
  upstream/            routing (MODEL_MAP), auth, HTTP client + error mapping
tests/                 fake_aim.py, fake_upstream.py, unit + integration tests
```
