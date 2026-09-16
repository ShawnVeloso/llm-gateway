# llm-gateway

A small local server that sits between your apps and AI providers. Apps send OpenAI-style chat requests to the gateway instead of directly to Gemini or Ollama; the gateway forwards them, returns the response, and logs what happened.

> Status: under construction. Stage 1 (pass-through + logging) is in progress. See `docs/ROADMAP.md`.

## Requirements
- Windows, macOS or Linux
- [uv](https://docs.astral.sh/uv/)

## Setup
```powershell
uv sync
```

## Results
_Real measurements go here later._
