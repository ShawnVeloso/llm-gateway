"""The single HTTP path to every provider's OpenAI-compatible chat-completions endpoint."""

from typing import Any

import httpx

from llm_gateway.config import ProviderEndpoint


def chat_completions_url(endpoint: ProviderEndpoint) -> str:
    # rstrip so ".../v1" and ".../v1/" both work; urljoin would silently drop "v1" without a slash.
    return endpoint.base_url.rstrip("/") + "/chat/completions"


def auth_headers(endpoint: ProviderEndpoint) -> dict[str, str]:
    if endpoint.api_key is None:
        return {}
    return {"Authorization": f"Bearer {endpoint.api_key.get_secret_value()}"}


async def post_chat_completions(
    http: httpx.AsyncClient, endpoint: ProviderEndpoint, payload: dict[str, Any]
) -> httpx.Response:
    """Send a non-streaming request. Transport failures raise httpx errors; HTTP errors return."""
    return await http.post(
        chat_completions_url(endpoint), json=payload, headers=auth_headers(endpoint)
    )
