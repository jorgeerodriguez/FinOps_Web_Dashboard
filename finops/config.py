from __future__ import annotations
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def config_path() -> Path:
    """config.toml next to the app, unless FINOPS_CONFIG points elsewhere (used by the test suite so it never touches real data)."""
    return Path(os.environ["FINOPS_CONFIG"]) if os.environ.get("FINOPS_CONFIG") else ROOT / "config.toml"


def load_config() -> dict:
    with open(config_path(), "rb") as f:
        return tomllib.load(f)


def resolve(path: str) -> Path:
    return (ROOT / path).resolve()          # an absolute path stays absolute
