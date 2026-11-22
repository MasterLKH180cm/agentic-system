"""Tenant-scoped artifact storage, with explicit local or S3 configuration."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from agentic.features import Features


class ArtifactStore:
    def __init__(self, features: Features | None = None):
        self.features = features if features is not None else Features.from_env()
        self.features.require("artifacts")
        self.backend = os.getenv("AGENT_ARTIFACT_BACKEND", "local")
        if self.backend not in {"local", "s3"}:
            raise ValueError("Unknown artifact backend")
        self.root = Path(os.getenv("AGENT_ARTIFACT_DIR", "data/artifacts"))
        self.client = None
        if self.backend == "s3":
            self.bucket = os.getenv("AGENT_S3_BUCKET", "")
            endpoint = os.getenv("AGENT_S3_ENDPOINT", "") or None
            if not self.bucket:
                raise ValueError("S3 artifact storage requires AGENT_S3_BUCKET")
            if endpoint:
                parsed = urlparse(endpoint)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
                    raise ValueError("Invalid S3 endpoint")
                if os.getenv("AGENT_ENV", "production") == "production" and parsed.scheme != "https":
                    raise ValueError("Production object storage endpoint requires HTTPS")
            try:
                import boto3
                from botocore.config import Config
                self.client = boto3.client("s3", endpoint_url=endpoint,
                                           config=Config(connect_timeout=3, read_timeout=5,
                                                         retries={"max_attempts": 2},
                                                         s3={"addressing_style": "path"}))
                self.client.head_bucket(Bucket=self.bucket)
            except Exception as exc:
                raise ValueError("Configured S3 artifact store is unavailable") from exc

    @staticmethod
    def _key(tenant: str, artifact_id: str) -> str:
        if not tenant or not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise ValueError("Invalid artifact identity")
        tenant_id = hashlib.sha256(tenant.encode()).hexdigest()
        return f"artifacts/{tenant_id}/{artifact_id}.json"

    def put(self, tenant: str, value: dict) -> dict:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > 100_000:
            raise ValueError("Artifact exceeds 100KB")
        identifier = hashlib.sha256(body).hexdigest()
        key = self._key(tenant, identifier)
        if self.client is not None:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        else:
            target = self.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = stream.name
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                temporary = None
            finally:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
        return {"artifact_id": identifier, "storage": self.backend, "size_bytes": len(body)}

    def get(self, tenant: str, artifact_id: str) -> dict | None:
        key = self._key(tenant, artifact_id)
        if self.client is not None:
            from botocore.exceptions import ClientError
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return None
                raise
            stream = response["Body"]
            try:
                body = stream.read(100_001)
            finally:
                stream.close()
        else:
            target = self.root / key
            if not target.exists():
                return None
            with target.open("rb") as stream:
                body = stream.read(100_001)
        if len(body) > 100_000 or hashlib.sha256(body).hexdigest() != artifact_id:
            raise ValueError("Artifact integrity check failed")
        return json.loads(body)
