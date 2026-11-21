"""Small, permission-bounded tool registry. No shell, arbitrary SQL or URL fetching."""
import ast
import asyncio
import math
from dataclasses import dataclass
import json
import re
from typing import Any, Awaitable, Callable

import httpx

from agentic.config import Settings
from agentic.admission import CONTRACT_VERSION, normalize_structured, parse_assessment, require_admission, upstream_identity
from agentic.evidence import (
    CITATION, SOURCE_ID, SYNTHESIS_SYSTEM, SynthesisEnvelope, collect_evidence,
    extractive_answer, normalize_citations, pack_evidence, refusal_result, strict_json_loads, validate_synthesis,
)
from agentic.model import ModelGateway
from agentic.control import ControlPlane
from agentic.memory import Memory
from agentic.schemas import ALL_TOOLS, ARGUMENT_MODELS, Step
from agentic.store import LeaseLost, Store
from agentic.storage_adapters import ArtifactStore


def safe_calculate(expression: str) -> float | int:
    if not isinstance(expression, str) or not 1 <= len(expression) <= 120:
        raise ValueError("Expression length must be 1..120")
    if any(c not in "0123456789.+-*/()% \t" for c in expression):
        raise ValueError("Only arithmetic literals are permitted")
    tree = ast.parse(expression, mode="eval")

    def evaluate(node: ast.AST, depth: int = 0) -> float | int:
        if depth > 16:
            raise ValueError("Expression too deep")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = evaluate(node.operand, depth + 1)
            value = -operand if isinstance(node.op, ast.USub) else operand
        elif isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)
        ):
            left, right = evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.Mod):
                value = left % right
            else:
                if abs(right) > 12 or abs(left) > 1_000_000:
                    raise ValueError("Exponentiation out of bounds")
                value = left**right
        else:
            raise ValueError("Unsupported arithmetic")
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError("Arithmetic result out of bounds")
        return value

    return evaluate(tree.body)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    handler: Callable[[dict, dict, dict], Awaitable[dict]]
    read_only: bool = True
    approval_required: bool = False
    internal: bool = False


