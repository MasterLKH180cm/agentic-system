"""Deployment bootstrap contracts; all credentials stay in disposable files."""
import importlib.util
import json
from pathlib import Path
import shlex
import stat

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def bootstrap():
    spec = importlib.util.spec_from_file_location("compose_bootstrap", ROOT / "scripts/bootstrap_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        tokens = shlex.split(raw, comments=True)
        values[key] = tokens[0] if tokens else ""
    return values


def test_generated_credentials_are_private_distinct_and_tenant_aligned(tmp_path):
    module = bootstrap()
    module.create(tmp_path)
    env, clients = settings(tmp_path / ".env"), settings(tmp_path / "data/client.env")
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data/client.env").stat().st_mode) == 0o600
    assert json.loads(env["RAGS_KEYS_JSON"]) == {clients["RAGS_API_KEY"]: "local"}
    secrets = [env["POSTGRES_PASSWORD"], env["RAGS_DB_PASSWORD"], clients["RAGS_API_KEY"]]
    if module.FULL_STACK:
        assert json.loads(env["AGENT_KEYS_JSON"]) == {clients["AGENT_API_KEY"]: "local"}
        assert json.loads(env["AGENT_APPROVER_KEYS_JSON"]) == {clients["AGENT_APPROVER_API_KEY"]: "local"}
        assert json.loads(env["AGENT_RAG_KEYS_JSON"]) == {"local": clients["RAGS_API_KEY"]}
        secrets += [env["AGENT_DB_PASSWORD"], clients["AGENT_API_KEY"], clients["AGENT_APPROVER_API_KEY"]]
    assert len(set(secrets)) == len(secrets) and all(len(secret) >= 40 for secret in secrets)
    for path in (tmp_path / "data/config").glob("*.json"):
        assert json.loads(path.read_text()) == {}
        assert stat.S_IMODE(path.stat().st_mode) & 0o444 == 0o444


def test_existing_client_configuration_prevents_partial_bootstrap(tmp_path):
    target = tmp_path / "data/client.env"
    target.parent.mkdir()
    target.write_text("keep-existing")
    with pytest.raises(FileExistsError):
        bootstrap().create(tmp_path)
    assert target.read_text() == "keep-existing"
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "data/config").exists()


def test_symlink_target_is_never_followed_or_overwritten(tmp_path):
    original = tmp_path / "original"
    original.write_text("preserved")
    (tmp_path / ".env").symlink_to(original)
    with pytest.raises(FileExistsError):
        bootstrap().create(tmp_path)
    assert original.read_text() == "preserved"


def test_failed_bootstrap_removes_only_new_files(tmp_path, monkeypatch):
    module = bootstrap()
    original = module.os.open

    def fail_flag_file(path, flags, mode):
        if Path(path).suffix == ".json":
            raise OSError("controlled disk failure")
        return original(path, flags, mode)

    monkeypatch.setattr(module.os, "open", fail_flag_file)
    with pytest.raises(OSError, match="controlled disk failure"):
        module.create(tmp_path)
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "data/client.env").exists()
