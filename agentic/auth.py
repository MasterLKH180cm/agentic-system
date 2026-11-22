"""Tenant principals, verified OIDC JWTs, role/scope checks and shared limits."""
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request
import jwt


ROLE_SCOPES = {
    "reader": {"workflow:read", "registry:read", "memory:read", "usage:read"},
    "requester": {"workflow:read", "workflow:write", "registry:read", "registry:write",
                  "memory:read", "memory:write", "usage:read"},
    "approver": {"workflow:read", "workflow:approve", "registry:read"},
}


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    roles: frozenset[str]
    scopes: frozenset[str]
    attributes: dict = field(default_factory=dict)


class RateLimiter:
    """Atomic fixed-window limits shared across API processes; Redis is optional."""
    def __init__(self, path: str, per_minute: int):
        if not 1 <= per_minute <= 100_000:
            raise ValueError("Rate limit must be 1..100000 requests per minute")
        self.limit = per_minute
        self.path = path
        self.redis = None
        url = os.getenv("AGENT_REDIS_URL", "")
        if url:
            try:
                import redis
                self.redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
                self.redis.ping()
            except Exception as exc:
                raise ValueError("Configured Redis rate limiter is unavailable") from exc
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS request_limits (bucket TEXT PRIMARY KEY, "
                           "window INTEGER NOT NULL, requests INTEGER NOT NULL)")

    def take(self, bucket: str) -> bool:
        window = int(time.time() // 60)
        digest = hashlib.sha256(bucket.encode()).hexdigest()
        if self.redis is not None:
            # Increment and expiry are atomic even if an API process crashes.
            script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],120) end; return n"
            try:
                return int(self.redis.eval(script, 1, f"agent:limit:{window}:{digest}")) <= self.limit
            except Exception as exc:
                raise HTTPException(503, "Rate limiter unavailable") from exc
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM request_limits WHERE window<?", (window - 2,))
            db.execute("INSERT INTO request_limits(bucket,window,requests) VALUES(?,?,1) "
                       "ON CONFLICT(bucket) DO UPDATE SET window=excluded.window, "
                       "requests=CASE WHEN request_limits.window=excluded.window "
                       "THEN request_limits.requests+1 ELSE 1 END", (digest, window))
            return db.execute("SELECT requests FROM request_limits WHERE bucket=?", (digest,)).fetchone()[0] <= self.limit


