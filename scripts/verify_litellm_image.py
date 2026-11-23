"""Verify the pinned gateway image's release identity before deployment.

Install a verified official Cosign binary first. This command performs no
deployment and does not disable signature, certificate or transparency checks.
The identity comes from the v1.110.2 release notes, which supersede the older
key-based instructions on the generic Docker security page.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/berriai/litellm:v1.110.2@sha256:ef798594ca3db8d81bad2e1439f536f49b71fe095ee9f20416f5e149b4e22035"
IDENTITY = "https://github.com/BerriAI/project-releaser/.github/workflows/release.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
RELEASE = "https://github.com/BerriAI/litellm/releases/tag/v1.110.2"


def verify(cosign: str) -> dict:
    source = (ROOT / "deploy/litellm/compose.yaml").read_bytes()
    images = re.findall(r"^\s+image:\s*(ghcr\.io/berriai/litellm:[^\s]+)\s*$", source.decode(), re.MULTILINE)
    if images != [IMAGE]:
        raise ValueError("Gateway image pin changed; review its release signing identity before updating verification")
    executable = shutil.which(cosign)
    if executable is None:
        raise ValueError("Cosign is required; install a verified official release")
    version = subprocess.run([executable, "version", "--json"], capture_output=True, text=True, check=True, timeout=10)
    digest = IMAGE.split("@", 1)[1]
    command = [executable, "verify", "--timeout=120s", "--certificate-identity", IDENTITY,
               "--certificate-oidc-issuer", ISSUER, "--output=json", "ghcr.io/berriai/litellm@" + digest]
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=130)
    signatures = json.loads(result.stdout)
    if not isinstance(signatures, list) or not signatures:
        raise ValueError("Cosign returned no verified signatures")
    evidence = []
    for signature in signatures:
        critical, optional = signature["critical"], signature["optional"]
        if (critical["image"]["docker-manifest-digest"] != digest
                or critical["identity"]["docker-reference"] != "ghcr.io/berriai/litellm"
                or optional.get("Subject") != IDENTITY or optional.get("Issuer") != ISSUER):
            raise ValueError("Verified signature identity differs from the approved release contract")
        bundle = optional.get("Bundle", {}).get("Payload", {})
        evidence.append({"digest": digest, "subject": optional["Subject"], "issuer": optional["Issuer"],
                         "workflow_sha": optional.get("githubWorkflowSha"),
                         "rekor_log_index": bundle.get("logIndex"),
                         "rekor_integrated_time": bundle.get("integratedTime")})
    return {"schema_version": 1, "status": "verified", "image": IMAGE,
            "verified_at": datetime.now(timezone.utc).isoformat(), "release_identity_source": RELEASE,
            "cosign_version": json.loads(version.stdout),
            "cosign_binary_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            "compose_source_sha256": hashlib.sha256(source).hexdigest(),
            "cosign_output_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "signatures": evidence, "container_started": False, "model_inference": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cosign", default="cosign")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.cosign)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Registry/client diagnostics can contain credential or proxy details.
        # A failure never produces a successful verification report.
        report = {"schema_version": 1, "status": "failed", "image": IMAGE,
                  "error_type": type(exc).__name__, "container_started": False, "model_inference": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "image": IMAGE}))
    raise SystemExit(0 if report["status"] == "verified" else 1)


if __name__ == "__main__":
    main()
