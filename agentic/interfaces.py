"""Authenticated event streams, signed inbound hooks and a small local UI."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError

from agentic.schemas import WorkflowIn


def dashboard_html(config):
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    features = config.features
    goal_generation = config.provider == "offline" or features.enabled("model_generation")
    modes = {name: features.enabled(name) and goal_generation for name in ("dag", "agent_loop")}
    for mode, enabled in modes.items():
        if not enabled:
            html = html.replace(f'<option value="{mode}">', f'<option value="{mode}" disabled>')
    controls = {"submit": any(modes.values()), "mode": any(modes.values()),
                "stream": features.enabled("sse"), "stop": features.enabled("sse"),
                "approve": features.enabled("approvals"), "reject": features.enabled("approvals"),
                "step": features.enabled("approvals")}
    for name, enabled in controls.items():
        if not enabled:
            html = html.replace(f'id="{name}"', f'id="{name}" disabled data-feature-disabled="true"')
    if not features.enabled("api_docs"):
        html = html.replace('<p><a href="/docs">API reference</a></p>', '')
    return html


def install_interfaces(app, store, submit_callback, authenticator, config=None):
    config = config or authenticator.config
    features = config.features
    hooks = json.loads(os.getenv("AGENT_WEBHOOKS_JSON", "{}")) if features.enabled("webhooks") else {}
    if not isinstance(hooks, dict):
        raise ValueError("AGENT_WEBHOOKS_JSON must be an object")
    for name, hook_config in hooks.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", name) or not isinstance(hook_config, dict):
            raise ValueError("Invalid webhook identity")
        if hook_config.get("tenant") not in authenticator.config.rag_keys or len(hook_config.get("secret", "")) < 24:
            raise ValueError("Webhooks require a configured tenant and strong secret")
    if features.enabled("webhooks"):
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hook_receipts ("
                       "hook_id TEXT NOT NULL, digest TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, "
                       "PRIMARY KEY(hook_id,digest))")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        if not features.enabled("ui"):
            raise HTTPException(404, "Not found")
        html = dashboard_html(config)
        # Inline script is static and hash-authorized; data is rendered via textContent.
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        import base64
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        return HTMLResponse(html, headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'sha256-" + digest
            + "'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/workflows/{run_id}/events/stream")
    async def stream_events(
        run_id: str, request: Request,
        tenant: str = Depends(authenticator.tenant_dependency("workflow:read")),
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        last_event_id: str | None = Header(default=None),
    ):
        features.require("sse")
        if last_event_id is not None:
            if (not last_event_id.isascii() or not last_event_id.isdigit()
                    or len(last_event_id) > 19 or int(last_event_id) > 2**63 - 1):
                raise HTTPException(400, "Invalid event cursor")
            after = max(after, int(last_event_id))
        run = await asyncio.to_thread(store.get, tenant, run_id)
        if run is None:
            raise HTTPException(404, "Workflow not found")
        resource = {"tenant": tenant, "namespace": run["specification"]["memory_namespace"]}
        principal = authenticator.authenticate(request.headers.get("authorization"))
        authenticator.require(principal, "workflow:read", resource)

        async def events():
            cursor = after
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not await request.is_disconnected():
                try:
                    principal = authenticator.authenticate(request.headers.get("authorization"))
                    authenticator.require(principal, "workflow:read", resource)
                except HTTPException:
                    yield "event: auth_expired\ndata: {}\n\n"
                    return
                pending = await asyncio.to_thread(store.events, tenant, run_id, cursor, 201)
                for event in pending[:200]:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: workflow\ndata: {json.dumps(event)}\n\n"
                state = await asyncio.to_thread(store.get, tenant, run_id)
                if state is None:
                    return
                if len(pending) <= 200 and state["status"] in {"completed", "failed", "cancelled"}:
                    yield "event: done\ndata: " + json.dumps({"status": state["status"]}) + "\n\n"
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    @app.post("/hooks/workflows", status_code=202)
    async def receive_hook(request: Request):
        features.require("webhooks")
        identity = request.headers.get("x-agent-hook", "")
        timestamp = request.headers.get("x-agent-timestamp", "")
        signature = request.headers.get("x-agent-signature", "")
        hook = hooks.get(identity)
        if hook is None or re.fullmatch(r"[0-9]{1,12}", timestamp) is None:
            raise HTTPException(401, "Invalid webhook authentication")
        if abs(time.time() - int(timestamp)) > 300:
            raise HTTPException(401, "Expired webhook timestamp")
        if re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None:
            raise HTTPException(401, "Invalid webhook signature")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128_000:
                raise HTTPException(413, "Webhook body exceeds limit")
        digest = hmac.new(hook["secret"].encode(), timestamp.encode() + b"." + body,
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, "sha256=" + digest):
            raise HTTPException(401, "Invalid webhook signature")
        if not authenticator.limiter.take("hook:" + identity):
            raise HTTPException(429, "Webhook rate exceeded")
        with store.connect() as db:
            db.execute("DELETE FROM hook_receipts WHERE expires_at<?", (time.time(),))
            if db.execute("SELECT digest FROM hook_receipts WHERE hook_id=? AND digest=?",
                          (identity, digest)).fetchone():
                raise HTTPException(409, "Webhook already received")
        try:
            payload = WorkflowIn.model_validate_json(bytes(body))
        except ValidationError as exc:
            raise HTTPException(422, "Invalid workflow payload") from exc
        # Keep a caller's logical idempotency key stable across fresh signatures.
        # Without one, an exact signed delivery still survives a crash before receipt storage.
        logical_key = payload.idempotency_key or digest
        delivery = hashlib.sha256((identity + ":" + logical_key).encode()).hexdigest()
        payload = payload.model_copy(update={"idempotency_key": "hook:" + delivery})
        result = await submit_callback(payload, hook["tenant"])
        with store.connect() as db:
            accepted = db.execute("INSERT INTO hook_receipts(hook_id,digest,expires_at) VALUES(?,?,?) "
                                  "ON CONFLICT(hook_id,digest) DO NOTHING",
                                  (identity, digest, time.time() + 600)).rowcount
        if not accepted:
            raise HTTPException(409, "Webhook already received")
        return result
