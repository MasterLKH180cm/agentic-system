"""Purpose routing, conservative budget reservation and provider usage accounting."""
import asyncio
import math

import httpx

from agentic.config import ModelRoute, Settings
from agentic.store import Store
from agentic.telemetry import span


def estimated_cost(route: ModelRoute, input_tokens: int, output_tokens: int) -> int | None:
    """Return micro-USD using operator-supplied per-million token prices."""
    if route.input_cost_per_million is None or route.output_cost_per_million is None:
        return None
    return math.ceil(input_tokens * route.input_cost_per_million + output_tokens * route.output_cost_per_million)


class ModelGateway:
    def __init__(self, config: Settings, store: Store | None = None):
        self.config = config
        self.store = store or Store(config.db_path, lease_seconds=config.lease_seconds, max_attempts=config.max_attempts,
                                    step_timeout_seconds=config.step_timeout_seconds)

    def routes(self, purpose: str) -> list[ModelRoute]:
        routes = list(self.config.routes)
        if not routes and self.config.provider in {"openai", "anthropic"}:
            is_openai = self.config.provider == "openai"
            routes = [ModelRoute(
                "default", self.config.provider,
                self.config.openai_model if is_openai else self.config.anthropic_model,
                self.config.openai_base_url if is_openai else "https://api.anthropic.com/v1",
                self.config.openai_key if is_openai else self.config.anthropic_key,
            )]
        result = [route for route in routes if purpose in route.purposes]
        if not result:
            raise ValueError("No model route configured for this purpose")
        return result

    @staticmethod
    async def _request(client, route, system, message, output_limit, run_id):
        with span("model.request", {"run.id": run_id or "unbound", "model.route": route.name,
                                    "model.provider": route.provider}):
            if route.provider == "openai":
                path = "/chat/completions"
                headers = {"Authorization": "Bearer " + route.api_key}
                payload = {"model": route.model, "messages": [
                    {"role": "system", "content": system}, {"role": "user", "content": message},
                ], "max_completion_tokens": output_limit}
            else:
                path = "/messages"
                headers = {"x-api-key": route.api_key, "anthropic-version": "2023-06-01"}
                payload = {"model": route.model, "system": system,
                           "messages": [{"role": "user", "content": message}], "max_tokens": output_limit}
            headers["Accept-Encoding"] = "identity"
            async with client.stream("POST", route.base_url + path, headers=headers, json=payload) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed model responses are not accepted")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 1_000_000:
                        raise ValueError("Model response exceeds size limit")
                    body.extend(chunk)
                return httpx.Response(response.status_code, content=bytes(body), request=response.request)

    async def complete(
        self, system: str, message: str, *, tenant: str = "local",
        run_id: str | None = None, purpose: str = "synthesis",
    ) -> str | None:
        if self.config.provider == "offline":
            return None
        if len(system) + len(message) > 100000:
            raise ValueError("Model input exceeds size limit")
        # UTF-8 bytes plus message overhead intentionally over-reserves compared
        # with typical text tokenizers. Reported usage replaces it when available.
        estimated_input = len(system.encode()) + len(message.encode()) + 128
        output_limit = self.config.max_model_output_tokens
        routes = self.routes(purpose)
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            for index, route in enumerate(routes):
                if not route.api_key:
                    raise ValueError("Configured model route requires an API key")
                reservation = self.store.reserve_usage(
                    tenant, run_id, route.name, purpose, estimated_input + output_limit,
                    estimated_cost(route, estimated_input, output_limit),
                    default_limit=self.config.max_model_tokens,
                )
                try:
                    response = await self._request(client, route, system, message, output_limit, run_id)
                    response.raise_for_status()
                    if len(response.content) > 1_000_000:
                        raise ValueError("Model response exceeds size limit")
                    data = response.json()
                    usage = data.get("usage", {})
                    names = ("prompt_tokens", "completion_tokens") if route.provider == "openai" else (
                        "input_tokens", "output_tokens"
                    )
                    counts = [usage.get(name) for name in names] if isinstance(usage, dict) else [None, None]
                    reported = all(type(value) is int and 0 <= value <= 2_000_000_000 for value in counts)
                    if reported and route.provider == "anthropic":
                        cache = [usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)]
                        reported = all(type(value) is int and 0 <= value <= 2_000_000_000 for value in cache)
                        if reported:
                            counts[0] += sum(cache)
                    if reported:
                        self.store.settle_usage(
                            reservation, input_tokens=counts[0], output_tokens=counts[1],
                            cost_microusd=estimated_cost(route, *counts), status="reported",
                        )
                    else:
                        self.store.settle_usage(reservation, status="estimated_missing_usage")
                    if route.provider == "openai":
                        text = data["choices"][0]["message"]["content"]
                    else:
                        text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Model returned no text")
                    return text[:32000]
                except asyncio.CancelledError:
                    self.store.settle_usage(reservation, status="uncertain_cancelled")
                    raise
                except httpx.HTTPStatusError as exc:
                    self.store.settle_usage(reservation, status="uncertain_http_error")
                    transient = exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
                    if not transient or index == len(routes) - 1:
                        raise
                except httpx.TransportError:
                    self.store.settle_usage(reservation, status="uncertain_transport_error")
                    if index == len(routes) - 1:
                        raise
                except Exception:
                    self.store.settle_usage(reservation, status="uncertain_response_error")
                    raise
        raise ValueError("No model route completed")
