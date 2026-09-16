# 0004: Providers via OpenAI-compatible endpoints, routed by model name

- **Status:** accepted
- **Stage:** 1

## Context
The owner may switch Lithe from Gemini/Ollama models to Claude or GPT models. The gateway must handle a model change without a code change, and a model it can't serve must fail clearly instead of being sent to the wrong provider.

## Decision
- Four providers (Gemini, Ollama, OpenAI, Anthropic), all called through their OpenAI-compatible `chat/completions` endpoint with `Authorization: Bearer <key>`. One request/streaming code path serves all of them.
- Provider chosen per request from the model name: explicit `provider/model` pins it (prefix stripped before sending); else the longest case-insensitive prefix match in `MODEL_PREFIXES`; else `DEFAULT_PROVIDER` (Ollama, since local model names are arbitrary).
- A provider with no API key is "off": requests for its models get an OpenAI-style error naming the missing env var, and nothing is sent upstream.

## Alternatives
- Native APIs per provider (e.g. Anthropic `/v1/messages`) - full features, but each needs its own request/response/stream translation; too much for Stage 1. Revisit if the compat layer blocks something Lithe needs.
- A third-party router library (e.g. LiteLLM) - does this already, but hides exactly the parts this project exists to learn.
- Unknown model -> error instead of default provider - safer, but every new Ollama model would need a config entry.

## Why
Every provider already accepts the OpenAI format, so supporting a new one is a settings entry, not new code. Longest-prefix matching lets specific rules override general ones (`gpt-oss` is a local Ollama model, while `gpt-` goes to OpenAI). The explicit `provider/model` form is the escape hatch when names are ambiguous.

Trade-offs accepted: Anthropic states its OpenAI compatibility layer is not a long-term or production-ready solution; it ignores fields such as `response_format`, `seed`, penalties, and `logprobs`, and has no prompt caching or extended thinking ([docs](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)). Some OpenAI models (e.g. GPT-5.1-Codex) are Responses-API-only and will return the upstream error. Provider env var names are the standard ones, so a key already set in the Windows environment is picked up too.