class Tools:
    def __init__(self, config: Settings, model: ModelGateway, store: Store | None = None):
        self.config = config
        self.model = model
        self.store = store or model.store
        self.memory = Memory(self.store)
        self.artifacts = ArtifactStore()
        self.registry = {}
        for tool in (
            RegisteredTool("rag.search", self._search),
            RegisteredTool("model.summarize", self._summarize),
            RegisteredTool("math.calculate", self._calculate),
            RegisteredTool("data.merge", self._merge),
            RegisteredTool("memory.search", self._memory_search),
            RegisteredTool("artifact.publish", self._publish, read_only=False, approval_required=True),
            RegisteredTool("agent.run", self._agent, internal=True),
            RegisteredTool("workflow.plan", self._plan, internal=True),
        ):
            self.register(tool)

    def register(self, tool: RegisteredTool) -> None:
        # Registration is a trusted-code operation. HTTP clients cannot install
        # code or change argument contracts by editing registry metadata.
        if tool.name not in ARGUMENT_MODELS or tool.name in self.registry:
            raise ValueError("A registered tool needs a unique declared argument schema")
        self.registry[tool.name] = tool

    def describe(self) -> list[dict]:
        return [
            {"name": name, "read_only": tool.read_only, "approval_required": tool.approval_required,
             "arguments": ARGUMENT_MODELS[name].model_json_schema()}
            for name, tool in self.registry.items() if not tool.internal
        ]

    async def execute(self, job: dict, inputs: dict[str, Any]) -> dict:
        if len(json.dumps(inputs)) > 100000:
            raise ValueError("Tool inputs exceed size limit")
        name = job["tool"]
        tool = self.registry.get(name)
        if tool is None:
            raise ValueError("Tool is not registered")
        args = ARGUMENT_MODELS[name].model_validate(job.get("args", {})).model_dump()
        specification = job.get("specification", {})
        if name != "workflow.plan" and name not in specification.get("allowed_tools", ALL_TOOLS):
            raise PermissionError("Tool is not allowed by the snapshotted policy")
        if job.get("in_agent_loop") and (not tool.read_only or tool.internal):
            raise PermissionError("Adaptive plans can only execute read-only tools")
        if tool.approval_required and not job.get("approved"):
            raise PermissionError("Separate human approval is required")
        result = await tool.handler(job, args, inputs)
        if not isinstance(result, dict) or len(json.dumps(result)) > 100000:
            raise ValueError("Tool result exceeds size limit")
        return result

    async def _calculate(self, job, args, inputs) -> dict:
        return {"expression": args["expression"], "value": safe_calculate(args["expression"])}

    async def _merge(self, job, args, inputs) -> dict:
        return {"outputs": inputs}

    def evidence_policy(self, job):
        specification = job.get("specification", {})
        return (specification.get("rag_admission_policy", self.config.rag_admission_policy),
                specification.get("rag_admission_profile_ids", self.config.rag_admission_profile_ids))

    async def _search(self, job, args, inputs) -> dict:
        key = self.config.rag_keys.get(job["tenant"])
        if not key:
            raise PermissionError("No RAG credential for workflow tenant")
        payload = {"question": args["question"] or job["goal"], "mode": args["mode"],
                   "top_k": args["top_k"], "generate": False}
        for name in ("row_filters", "aggregate"):
            if args.get(name):
                payload[name] = args[name]
        # One deadline covers connection, headers and the entire streamed body.
        # Cancellation from the worker's enclosing step deadline still wins.
        async with asyncio.timeout(self.config.rag_timeout_seconds), httpx.AsyncClient(
            timeout=None, follow_redirects=False,
        ) as client:
            async with client.stream("POST",
                self.config.rag_url + "/query",
                headers={"Authorization": "Bearer " + key, "Accept-Encoding": "identity"},
                json=payload,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed RAG responses are not accepted")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 1_000_000:
                        raise ValueError("RAG response exceeds size limit")
                    body.extend(chunk)
                data = strict_json_loads(body)
        if not isinstance(data, dict):
            raise ValueError("Invalid RAG response")
        citations = normalize_citations(data.get("citations", []))
        if len(citations) > args["top_k"]:
            raise ValueError("RAG returned more evidence than requested")
        assessment = parse_assessment(data, len(citations))
        structured = None
        if assessment["outcome"] == "success" and assessment["admission"]["basis"] == "structured_exact":
            structured = normalize_structured(data.get("structured_result"))
        policy, profiles = self.evidence_policy(job)
        require_admission(assessment, structured, policy, profiles)
        strategy = data.get("mode_used")
        if strategy is not None and (not isinstance(strategy, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", strategy)):
            raise ValueError("Invalid RAG strategy identifier")
        quarantined = []
        if assessment["outcome"] == "insufficient_evidence":
            quarantined = [source["chunk_id"] for source in citations]
            diagnostics = data.get("retrieval_diagnostics", {})
            if not isinstance(diagnostics, dict):
                raise ValueError("Invalid retrieval diagnostics")
            identifiers = diagnostics.get("candidate_ids", [])
            if not isinstance(identifiers, list) or len(identifiers) > 20 or any(
                not isinstance(identifier, str) or not SOURCE_ID.fullmatch(identifier) for identifier in identifiers
            ) or len(set(identifiers)) != len(identifiers):
                raise ValueError("Invalid diagnostic source identifiers")
            quarantined = list(dict.fromkeys([*quarantined, *identifiers]))
            citations = []
        # Only citations returned by the configured, tenant-authenticated RAG API
        # can enter the evidence set; the model never supplies source records.
        observations = [] if structured is None else [{"step": "structured", "expression": structured["aggregate"]["operation"],
                                                       "value": structured["aggregate"]["value"]}]
        result = {"answer": extractive_answer(citations, observations) if citations or observations else None,
                  "citations": citations, "retrieval": assessment, "quarantined_ids": quarantined,
                  "strategy": strategy, "requested_strategy": args["mode"], "generation": False,
                  "evidence_contract_version": CONTRACT_VERSION}
        if structured is not None:
            result["structured_result"] = structured
        if data.get("engine") is not None:
            if not isinstance(data["engine"], str) or len(data["engine"]) > 250:
                raise ValueError("Invalid RAG engine identity")
            result["engine"] = data["engine"]
        upstream = upstream_identity(data.get("trace"))
        if upstream is not None:
            if upstream["engine"] != strategy:
                raise ValueError("Reported upstream execution differs from the actual strategy")
            result["upstream"] = upstream
        return result

    async def _summarize(self, job, args, inputs) -> dict:
        question = args["question"] or job["goal"]
        policy, profiles = self.evidence_policy(job)
        bundle = collect_evidence(inputs, policy=policy, profiles=profiles)
        if not bundle.sources and not bundle.observations:
            return refusal_result(bundle.refusal_reason)
        capacity = self.model.input_capacity(SYNTHESIS_SYSTEM, tenant=job["tenant"], run_id=job["run_id"])
        prompt, citations, observations = pack_evidence(question, bundle, limit=capacity)
        if not citations and not observations:
            return refusal_result("context_budget_exhausted", stop_reason="context_budget_exhausted")
        answer = await self.model.complete(
            SYNTHESIS_SYSTEM, prompt,
            tenant=job["tenant"], run_id=job["run_id"], purpose="synthesis",
        )
        if answer is None:
            answer = extractive_answer(citations, observations)
        else:
            envelope = SynthesisEnvelope.model_validate(strict_json_loads(answer))
            if envelope.outcome == "insufficient_evidence":
                result = refusal_result("model_rejected", stop_reason="model_refused")
                result["quarantined_ids"] = [source["chunk_id"] for source in citations]
                return result
            answer = envelope.answer
            if set(CITATION.findall(answer)) != set(envelope.cited_ids):
                raise ValueError("Synthesis citation list differs from its answer")
        cited = validate_synthesis(answer, citations, observations)
        return {"outcome": "success", "answer": answer, "citations": cited, "refusal_reason": None,
                "observations": observations, "evidence_contract_version": CONTRACT_VERSION,
                "source_admissions": {source["chunk_id"]: bundle.admissions[source["chunk_id"]]
                                      for source in cited if source["chunk_id"] in bundle.admissions}}

    async def _memory_search(self, job, args, inputs) -> dict:
        if args["namespace"] not in job.get("specification", {}).get("allowed_namespaces", ["default"]):
            raise PermissionError("Memory namespace is not allowed")
        if args["namespace"] != job.get("specification", {}).get("memory_namespace", "default"):
            raise PermissionError("Memory tool must use the workflow's bound namespace")
        rows = self.memory.search(job["tenant"], args["namespace"], args["query"] or job["goal"], args["limit"])
        return self.memory_evidence(rows)

    @staticmethod
    def memory_evidence(rows: list[dict]) -> dict:
        return {
            "answer": "\n".join(row["content"][:2000] for row in rows),
            "citations": [{"chunk_id": f"memory:{row['namespace']}:{row['id']}", "document_id": row["id"],
                           "source": f"memory:{row['namespace']}", "title": row["id"],
                           "text": row["content"][:4000], "source_run_id": row["source_run_id"]}
                          for row in rows],
            "search_type": "lexical",
        }

    async def _publish(self, job, args, inputs) -> dict:
        reference = await asyncio.to_thread(self.artifacts.put, job["tenant"], inputs)
        if not self.store.register_artifact(job, reference):
            raise LeaseLost("Artifact cannot be attached to a cancelled or stale workflow")
        return {"published": True, "scope": "internal-run-record", "data": inputs, "artifact_ref": reference}

    async def _agent(self, job, args, inputs) -> dict:
        from agentic.runtime import AgentRuntime
        return await AgentRuntime(self.store, self).run(job)

    async def _plan(self, job, args, inputs) -> dict:
        from agentic.planner import plan
        if job.get("in_agent_loop") or job.get("specification", {}).get("runtime_mode", "dag") != "dag":
            raise PermissionError("Nested planning is not permitted")
        if job.get("checkpoint", {}).get("expanded"):
            return {"planned_steps": job["checkpoint"]["step_ids"]}
        steps = await plan(job["goal"], self.model, specification=job.get("specification", {}),
                           tenant=job["tenant"], run_id=job["run_id"])
        ControlPlane.validate_steps([Step.model_validate(s.model_dump()) for s in steps], job.get("specification", {}))
        return {"planned_steps": self.store.expand_plan(job, steps)}
