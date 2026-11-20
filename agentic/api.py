"""Tenant-scoped workflow API and separate human-approver credentials."""
import hmac
import json
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.planner import plan
from agentic.schemas import ApprovalIn, WorkflowIn
from agentic.store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(title="Agentic Workflow System", version="0.1.0")
    store = Store(config.db_path)
    model = ModelGateway(config)
    app.state.store = store

    try:
        approver_keys = json.loads(os.getenv("AGENT_APPROVER_KEYS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("AGENT_APPROVER_KEYS_JSON must be valid JSON") from exc
    if not isinstance(approver_keys, dict):
        raise ValueError("AGENT_APPROVER_KEYS_JSON must be an object")
    if os.getenv("AGENT_ENV", "production") in {"development", "test"} and not approver_keys:
        approver_keys = {"local-approver-key": "local"}
    if any(
        not isinstance(key, str) or not key or not isinstance(tenant, str) or not tenant
        for key, tenant in approver_keys.items()
    ):
        raise ValueError("Approver tokens must map to tenant IDs")

    if set(approver_keys) & set(config.keys):
        raise ValueError("Approver and requester credentials must be disjoint")

    def authorize(auth_header: str | None, tokens: dict[str, str]) -> str:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        supplied = auth_header[7:]
        for key, tenant in tokens.items():
            if hmac.compare_digest(supplied, key):
                return tenant
        raise HTTPException(status_code=403, detail="Not authorized")

    def user(authorization: Annotated[str | None, Header()] = None) -> str:
        return authorize(authorization, config.keys)

    def approver(authorization: Annotated[str | None, Header()] = None) -> str:
        return authorize(authorization, approver_keys)

    @app.get("/live")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health")
    def health(tenant: Annotated[str, Depends(user)]) -> dict:
        return {"status": "ok", "tenant": tenant}

    @app.get("/tools")
    def tools(tenant: Annotated[str, Depends(user)]) -> dict:
        return {
            "tenant": tenant,
            "allowlisted": [
                "rag.search", "math.calculate", "data.merge",
                "model.summarize", "artifact.publish",
            ],
            "approval_required": ["artifact.publish"],
        }

    @app.post("/workflows", status_code=status.HTTP_202_ACCEPTED)
    async def submit(payload: WorkflowIn, tenant: Annotated[str, Depends(user)]) -> dict:
        if payload.steps is None:
            try:
                payload.steps = await plan(payload.goal, model)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Validate model-generated steps under the same strict DAG constraints.
        payload = WorkflowIn.model_validate(payload.model_dump())
        try:
            return store.submit(tenant, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/workflows")
    def workflows(tenant: Annotated[str, Depends(user)]) -> list[dict]:
        return store.list(tenant)

    @app.get("/workflows/{run_id}")
    def workflow(run_id: str, tenant: Annotated[str, Depends(user)]) -> dict:
        value = store.get(tenant, run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return value

    @app.get("/workflows/{run_id}/events")
    def events(run_id: str, tenant: Annotated[str, Depends(user)]) -> list[dict]:
        if store.get(tenant, run_id) is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return store.events(tenant, run_id)

    @app.post("/workflows/{run_id}/approve/{step_id}")
    def approve(
        run_id: str, step_id: str, payload: ApprovalIn,
        tenant: Annotated[str, Depends(approver)],
    ) -> dict:
        if not store.approve(tenant, run_id, step_id, payload.approved, payload.reason):
            raise HTTPException(status_code=409, detail="Step is not awaiting approval")
        return {"run_id": run_id, "step_id": step_id, "approved": payload.approved}

    @app.post("/workflows/{run_id}/cancel")
    def cancel(run_id: str, tenant: Annotated[str, Depends(user)]) -> dict:
        if not store.cancel(tenant, run_id):
            raise HTTPException(status_code=409, detail="Workflow cannot be cancelled")
        return {"run_id": run_id, "cancelled": True}

    return app


app = create_app()
