"""Purpose routing, conservative budget reservation and provider usage accounting."""
import asyncio
from contextlib import AsyncExitStack
import math

import httpx

from agentic.config import ModelRoute, Settings, model_ssl_context
from agentic.async_io import OffloadBusy, run_sync, submit_sync
from agentic.store import Store
from agentic.telemetry import span


def estimated_cost(route: ModelRoute, input_tokens: int, output_tokens: int) -> int | None:
    """Return micro-USD using operator-supplied per-million token prices."""
    if route.input_cost_per_million is None or route.output_cost_per_million is None:
        return None
    return math.ceil(input_tokens * route.input_cost_per_million + output_tokens * route.output_cost_per_million)


def reported_usage(provider: str, usage: object) -> tuple[int, int] | None:
    """Validate provider counters, including Anthropic cached input tokens.

    Incomplete or malformed usage cannot replace a conservative reservation.
    Boolean counters are invalid even though Python treats them as integers.
    """
    if not isinstance(usage, dict):
        return None
    names = ("prompt_tokens", "completion_tokens") if provider == "openai" else (
        "input_tokens", "output_tokens"
    )
    input_tokens, output_tokens = (usage.get(name) for name in names)
    cached = () if provider == "openai" else (
        usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
    )
    if not all(type(value) is int and 0 <= value <= 2_000_000_000
               for value in (input_tokens, output_tokens, *cached)):
        return None
    return input_tokens + sum(cached), output_tokens


