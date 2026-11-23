"""Purpose routing, conservative budget reservation and provider usage accounting."""
import asyncio
from contextlib import AsyncExitStack
import json
import math
import re

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agentic.config import ModelRoute, Settings, model_ssl_context
from agentic.async_io import OffloadBusy, run_sync, submit_sync
from agentic.store import Store
from agentic.telemetry import span

MAX_SCHEMA_BYTES = 16_384
MAX_SCHEMA_NODES = 2048
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_EXPANSION = 8192
MAX_REQUEST_BYTES = 1_000_000
SCHEMA_ERROR = "Invalid or oversized structured response schema"


def _schema_text_bytes(value: str) -> int:
    if len(value) > MAX_SCHEMA_BYTES:
        raise ValueError(SCHEMA_ERROR)
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError(SCHEMA_ERROR) from None


def response_format(response_schema: dict | None) -> dict | None:
    """Snapshot a bounded application-owned schema without resolving remote resources."""
    if response_schema is None:
        return None
    error = SCHEMA_ERROR
    if type(response_schema) is not dict or response_schema.get("type") != "object":
        raise ValueError(error)
    pending = [(response_schema, (), 0)]
    nodes, edges, references = {}, {}, []
    text_bytes = 0
    while pending:
        value, path, depth = pending.pop()
        if len(nodes) >= MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
            raise ValueError(error)
        nodes[path] = value
        children = []
        if type(value) is dict:
            if len(value) > MAX_SCHEMA_NODES or any(type(key) is not str for key in value):
                raise ValueError(error)
            if set(value) & {"$id", "$schema", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor"}:
                raise ValueError(error)
            for key, child in value.items():
                text_bytes += _schema_text_bytes(key)
                children.append(path + (key,))
                pending.append((child, children[-1], depth + 1))
            if "$ref" in value:
                reference = value["$ref"]
                if not isinstance(reference, str):
                    raise ValueError(error)
                _schema_text_bytes(reference)
                if not reference.startswith("#/") or "%" in reference or re.search(r"~(?![01])", reference):
                    raise ValueError(error)
                target = tuple(part.replace("~1", "/").replace("~0", "~") for part in reference[2:].split("/"))
                references.append((path, target))
        elif type(value) is list:
            if len(value) > MAX_SCHEMA_NODES:
                raise ValueError(error)
            for index, child in enumerate(value):
                children.append(path + (str(index),))
                pending.append((child, children[-1], depth + 1))
        elif type(value) is str:
            text_bytes += _schema_text_bytes(value)
        elif value is None or type(value) in (bool, int, float):
            try:
                text_bytes += len(json.dumps(value, allow_nan=False))
            except ValueError:
                raise ValueError(error) from None
        else:
            raise ValueError(error)
        if text_bytes > MAX_SCHEMA_BYTES:
            raise ValueError(error)
        edges[path] = children
    for source, target in references:
        if target not in nodes or type(nodes[target]) not in (dict, bool):
            raise ValueError(error)
        edges[source].append(target)
    visiting, sizes, depths = set(), {}, {}

    def bound_expansion(path, depth=0):
        if path in visiting or depth > MAX_SCHEMA_DEPTH:
            raise ValueError(error)
        if path in sizes:
            return sizes[path], depths[path]
        visiting.add(path)
        size, longest = 1, 0
        for child in edges[path]:
            child_size, child_depth = bound_expansion(child, depth + 1)
            size += child_size
            longest = max(longest, child_depth + 1)
            if size > MAX_SCHEMA_EXPANSION or longest > MAX_SCHEMA_DEPTH:
                raise ValueError(error)
        visiting.remove(path)
        sizes[path], depths[path] = size, longest
        return size, longest

    bound_expansion(())
    encoded = json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError(error)
    schema = json.loads(encoded)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError(error) from None
    return {"type": "json_schema", "json_schema": {"name": "agentic_response", "strict": True, "schema": schema}}


def format_units(format_spec: dict | None) -> int:
    if format_spec is None:
        return 0
    return len(json.dumps({"response_format": format_spec}, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON response fields")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON response value")
    return number


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON response value")


def validate_structured_text(text: str, format_spec: dict) -> None:
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object, parse_float=_finite_float,
                            parse_constant=_reject_constant)
        Draft202012Validator(format_spec["json_schema"]["schema"]).validate(parsed)
    except (ValueError, ValidationError, RecursionError):
        raise ValueError("Model returned invalid structured output") from None


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
    def input_units(system: str, message: str, *, response_schema: dict | None = None) -> int:
        # Includes system/schema text and conservative chat-message framing.
        return (len(system.encode("utf-8")) + len(message.encode("utf-8")) + 128
                + format_units(response_format(response_schema)))

    def input_capacity(self, system: str, *, tenant: str, run_id: str | None,
                       response_schema: dict | None = None) -> int:
        if self.config.provider == "offline":
            return 14000
        self.config.features.require("model_generation", self._snapshot(tenant, run_id))
        remaining = self.store.remaining_model_tokens(tenant, run_id, self.config.max_model_tokens)
        return max(0, min(14000, remaining - self.config.max_model_output_tokens
                          - self.input_units(system, "", response_schema=response_schema)))

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
    def _request_body(route, system, message, output_limit, format_spec):
        if route.provider == "openai":
            payload = {"model": route.model, "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": message},
            ], route.output_token_parameter or "max_completion_tokens": output_limit}
            if route.reasoning_effort is not None:
                payload["reasoning_effort"] = route.reasoning_effort
            if format_spec is not None:
                payload["response_format"] = format_spec
        else:
            payload = {"model": route.model, "system": system,
                       "messages": [{"role": "user", "content": message}], "max_tokens": output_limit}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("Model request exceeds size limit")
        return body

    @staticmethod
    async def _request(client, route, body, run_id, telemetry_allowed):
        with span("model.request", {"run.id": run_id or "unbound", "model.route": route.name,
                                    "model.provider": route.provider}, allowed=telemetry_allowed):
            if route.provider == "openai":
                path = "/chat/completions"
                headers = {"Authorization": "Bearer " + route.api_key} if route.auth_mode == "required" else {}
            else:
                path = "/messages"
                headers = {"x-api-key": route.api_key, "anthropic-version": "2023-06-01"}
            headers["Accept-Encoding"] = "identity"
            headers["Content-Type"] = "application/json"
            async with client.stream("POST", route.base_url + path, headers=headers, content=body) as response:
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
        response_schema: dict | None = None,
    ) -> str | None:
        if self.config.provider == "offline":
            if response_schema is not None:
                raise ValueError("Structured output requires a configured model route")
            return None
        clock = asyncio.get_running_loop().time
        deadline = clock() + self.config.model_timeout_seconds
        if len(system) + len(message) > 100000:
            raise ValueError("Model input exceeds size limit")
        # UTF-8 bytes plus message overhead intentionally over-reserves compared
        # with typical text tokenizers. Reported usage replaces it when available.
        format_spec = response_format(response_schema)
        estimated_input = self.input_units(system, message) + format_units(format_spec)
        output_limit = self.config.max_model_output_tokens
        # One elapsed budget covers all routes, connection, headers and body.
        # HTTPX's per-read inactivity timeout alone allows endless dribbling.
        async with asyncio.timeout_at(deadline), AsyncExitStack() as clients:
            snapshot = await run_sync(self._snapshot, tenant, run_id)
            self.config.features.require("model_generation", snapshot)
            routes = self.routes(purpose, snapshot)
            if format_spec is not None and any(not route.supports_json_schema for route in routes):
                raise ValueError("Every selected model route must explicitly support JSON schema output")
            transports = {}
            for index, route in enumerate(routes):
                if route.auth_mode == "required" and not route.api_key:
                    raise ValueError("Configured model route requires an API key")
                body = self._request_body(route, system, message, output_limit, format_spec)
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
                    response = await self._request(client, route, body, run_id,
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
                        message_data = choice["message"]
                        if message_data.get("refusal"):
                            raise ValueError("Model refused the requested response")
                        text = message_data["content"]
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
                    if format_spec is not None:
                        validate_structured_text(text, format_spec)
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
