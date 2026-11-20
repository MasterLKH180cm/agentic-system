"""Set a safe test environment before importing the module-level app."""
import os

os.environ.setdefault("AGENT_ENV", "test")
