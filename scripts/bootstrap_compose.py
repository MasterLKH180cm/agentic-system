"""Create private local Compose credentials without overwriting existing files."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex


ROOT = Path(__file__).resolve().parents[1]
FULL_STACK = (ROOT / "agentic").is_dir()


def create(directory: Path) -> list[Path]:
    token = secrets.token_urlsafe(32)
    values = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "RAGS_DB_PASSWORD": secrets.token_urlsafe(32),
        "RAGS_API_KEY": token,
        "RAGS_KEYS_JSON": json.dumps({token: "local"}),
    }
    values["RAGS_DATABASE_URL"] = (
        "postgresql://rags_app:" + values["RAGS_DB_PASSWORD"] + "@postgres:5432/rags"
    )
    clients = {"RAGS_API_KEY": token, "RAGS_URL": "http://127.0.0.1:8000"}
    if FULL_STACK:
        requester, approver = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        values.update({
            "AGENT_DB_PASSWORD": secrets.token_urlsafe(32),
            "AGENT_API_KEY": requester,
            "AGENT_APPROVER_API_KEY": approver,
            "AGENT_KEYS_JSON": json.dumps({requester: "local"}),
            "AGENT_APPROVER_KEYS_JSON": json.dumps({approver: "local"}),
            "AGENT_RAG_KEYS_JSON": json.dumps({"local": token}),
            "AGENT_RAG_API_KEY": token,
        })
        values["AGENT_DATABASE_URL"] = (
            "postgresql://agent_app:" + values["AGENT_DB_PASSWORD"] + "@postgres:5432/agentic"
        )
        clients.update(AGENT_API_KEY=requester, AGENT_APPROVER_API_KEY=approver,
                       AGENT_URL="http://127.0.0.1:8001")
    lines, replaced = [], set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0]
        if key in values:
            line = key + "=" + shlex.quote(values[key])
            replaced.add(key)
        lines.append(line)
    if replaced != set(values):
        raise ValueError("Environment template does not declare every generated setting")
    targets = [
        (directory / ".env", "\n".join(lines) + "\n", 0o600),
        (directory / "data/client.env",
         "\n".join(key + "=" + shlex.quote(value) for key, value in clients.items()) + "\n", 0o600),
        # Boolean flags contain no secrets and must be readable by container UID 10001.
        (directory / "data/config/rags.features.json", "{}\n", 0o644),
    ]
    if FULL_STACK:
        targets.append((directory / "data/config/agent.features.json", "{}\n", 0o644))
    if any(path.exists() or path.is_symlink() for path, _body, _mode in targets):
        raise FileExistsError("Existing configuration kept; reuse it or choose a new directory")
    config_dir = directory / "data/config"
    try:
        config_dir.mkdir(parents=True)
    except FileExistsError:
        if not config_dir.is_dir() or config_dir.stat().st_mode & 0o005 != 0o005:
            raise PermissionError(
                "Existing data/config must permit container reads and directory traversal; "
                "its permissions were not changed"
            ) from None
    else:
        # mkdir/open creation modes are filtered by the caller's umask.
        # Widen only this newly created nonsecret directory, never existing paths.
        config_dir.chmod(0o755)
    created = []
    try:
        for path, body, mode in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), mode)
                output.write(body)
    except BaseException:
        for path in reversed(created):
            path.unlink()
        raise
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT,
                        help="Output directory; default is this repository")
    args = parser.parse_args()
    for path in create(args.directory.resolve()):
        print("Created " + str(path))
    print("Run docker compose up -d --build --wait, then source data/client.env for API examples.")


if __name__ == "__main__":
    main()
