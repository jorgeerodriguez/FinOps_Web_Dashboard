"""Every test runs against a synthetic data folder (FINOPS_CONFIG), never the real config.toml / data/."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import synth                                  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _env(tmp_path_factory):
    cfg = synth.make_env(tmp_path_factory.mktemp("synth"))
    os.environ["FINOPS_CONFIG"] = str(cfg)
    yield cfg
    os.environ.pop("FINOPS_CONFIG", None)


@pytest.fixture(autouse=True)
def _clear_caches():
    import streamlit as st
    st.cache_data.clear()
    yield


@pytest.fixture
def billing():
    return synth.make_billing()


@pytest.fixture
def cloud(billing):
    b = billing[~billing.is_partial]
    return b[b.category == "Cloud Services"]


@pytest.fixture
def empty_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_CONFIG", str(synth.make_env(tmp_path, with_data=False)))
    return tmp_path
