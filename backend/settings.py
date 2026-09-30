"""Integration settings from the environment, falling back to the gitignored .env.local."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def setting(name: str) -> str | None:
    """Read a setting from the environment, falling back to .env.local.

    A variable that is present but empty counts as unset and is not looked up in the file.
    """
    if name in os.environ:
        return os.environ[name].strip() or None
    env_file = PROJECT_ROOT / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip("'\"")
    return None
