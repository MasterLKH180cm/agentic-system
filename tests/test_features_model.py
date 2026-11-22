"""Real local provider requests prove feature ceilings and accounting behavior."""
from dataclasses import replace
import json

import httpx
import pytest

from agentic.features import FeatureDisabled, Features
from agentic.schemas import Step, WorkflowIn
from agentic.worker import Worker
from test_features_integration import REQUESTER, factory as factory
from test_model_failures import provider as provider

pytestmark = pytest.mark.integration


def snapshotted_run(store, feature, enabled):
    return store.submit("tenant-a", WorkflowIn(goal="Calculate 6 * 7", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
    ]), specification={"feature_flags": {feature: enabled}})["id"]


@pytest.mark.parametrize("restriction", ["current", "snapshot"])
async def test_generation_disabled_prevents_requests_and_reservations(provider, restriction):
    model, store, _, state = provider
    run_id = snapshotted_run(store, "model_generation", restriction != "snapshot")
    if restriction == "current":
        model.config = replace(model.config, features=Features({"model_generation": False}))
    with pytest.raises(FeatureDisabled, match="model_generation"):
        model.input_capacity("System", tenant="tenant-a", run_id=run_id)
    with pytest.raises(FeatureDisabled, match="model_generation"):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id)
    assert state["calls"] == []
    assert store.usage("tenant-a", run_id)["calls"] == []
    assert model.config.provider == "routed"


@pytest.mark.parametrize("restriction", ["current", "snapshot"])
async def test_fallback_disabled_stops_at_first_applicable_route_with_durable_charge(provider, restriction):
    model, store, _, state = provider
    run_id = snapshotted_run(store, "model_fallback", restriction != "snapshot")
    if restriction == "current":
        model.config = replace(model.config, features=Features({"model_fallback": False}))
    state["replies"]["primary"]["status"] = 503
    with pytest.raises(httpx.HTTPStatusError) as error:
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id)
    assert error.value.response.status_code == 503
    assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions"]
    usage = store.usage("tenant-a", run_id)
    assert len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "uncertain_http_error"
    assert usage["charged_tokens"] == model.input_units("System", "Question") + 32


async def test_enabled_fallback_still_uses_second_provider_and_settles_usage(provider):
    model, store, run_id, state = provider
    state["replies"]["primary"]["status"] = 503
    assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id) == "Fixture answer"
    assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions", "/secondary/messages"]
    usage = store.usage("tenant-a", run_id)
    assert [call["status"] for call in usage["calls"]] == ["uncertain_http_error", "reported"]


async def test_online_planner_cannot_install_disabled_tool_steps(provider, factory):
    model, _, _, state = provider
    body = {"choices": [{"message": {"content": json.dumps({"steps": [
        {"id": "calculate", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
    ]})}}], "usage": {"prompt_tokens": 20, "completion_tokens": 25}}
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    client, app, _ = factory({"math": False}, provider="routed", routes=model.config.routes)
    response = client.post("/workflows", headers=REQUESTER, json={"goal": "calculate 6 * 7"})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert await Worker(app.state.store, app.state.tools).once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "failed" and len(run["steps"]) == 1
    assert run["steps"][0]["tool"] == "workflow.plan"
    assert run["steps"][0]["result"]["feature"] == "math"
    assert len(state["calls"]) == 1
    assert app.state.store.usage("tenant-a", run_id)["calls"][0]["status"] == "reported"


async def test_online_generation_disable_rejects_goal_but_keeps_explicit_arithmetic(factory):
    client, app, _ = factory({"model_generation": False}, provider="openai")
    goal = {"goal": "calculate 6 * 7"}
    for mode in ("dag", "agent_loop"):
        response = client.post("/workflows", headers=REQUESTER, json={**goal, "runtime_mode": mode})
        assert response.status_code == 403
        assert response.json()["detail"]["feature"] == "model_generation"
    response = client.post("/workflows", headers=REQUESTER, json={**goal, "steps": [
        {"id": "calculate", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
    ]})
    assert response.status_code == 202
    assert await Worker(app.state.store, app.state.tools).once() == 1
    run = app.state.store.get("tenant-a", response.json()["id"])
    assert run["status"] == "completed" and run["steps"][0]["result"]["value"] == 42
    assert app.state.store.usage("tenant-a")["calls"] == []
