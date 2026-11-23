"""Tenant-scoped workflows, immutable definitions, memory and execution inspection."""
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from agentic.auth import Authenticator
from agentic.async_io import OffloadBusy, run_sync
from agentic.config import Settings
from agentic.control import ControlPlane
from agentic.features import FeatureDisabled
from agentic.interfaces import install_interfaces
from agentic.limits import BodyLimitMiddleware
from agentic.memory import Memory
from agentic.model import ModelGateway
from agentic.schemas import ApprovalIn, MemoryIn, NAME_PATTERN, REGISTRY_MODELS, WorkflowIn
from agentic.store import QueueFull, Store
from agentic.telemetry import install_telemetry
from agentic.tools import Tools


MemoryName = Annotated[str, Path(pattern=NAME_PATTERN)]


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    docs = config.features.enabled("api_docs")
    app = FastAPI(title="Agentic Workflow System", version="0.5.0",
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)

    @app.exception_handler(FeatureDisabled)
    async def disabled_feature(request: Request, exc: FeatureDisabled):
        return JSONResponse(status_code=403, content={"detail": exc.detail()})

    @app.exception_handler(OffloadBusy)
    async def saturated_persistence(request: Request, exc: OffloadBusy):
        return JSONResponse(status_code=503, content={"detail": "Persistence capacity is occupied"},
                            headers={"Retry-After": "1"})

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        def safe_text(value: str) -> str:
            return value.encode("utf-8", "backslashreplace").decode("utf-8")

        # Rejected input and exception contexts may be private or not JSON-safe.
        details = [{"type": safe_text(error["type"]),
                    "loc": [safe_text(part) if isinstance(part, str) else part for part in error["loc"]],
                    "msg": safe_text(error["msg"])} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": details})

    store = Store(config.db_path, lease_seconds=config.lease_seconds, max_attempts=config.max_attempts,
                  step_timeout_seconds=config.step_timeout_seconds)
    auth = Authenticator(config)
    model = ModelGateway(config, store)
    tools = Tools(config, model, store)
    control = ControlPlane(store, config)
    memory = Memory(store, config.features)
    app.state.store = store
    app.state.authenticator = auth
    app.state.control = control
    app.state.model = model
    app.state.tools = tools
    install_telemetry(app, allowed=config.features.enabled("telemetry"))

    async def submit_workflow(payload: WorkflowIn, tenant: str, *, principal=None) -> dict:
        try:
            control.validate_submission_features(payload)
            duplicate = await run_sync(store.duplicate, tenant, payload)
            if duplicate:
                if principal is not None:
                    original = await run_sync(store.get, tenant, duplicate["id"])
                    auth.require(principal, "workflow:write", {
                        "tenant": tenant, "namespace": original["specification"].get("memory_namespace", "default"),
                    })
                return duplicate
            compiled, specification = await run_sync(control.resolve, tenant, payload)
            if principal is not None:
                specification["requester_subject"] = principal.subject
                auth.require(principal, "workflow:write", {"tenant": tenant, "namespace": specification["memory_namespace"]})
                restricted_namespaces = principal.attributes.get("memory_namespaces")
                if restricted_namespaces is not None:
                    specification["allowed_namespaces"] = sorted(
                        set(specification["allowed_namespaces"]) & set(restricted_namespaces)
                    )
                if "memory:read" not in principal.scopes:
                    specification["allowed_tools"] = [t for t in specification["allowed_tools"] if t != "memory.search"]
                if payload.steps or payload.workflow_id:
                    control.validate_steps(compiled.steps, specification, features=config.features,
                                           online=config.provider != "offline")
            return await run_sync(
                store.submit, tenant, compiled, specification=specification,
                request_hash=store.fingerprint(payload), max_queued_runs=config.max_queued_runs,
            )
        except QueueFull as exc:
            raise HTTPException(429, "Tenant workflow queue is full") from exc
        except FeatureDisabled:
            raise
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            code = 409 if "Idempotency" in str(exc) else 422
            raise HTTPException(code, str(exc)) from exc

    app.state.submit_workflow = submit_workflow

    def authorized_run(request: Request, tenant: str, run_id: str, action: str = "workflow:read", missing: int = 404):
        value = store.get(tenant, run_id)
        if value is None:
            raise HTTPException(missing, "Workflow not found")
        auth.require(auth.authenticate(request.headers.get("authorization")), action, {
            "tenant": tenant, "namespace": value["specification"].get("memory_namespace", "default"),
        })
        return value

    @app.get("/live")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health")
    def health(tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> dict:
        return {"status": "ok", "tenant": tenant}

    @app.get("/capabilities")
    def capabilities(tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> dict:
        return {"features": config.features.effective(),
                "modes": {name: config.features.enabled(name) for name in ("dag", "agent_loop")},
                "tools": tools.describe(),
                "storage": {"backend": "postgresql" if config.db_path.startswith(("postgresql://", "postgres://"))
                            else "sqlite", "memory_search": "lexical"},
                "model": {"provider": config.provider,
                          "generation_enabled": config.provider != "offline" and config.features.enabled("model_generation"),
                          "fallback_enabled": config.features.enabled("model_fallback")}}

    @app.get("/tools")
    def available_tools(tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> dict:
        return {"tenant": tenant, "allowlisted": [t["name"] for t in tools.describe() if t["enabled"]],
                "approval_required": ["artifact.publish"], "definitions": tools.describe(),
                "sandbox": {"available": False, "reason": "No arbitrary-code execution adapter configured"}}

    @app.post("/workflows", status_code=status.HTTP_202_ACCEPTED)
    async def submit(
        payload: WorkflowIn, request: Request,
        tenant: str = Depends(auth.tenant_dependency("workflow:write")),
    ) -> dict:
        principal = await run_sync(auth.authenticate, request.headers.get("authorization"))
        return await submit_workflow(payload, tenant, principal=principal)

    @app.get("/workflows")
    def workflows(request: Request, tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> list[dict]:
        result = []
        for item in store.list(tenant):
            try:
                authorized_run(request, tenant, item["id"])
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                continue
            result.append(item)
        return result

    @app.get("/workflows/{run_id}")
    def workflow(run_id: str, request: Request, tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> dict:
        value = authorized_run(request, tenant, run_id)
        value["usage"] = store.usage(tenant, run_id)
        return value

    @app.get("/workflows/{run_id}/events")
    def events(
        run_id: str, request: Request, tenant: str = Depends(auth.tenant_dependency("workflow:read")),
        after: int = Query(default=0, ge=0, le=2**63 - 1),
    ) -> list[dict]:
        authorized_run(request, tenant, run_id)
        return store.events(tenant, run_id, after)

    @app.post("/workflows/{run_id}/approve/{step_id}")
    def approve(
        run_id: str, step_id: str, payload: ApprovalIn, request: Request,
        tenant: str = Depends(auth.tenant_dependency("workflow:approve")),
    ) -> dict:
        run = authorized_run(request, tenant, run_id, "workflow:approve", missing=409)
        specification = run["specification"]
        snapshot = specification.get("feature_flags")
        config.features.require("approvals", snapshot)
        config.features.require(specification.get("runtime_mode", "dag"), snapshot)
        step = next((item for item in run["steps"] if item["id"] == step_id), None)
        if step is not None:
            config.features.require_tool(step["tool"], snapshot=snapshot, online=config.provider != "offline",
                                         approval_required=bool(step["approval_required"]))
        approver = auth.authenticate(request.headers.get("authorization"))
        if run["specification"].get("requester_subject") == approver.subject:
            raise HTTPException(403, "The workflow requester cannot approve their own workflow")
        if not store.approve(tenant, run_id, step_id, payload.approved, payload.reason):
            raise HTTPException(409, "Step is not awaiting approval")
        return {"run_id": run_id, "step_id": step_id, "approved": payload.approved}

    @app.post("/workflows/{run_id}/cancel")
    def cancel(run_id: str, request: Request, tenant: str = Depends(auth.tenant_dependency("workflow:write"))) -> dict:
        authorized_run(request, tenant, run_id, "workflow:write", missing=409)
        if not store.cancel(tenant, run_id):
            raise HTTPException(409, "Workflow cannot be cancelled")
        return {"run_id": run_id, "cancelled": True}

    @app.post("/registry/{kind}", status_code=201)
    def register(kind: str, payload: dict, tenant: str = Depends(auth.tenant_dependency("registry:write"))) -> dict:
        try:
            return control.register(tenant, kind, payload)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid definition") from exc
        except FeatureDisabled:
            raise
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/registry/{kind}")
    def definitions(kind: str, tenant: str = Depends(auth.tenant_dependency("registry:read"))) -> list[dict]:
        config.features.require("registry")
        if kind not in REGISTRY_MODELS:
            raise HTTPException(404, "Unknown definition kind")
        return store.definitions(tenant, kind)

    @app.get("/registry/{kind}/{name}/{version}")
    def definition(
        kind: str, name: str, version: int,
        tenant: str = Depends(auth.tenant_dependency("registry:read")),
    ) -> dict:
        config.features.require("registry")
        item = store.definition(tenant, kind, name, version)
        if item is None:
            raise HTTPException(404, "Definition not found")
        return item

    def namespace_allowed(tenant: str, namespace: str) -> None:
        config.features.require("memory")
        if namespace not in control.server_policy(tenant).allowed_namespaces:
            raise HTTPException(403, "Memory namespace denied by server policy")

    @app.put("/memory/{namespace}/{record_id}")
    def put_memory(
        namespace: MemoryName, record_id: MemoryName, payload: MemoryIn,
        tenant: str = Depends(auth.tenant_dependency("memory:write")),
    ) -> dict:
        namespace_allowed(tenant, namespace)
        try:
            return memory.put(tenant, namespace, record_id, payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/memory/{namespace}/{record_id}")
    def get_memory(
        namespace: MemoryName, record_id: MemoryName,
        tenant: str = Depends(auth.tenant_dependency("memory:read")),
    ) -> dict:
        namespace_allowed(tenant, namespace)
        item = store.memory(tenant, namespace, record_id)
        if item is None:
            raise HTTPException(404, "Memory not found")
        return item

    @app.get("/memory/{namespace}")
    def search_memory(
        namespace: MemoryName, q: str = Query(default="", max_length=2000),
        limit: int = Query(default=5, ge=1, le=10),
        tenant: str = Depends(auth.tenant_dependency("memory:read")),
    ) -> dict:
        namespace_allowed(tenant, namespace)
        return {"search_type": "lexical", "records": memory.search(tenant, namespace, q, limit)}

    @app.delete("/memory/{namespace}/{record_id}")
    def delete_memory(
        namespace: MemoryName, record_id: MemoryName,
        tenant: str = Depends(auth.tenant_dependency("memory:write")),
    ) -> dict:
        namespace_allowed(tenant, namespace)
        if not store.delete_memory(tenant, namespace, record_id):
            raise HTTPException(404, "Memory not found")
        return {"deleted": True}

    @app.get("/usage")
    def usage(
        request: Request, run_id: str | None = None, tenant: str = Depends(auth.tenant_dependency("usage:read")),
    ) -> dict:
        if run_id is not None:
            authorized_run(request, tenant, run_id, "usage:read")
        principal = auth.authenticate(request.headers.get("authorization"))
        return store.usage(tenant, run_id, namespaces=principal.attributes.get("memory_namespaces"))

    @app.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str, request: Request, tenant: str = Depends(auth.tenant_dependency("workflow:read"))) -> dict:
        config.features.require("artifacts")
        namespaces = store.artifact_namespaces(tenant, artifact_id)
        if not namespaces:
            raise HTTPException(404, "Artifact not found")
        principal = auth.authenticate(request.headers.get("authorization"))
        permitted = False
        for namespace in namespaces:
            try:
                auth.require(principal, "workflow:read", {"tenant": tenant, "namespace": namespace})
                permitted = True
                break
            except HTTPException:
                continue
        if not permitted:
            raise HTTPException(403, "Artifact namespace denied")
        try:
            item = tools.artifacts.get(tenant, artifact_id)
        except ValueError as exc:
            raise HTTPException(404, "Artifact not found") from exc
        if item is None:
            raise HTTPException(404, "Artifact not found")
        return item

    install_interfaces(app, store, submit_workflow, auth, config)
    app.add_middleware(BodyLimitMiddleware, max_body_bytes=512_000)
    return app


app = create_app()
