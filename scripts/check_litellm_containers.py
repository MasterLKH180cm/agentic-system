"""Exercise real running Compose application → TLS gateway → fixture paths.

Requires the opt-in stack plus tests/litellm_docker_fixture.py. This intentionally
does not start, stop, reconfigure or remove any service or persistent volume.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

RAG_PROBE = """
import json
from rags.agents.config import AgentPlatformConfig
from rags.agents.router import ModelRouter
config = AgentPlatformConfig.from_env()
assert config.gateway_ca_sha256 and config.gateway_auth_mode == 'required'
assert config.gateway_url == 'https://litellm:4000/v1' and not config.allow_http
reply = ModelRouter(config).complete(model='inkling', messages=[{'role':'user','content':'FIXTURE_CASE:normal'}])
assert reply.content == 'Fixture answer' and reply.usage['prompt_tokens'] == 7
print(json.dumps({'id':'rag_container_gateway_generation','passed':True,'reported_tokens':10}))
"""

AGENT_PROBE = """
import asyncio,json
from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.store import Store
config = Settings.from_env()
assert config.routes[0].ca_file and config.routes[0].base_url == 'https://litellm:4000/v1'
store = Store(config.db_path)
assert store.is_postgres
gateway = ModelGateway(config, store)
before = store.usage('local')['charged_tokens']
answer = asyncio.run(gateway.complete('Synthetic container check','FIXTURE_CASE:normal',tenant='local'))
assert answer == 'Fixture answer'
usage = store.usage('local')
assert usage['charged_tokens'] == before + 10 and not usage['cost_complete']
print(json.dumps({'id':'agent_container_gateway_generation_and_accounting','passed':True,'reported_tokens':10}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"schema_version": "litellm-container-acceptance-v1", "status": "failed",
              "evidence_kind": "http_contract_fixture", "real_model_inference": False,
              "checks": [], "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        for service, probe in (("rags", RAG_PROBE), ("api", AGENT_PROBE)):
            result = subprocess.run([
                "docker", "compose", "-f", "compose.yaml", "-f", "compose.litellm.yaml", "--profile", "litellm",
                "exec", "-T", service, "python", "-c", probe,
            ], cwd=ROOT, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError("Container gateway contract failed: " + service)
            value = json.loads(result.stdout.strip().splitlines()[-1])
            if value.get("passed") is not True:
                raise RuntimeError("Container gateway contract did not pass")
            report["checks"].append(value)
        report["status"] = "passed"
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "checks": len(report["checks"])}))


if __name__ == "__main__":
    main()
