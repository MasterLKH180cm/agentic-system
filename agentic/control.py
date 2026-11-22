"""Immutable definitions and policy intersections, resolved before execution."""
from agentic.config import Settings
from agentic.features import Features
from agentic.schemas import (
    ALL_TOOLS, READ_ONLY_TOOLS, REGISTRY_MODELS, PolicyDefinition, Step, WorkflowIn, offline_plan,
)
from agentic.store import Store


class ControlPlane:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        for tenant in settings.tenant_policies:
            self.server_policy(tenant)

    def server_policy(self, tenant: str) -> PolicyDefinition:
        raw = {"name": "server", "version": 1, "max_model_tokens": self.settings.max_model_tokens,
               **self.settings.tenant_policies.get(tenant, {})}
        return PolicyDefinition.model_validate(raw)

    def register(self, tenant: str, kind: str, payload: dict) -> dict:
        self.settings.features.require("registry")
        if kind not in REGISTRY_MODELS:
            raise LookupError("Unknown definition kind")
        if kind == "agents":
            self.settings.features.require("agent_loop")
        definition = REGISTRY_MODELS[kind].model_validate(payload)
        policy = self.server_policy(tenant)
        if kind in {"agents", "policies"}:
            if not set(definition.allowed_tools) <= set(policy.allowed_tools):
                raise PermissionError("Definition grants tools outside server policy")
        if kind == "agents" and definition.memory_namespace not in policy.allowed_namespaces:
            raise PermissionError("Agent memory namespace is not allowed")
        if kind == "policies" and not set(definition.allowed_namespaces) <= set(policy.allowed_namespaces):
            raise PermissionError("Definition grants namespaces outside server policy")
        if kind == "workflows":
            self.validate_steps(definition.steps, policy.model_dump(), features=self.settings.features,
                                online=self.settings.provider != "offline")
        return self.store.register(tenant, kind, definition.model_dump())

    @staticmethod
    def validate_steps(
        steps: list[Step], specification: dict, *, loop: bool = False,
        features: Features | None = None, online: bool = False,
    ) -> None:
        permissions = features if features is not None else Features()
        snapshot = specification.get("feature_flags")
        permissions.require("agent_loop" if loop else "dag", snapshot)
        allowed = set(specification.get("allowed_tools", ALL_TOOLS))
        if loop:
            allowed &= READ_ONLY_TOOLS
        if len(steps) > specification.get("max_steps", 24):
            raise PermissionError("Plan exceeds policy step limit")
        for step in steps:
            permissions.require_tool(step.tool, snapshot=snapshot, online=online,
                                     approval_required=step.approval_required)
            if step.tool not in allowed or step.tool == "workflow.plan":
                raise PermissionError("Tool is not permitted by the workflow policy")
            if step.tool == "memory.search" and step.args.get("namespace", "default") not in specification.get(
                "allowed_namespaces", ["default"]
            ):
                raise PermissionError("Memory namespace is not permitted by the workflow policy")
            if step.tool == "memory.search" and "memory_namespace" in specification and (
                step.args.get("namespace", "default") != specification["memory_namespace"]
            ):
                raise PermissionError("Memory tools must use the workflow's bound namespace")
            if loop and step.approval_required:
                raise PermissionError("Approval-gated steps require an explicit DAG")

    def validate_submission_features(self, payload: WorkflowIn) -> None:
        features = self.settings.features
        features.require(payload.runtime_mode)
        if payload.agent_id or payload.workflow_id or payload.policy_id:
            features.require("registry")
        if payload.runtime_mode == "agent_loop":
            features.require_tool("agent.run", online=self.settings.provider != "offline")
        elif payload.steps is not None:
            for step in payload.steps:
                features.require_tool(step.tool, online=self.settings.provider != "offline",
                                      approval_required=step.approval_required)
        elif not payload.workflow_id:
            if self.settings.provider != "offline":
                features.require("model_generation")
            else:
                for step in offline_plan(payload.goal):
                    features.require_tool(step.tool)

    def resolve(self, tenant: str, payload: WorkflowIn) -> tuple[WorkflowIn, dict]:
        self.validate_submission_features(payload)
        server = self.server_policy(tenant).model_dump()
        policy = server
        snapshots = {}
        if payload.policy_id:
            policy = self.store.definition(tenant, "policies", payload.policy_id, payload.policy_version)
            if policy is None:
                raise LookupError("Policy definition not found")
            snapshots["policy"] = policy
        allowed = set(server["allowed_tools"]) & set(policy["allowed_tools"])
        allowed = {tool for tool in allowed if self.settings.features.tool_enabled(
            tool, online=self.settings.provider != "offline",
        )}
        namespaces = set(server["allowed_namespaces"]) & set(policy["allowed_namespaces"])
        agent = None
        namespace = payload.memory_namespace
        if payload.agent_id:
            agent = self.store.definition(tenant, "agents", payload.agent_id, payload.agent_version)
            if agent is None:
                raise LookupError("Agent definition not found")
            snapshots["agent"] = agent
            allowed &= set(agent["allowed_tools"]) | {"agent.run"}
            if "memory_namespace" in payload.model_fields_set and namespace != agent["memory_namespace"]:
                raise PermissionError("Agent memory namespace cannot be overridden")
            namespace = agent["memory_namespace"]
        if namespace not in namespaces:
            raise PermissionError("Workflow memory namespace is not allowed")

        def bounded(name: str, extra: int | None = None) -> int:
            maximum = min(server[name], policy[name], extra if extra is not None else server[name])
            requested = getattr(payload, name)
            if name in payload.model_fields_set and requested > maximum:
                raise PermissionError(f"Requested {name} exceeds policy")
            return min(requested, maximum)

        cost_limits = [value for value in (server["max_cost_usd"], policy["max_cost_usd"]) if value is not None]
        cost = payload.max_cost_usd
        if cost_limits:
            ceiling = min(cost_limits)
            if cost is not None and cost > ceiling:
                raise PermissionError("Requested cost budget exceeds policy")
            cost = min(cost, ceiling) if cost is not None else ceiling
        specification = {
            "feature_flags": self.settings.features.effective(),
            "evidence_contract_version": 2,
            "rag_admission_policy": self.settings.rag_admission_policy,
            "rag_admission_profile_ids": list(self.settings.rag_admission_profile_ids),
            "runtime_mode": payload.runtime_mode,
            "allowed_tools": sorted(allowed), "allowed_namespaces": sorted(namespaces),
            "memory_namespace": namespace,
            "max_steps": min(server["max_steps"], policy["max_steps"]),
            "max_rounds": bounded("max_rounds", agent["max_rounds"] if agent else None),
            "max_model_tokens": bounded("max_model_tokens"), "max_cost_usd": cost,
            "definitions": snapshots,
            "instructions": agent["instructions"] if agent else "",
            "evaluator": agent["evaluator"] if agent else (
                "arithmetic" if offline_plan(payload.goal)[0].tool == "math.calculate" else "evidence"
            ),
        }
        if payload.runtime_mode == "agent_loop":
            if "agent.run" not in allowed:
                raise PermissionError("Agent runtime is not permitted by policy")
            steps = [Step(id="agent", tool="agent.run")]
        elif payload.workflow_id:
            definition = self.store.definition(tenant, "workflows", payload.workflow_id, payload.workflow_version)
            if definition is None:
                raise LookupError("Workflow definition not found")
            snapshots["workflow"] = definition
            steps = [Step.model_validate(s) for s in definition["steps"]]
            self.validate_steps(steps, specification, features=self.settings.features,
                                online=self.settings.provider != "offline")
        elif payload.steps is not None:
            steps = payload.steps
            self.validate_steps(steps, specification, features=self.settings.features,
                                online=self.settings.provider != "offline")
        elif self.settings.provider != "offline":
            steps = [Step(id="planner", tool="workflow.plan")]
        else:
            steps = offline_plan(payload.goal)
            self.validate_steps(steps, specification, features=self.settings.features)
        # These compiled engine steps are never accepted from a client payload.
        compiled = payload.model_copy(update={"steps": steps})
        return compiled, specification
