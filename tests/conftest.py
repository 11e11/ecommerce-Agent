from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def mcp_server_module():
    path = ROOT / "integration" / "mcp-server.py"
    spec = importlib.util.spec_from_file_location("ecommerce_mcp_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Suppress bytecode writing: a __pycache__ beside the source would be
    # swept into dist by build_dist and trip build_dist --check.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module
