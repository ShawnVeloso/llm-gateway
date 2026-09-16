"""Decide which provider serves a request, from the model name the client sent."""

from dataclasses import dataclass

from llm_gateway.config import PROVIDERS, Provider, Settings


@dataclass(frozen=True)
class Route:
    provider: Provider
    model: str  # name sent upstream (an explicit "provider/" prefix is removed)


class ProviderNotConfiguredError(Exception):
    """The model routes to a provider whose API key is not set."""

    def __init__(self, model: str, provider: Provider, key_env_var: str) -> None:
        super().__init__(
            f"Model '{model}' is served by provider '{provider}', "
            f"but {key_env_var} is not set in the gateway's .env."
        )
        self.provider = provider


def resolve_route(model: str, settings: Settings) -> Route:
    """Map a client model name to a provider.

    "anthropic/claude-x" pins the provider explicitly. Otherwise the longest matching prefix
    from settings wins, falling back to the default provider. Ollama names may contain "/"
    (e.g. "hf.co/user/model"), so only a known provider name counts as an explicit prefix.
    """
    head, sep, rest = model.partition("/")
    if sep and rest and head.lower() in PROVIDERS:
        route = Route(head.lower(), rest)  # type: ignore[arg-type]
    else:
        route = Route(_match_prefix(model, settings), model)

    endpoint = settings.endpoint(route.provider)
    if endpoint.needs_key and endpoint.api_key is None:
        raise ProviderNotConfiguredError(model, route.provider, endpoint.key_env_var or "")
    return route


def _match_prefix(model: str, settings: Settings) -> Provider:
    name = model.lower()
    matches = [p for p in settings.model_prefixes if name.startswith(p.lower())]
    if not matches:
        return settings.default_provider
    return settings.model_prefixes[max(matches, key=len)]
