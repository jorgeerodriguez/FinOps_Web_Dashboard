from __future__ import annotations
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def resolve(path: str) -> Path:
    return (ROOT / path).resolve()
