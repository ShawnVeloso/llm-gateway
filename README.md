# llm-gateway

A small local server that sits between your apps and AI providers. Apps send OpenAI-style chat requests to the gateway instead of directly to Gemini or Ollama; the gateway forwards them, returns the response, and logs what happened.

> Status: Stage 1 (pass-through + logging). See `docs/ROADMAP.md`.

## Requirements
- Windows, macOS or Linux
- [uv](https://docs.astral.sh/uv/)
- For local models: [Ollama](https://ollama.com/) running (default `http://localhost:11434`)
- For Gemini: an API key from Google AI Studio

## Setup
```powershell
uv sync
Copy-Item .env.example .env
```
Then open `.env` and fill in the keys you have (e.g. `GEMINI_API_KEY`). A provider with no key is simply off: requests for its models get an error naming the missing setting. Ollama needs no key.

## Run
```powershell
uv run llm-gateway
```
It listens on `http://127.0.0.1:8787` (change the port with `PORT`). It only ever binds to `127.0.0.1`, because the gateway has no authentication and must not be reachable from other machines.

Check it is up:
```powershell
curl.exe http://127.0.0.1:8787/health
```

## Client setup
Point any OpenAI-compatible client at the gateway:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API key | anything (the gateway ignores it; it uses the keys in `.env`) |
| Header `X-App-Name` | a name for your app, e.g. `lithe` (used in the log; defaults to `unknown`) |

The model name picks the provider:
- `gemini-...` -> Gemini, `gpt-...` / `o1` / `o3` / `o4-...` -> OpenAI, `claude-...` -> Anthropic, `gpt-oss...` -> Ollama
- anything else -> `DEFAULT_PROVIDER` (Ollama by default)
- pin a provider explicitly with `provider/model`, e.g. `ollama/llama3.2`

Example (PowerShell):
```powershell
curl.exe http://127.0.0.1:8787/v1/chat/completions `
  -H "Content-Type: application/json" -H "X-App-Name: curl" `
  -d '{\"model\": \"gemini-3.6-flash\", \"messages\": [{\"role\": \"user\", \"content\": \"Hi\"}]}'
```
Add `\"stream\": true` to the body for streaming (Server-Sent Events).

Errors come back in the OpenAI shape: `{"error": {"message", "type", "code"}}`.

## Retries and fallback
If a provider fails in a way that might pass (can't connect, timeout, HTTP 429 or 5xx), the gateway tries again, waiting a little longer each time (`MAX_RETRIES`, default 2 retries). If Gemini still fails, the request goes to a local Ollama model instead (`FALLBACK_MODELS`, default `qwen2.5`; run `ollama pull qwen2.5` first). The response's `model` field shows which model actually answered.

Errors that won't fix themselves (a bad model name, a wrong or missing key) are returned straight away, with no retry or fallback. A stream can only retry or fall back before its first bytes reach your app; after that, a failure ends the stream with an error event.

## Request log
Every chat request writes one row to the `requests` table in a SQLite file at `data/gateway.db` (change with `DB_PATH`). A row has: time, request id, app, the model the app asked for, the provider and model that finally served it (or failed last), whether it streamed, input/output tokens (only when the provider reports them, otherwise empty), latency, time to first token for streams, HTTP status, number of upstream attempts, and a redacted error message. Each upstream call, including retries and fallback, is also a row in the `attempts` table, linked by `request_id`.

Prompt and response text are **not** stored unless you set `LOG_CONTENT=true`. API keys are never stored.

Look at recent rows with any SQLite tool, e.g.:
```powershell
sqlite3 data/gateway.db "SELECT created_at, app, provider, model, is_stream, input_tokens, output_tokens, latency_ms, status_code FROM requests ORDER BY id DESC LIMIT 10"
```

## Development
```powershell
uv run pytest -x -q --tb=short
uv run ruff check .
```

## Results
_Real measurements go here later._