class ModelGateway:
    def __init__(self, config: Settings, store: Store | None = None):
        self.config = config
        self.store = store or Store(config.db_path, lease_seconds=config.lease_seconds, max_attempts=config.max_attempts,
                                    step_timeout_seconds=config.step_timeout_seconds)

    @staticmethod
    def input_units(system: str, message: str) -> int:
        # Includes system/schema text and conservative chat-message framing.
        return len(system.encode("utf-8")) + len(message.encode("utf-8")) + 128

    def input_capacity(self, system: str, *, tenant: str, run_id: str | None) -> int:
        if self.config.provider == "offline":
            return 14000
        self.config.features.require("model_generation", self._snapshot(tenant, run_id))
        remaining = self.store.remaining_model_tokens(tenant, run_id, self.config.max_model_tokens)
        return max(0, min(14000, remaining - self.config.max_model_output_tokens - self.input_units(system, "")))

    def _snapshot(self, tenant: str, run_id: str | None) -> dict | None:
        run = self.store.get(tenant, run_id) if run_id is not None else None
        return run["specification"].get("feature_flags") if run is not None else None

    def routes(self, purpose: str, snapshot: dict | None = None) -> list[ModelRoute]:
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
        return result if self.config.features.enabled("model_fallback", snapshot) else result[:1]

    @staticmethod
    async def _request(client, route, system, message, output_limit, run_id, telemetry_allowed):
        with span("model.request", {"run.id": run_id or "unbound", "model.route": route.name,
                                    "model.provider": route.provider}, allowed=telemetry_allowed):
            if route.provider == "openai":
                path = "/chat/completions"
                headers = {"Authorization": "Bearer " + route.api_key} if route.auth_mode == "required" else {}
                payload = {"model": route.model, "messages": [
                    {"role": "system", "content": system}, {"role": "user", "content": message},
                ], route.output_token_parameter or "max_completion_tokens": output_limit}
                if route.reasoning_effort is not None:
                    payload["reasoning_effort"] = route.reasoning_effort
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
        clock = asyncio.get_running_loop().time
        deadline = clock() + self.config.model_timeout_seconds
        if len(system) + len(message) > 100000:
            raise ValueError("Model input exceeds size limit")
        # UTF-8 bytes plus message overhead intentionally over-reserves compared
        # with typical text tokenizers. Reported usage replaces it when available.
        estimated_input = self.input_units(system, message)
        output_limit = self.config.max_model_output_tokens
        # One elapsed budget covers all routes, connection, headers and body.
        # HTTPX's per-read inactivity timeout alone allows endless dribbling.
        async with asyncio.timeout_at(deadline), AsyncExitStack() as clients:
            snapshot = await run_sync(self._snapshot, tenant, run_id)
            self.config.features.require("model_generation", snapshot)
            routes = self.routes(purpose, snapshot)
            transports = {}
            for index, route in enumerate(routes):
                if route.auth_mode == "required" and not route.api_key:
                    raise ValueError("Configured model route requires an API key")
                # Local no-auth traffic must never pass through environment
                # proxies. Hosted routes retain their existing proxy behavior.
                trust_environment = route.auth_mode != "none" and route.ca_file is None
                transport_key = (trust_environment, route.ca_file)
                if transport_key not in transports:
                    options = {} if route.ca_file is None else {
                        "verify": await run_sync(model_ssl_context, route.ca_file),
                    }
                    transports[transport_key] = await clients.enter_async_context(httpx.AsyncClient(
                        timeout=None, follow_redirects=False, trust_env=trust_environment, **options,
                    ))
                client = transports[transport_key]
                if clock() >= deadline:
                    raise TimeoutError("Model call deadline elapsed")
                reservation = await run_sync(self.store.reserve_usage,
                    tenant, run_id, route.name, purpose, estimated_input + output_limit,
                    estimated_cost(route, estimated_input, output_limit),
                    default_limit=self.config.max_model_tokens,
                )
                settlement_started = False

                def begin_settlement(_reservation=reservation, **kwargs):
                    nonlocal settlement_started
                    if settlement_started:
                        return
                    # Cancellation may leave this operation running. Do not
                    # race it with a second, less precise settlement.
                    future = submit_sync(self.store.settle_usage, _reservation, **kwargs)
                    settlement_started = True
                    return future

                async def settle(_begin=begin_settlement, **kwargs):
                    future = _begin(**kwargs)
                    if future is not None:
                        await asyncio.shield(future)

                def record_stop(_begin=begin_settlement, **kwargs):
                    try:
                        _begin(**kwargs)
                    except OffloadBusy:
                        # Retain the conservative durable reservation if all
                        # finalization capacity is already occupied. An expired
                        # caller must not wait for a contended database lock.
                        pass

                try:
                    if clock() >= deadline:
                        raise TimeoutError("Model call deadline elapsed")
                    response = await self._request(client, route, system, message, output_limit, run_id,
                                                   self.config.features.enabled("telemetry", snapshot))
                    response.raise_for_status()
                    if len(response.content) > 1_000_000:
                        raise ValueError("Model response exceeds size limit")
                    data = response.json()
                    counts = reported_usage(route.provider, data.get("usage"))
                    if counts is not None:
                        await settle(
                            input_tokens=counts[0], output_tokens=counts[1],
                            cost_microusd=estimated_cost(route, *counts), status="reported",
                        )
                    else:
                        await settle(status="estimated_missing_usage")
                    if route.provider == "openai":
                        choice = data["choices"][0]
                        if choice.get("finish_reason") == "length":
                            raise ValueError("Model output was truncated by its token limit")
                        if "finish_reason" in choice and choice["finish_reason"] != "stop":
                            raise ValueError("Model did not finish a text response normally")
                        text = choice["message"]["content"]
                    else:
                        if data.get("stop_reason") == "max_tokens":
                            raise ValueError("Model output was truncated by its token limit")
                        if "stop_reason" in data and data["stop_reason"] not in {"end_turn", "stop_sequence"}:
                            raise ValueError("Model did not finish a text response normally")
                        text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Model returned no text")
                    if len(text) > 32000:
                        raise ValueError("Model text exceeds the response limit")
                    if counts is not None and counts[1] > output_limit:
                        raise ValueError("Model exceeded its configured output token limit")
                    if clock() >= deadline:
                        raise TimeoutError("Model call deadline elapsed")
                    return text
                except asyncio.CancelledError:
                    record_stop(status=(
                        "uncertain_timeout" if clock() >= deadline else "uncertain_cancelled"
                    ))
                    raise
                except TimeoutError:
                    record_stop(status="uncertain_timeout")
                    raise
                except httpx.HTTPStatusError as exc:
                    await settle(status="uncertain_http_error")
                    transient = exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
                    if not transient or index == len(routes) - 1:
                        raise
                except httpx.TransportError:
                    await settle(status="uncertain_transport_error")
                    if index == len(routes) - 1:
                        raise
                except Exception:
                    await settle(status="uncertain_response_error")
                    raise
        raise ValueError("No model route completed")