class Authenticator:
    def __init__(self, config):
        self.config = config
        self.production = os.getenv("AGENT_ENV", "production").lower() not in {"development", "test"}
        self.approvers = json.loads(os.getenv("AGENT_APPROVER_KEYS_JSON", "{}"))
        if not self.approvers and not self.production:
            self.approvers = {"local-approver-key": "local"}
        if not isinstance(self.approvers, dict) or any(
            not isinstance(k, str) or not k or not isinstance(t, str) or not t for k, t in self.approvers.items()
        ):
            raise ValueError("Approver keys must map nonempty credentials to tenants")
        if set(self.approvers) & set(config.keys):
            raise ValueError("Requester and approver credentials must be disjoint")
        if self.production and any(len(k) < 24 for k in self.approvers):
            raise ValueError("Production approver credentials require at least 24 characters")
        self.issuer = os.getenv("AGENT_OIDC_ISSUER", "")
        self.audience = os.getenv("AGENT_OIDC_AUDIENCE", "")
        jwks_url = os.getenv("AGENT_OIDC_JWKS_URL", "")
        if any((self.issuer, self.audience, jwks_url)) and not all((self.issuer, self.audience, jwks_url)):
            raise ValueError("OIDC requires issuer, audience, and JWKS URL together")
        self.jwks = None
        if jwks_url:
            parsed = urlparse(jwks_url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
                raise ValueError("OIDC JWKS URL must be an operator-controlled HTTP URL")
            if self.production and parsed.scheme != "https":
                raise ValueError("Production JWKS URL requires HTTPS")
            self.jwks = jwt.PyJWKClient(jwks_url, cache_keys=False, lifespan=300, timeout=5)
        self.claim_tenant = os.getenv("AGENT_OIDC_TENANT_CLAIM", "tenant_id")
        self.claim_roles = os.getenv("AGENT_OIDC_ROLES_CLAIM", "roles")
        self.algorithms = tuple(os.getenv("AGENT_OIDC_ALGORITHMS", "RS256").split(","))
        if not self.algorithms or not set(self.algorithms) <= {"RS256", "RS384", "RS512", "ES256", "ES384"}:
            raise ValueError("Only explicitly allowed asymmetric JWT algorithms are supported")
        path = os.getenv("AGENT_RATE_DB_PATH", "")
        if not path:
            path = config.db_path + ".limits.db" if "://" not in config.db_path else "data/limits.db"
        self.limiter = RateLimiter(path, int(os.getenv("AGENT_RATE_PER_MINUTE", "600")))

    @staticmethod
    def _principal(tenant, subject, roles, explicit_scopes=None, attributes=None):
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("Identity subject is required")
        if {"requester", "approver"} <= set(roles):
            raise ValueError("Requester and approver identities must be separate")
        permissions = set().union(*(ROLE_SCOPES.get(role, set()) for role in roles))
        # An explicit OAuth scope is a further restriction, never a role bypass.
        if explicit_scopes is not None:
            permissions &= explicit_scopes
        return Principal(tenant, subject, frozenset(roles), frozenset(permissions), attributes or {})

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:]
        if not token or len(token) > 16384:
            raise HTTPException(401, "Invalid credential")
        for key, tenant in self.config.keys.items():
            if hmac.compare_digest(token.encode(), key.encode()):
                return self._principal(tenant, "key:" + hashlib.sha256(key.encode()).hexdigest()[:16], {"requester"})
        for key, tenant in self.approvers.items():
            if hmac.compare_digest(token.encode(), key.encode()):
                return self._principal(tenant, "approval:" + hashlib.sha256(key.encode()).hexdigest()[:16], {"approver"})
        if self.jwks is not None:
            try:
                header = jwt.get_unverified_header(token)
                if header.get("alg") not in self.algorithms:
                    raise ValueError("Disallowed signing algorithm")
                key = self.jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=list(self.algorithms), issuer=self.issuer,
                                    audience=self.audience, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
                tenant = claims.get(self.claim_tenant)
                roles = claims.get(self.claim_roles, [])
                if not isinstance(tenant, str) or tenant not in self.config.rag_keys:
                    raise ValueError("Unconfigured tenant")
                if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                    raise ValueError("Invalid roles")
                scope = claims.get("scope")
                if scope is not None and not isinstance(scope, str):
                    raise ValueError("Invalid scope")
                namespaces = claims.get("memory_namespaces")
                if namespaces is not None and (
                    not isinstance(namespaces, list) or not all(isinstance(x, str) for x in namespaces)
                ):
                    raise ValueError("Invalid namespace attributes")
                return self._principal(tenant, claims["sub"], set(roles),
                                       set(scope.split()) if scope is not None else None,
                                       {"memory_namespaces": namespaces, "identity": "oidc"})
            except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(401, "Invalid identity token") from exc
        raise HTTPException(401, "Invalid credential")

    def require(self, principal: Principal, action: str, resource: dict | None = None) -> None:
        resource = resource or {}
        if action not in principal.scopes:
            raise HTTPException(403, "Permission denied")
        if resource.get("tenant", principal.tenant) != principal.tenant:
            raise HTTPException(403, "Tenant mismatch")
        namespaces = principal.attributes.get("memory_namespaces")
        if "namespace" in resource and namespaces is not None and resource["namespace"] not in namespaces:
            raise HTTPException(403, "Memory namespace denied")

    def tenant_dependency(self, action: str):
        def dependency(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
            principal = self.authenticate(authorization)
            resource = {"namespace": request.path_params["namespace"]} if "namespace" in request.path_params else {}
            self.require(principal, action, resource)
            if request.headers.get("x-agent-interface") == "mcp":
                self.config.features.require("mcp")
            if not self.limiter.take(principal.tenant + ":" + principal.subject):
                raise HTTPException(429, "Request rate exceeded", headers={"Retry-After": "60"})
            return principal.tenant
        return dependency
