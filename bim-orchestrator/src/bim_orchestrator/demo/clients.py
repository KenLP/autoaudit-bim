"""Bootstrap: import the SAME mock clients the test suite pins 1:1 against
the real MCP protocol (``tests/_mocks.py``, guarded by
``tests/test_mock_parity.py``).

``--demo`` deliberately does NOT ship its own hand-rolled fake client. The
mock in ``tests/_mocks.py`` is exercised by ~1500 tests and kept in lockstep
with ``RevitMCPClient`` / ``FormaMCPClient`` by the parity test — so reusing
it means the demo runs through EXACTLY the same code path as production
(``DesignAgent``, ``RevitQueryAgent``, the trust pipeline) with only the
transport swapped out. Writing a second mock would be a second source of
truth that could silently drift from the real protocol.

``tests/`` isn't a package on the normal import path (it's a test-only
directory, not part of the ``bim_orchestrator`` distribution), so this module
locates the repository checkout from ``__file__`` and adds it to
``sys.path`` before importing. This only works from a source checkout —
exactly the setting ``--demo`` is meant for (AU visitors cloning the repo).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests._mocks import MockFormaMCPClient, MockRevitMCPClient  # noqa: F401

_FRIENDLY_ERROR = (
    "Demo mode chạy từ source checkout: cd bim-orchestrator && "
    "uv run bim-orchestrator --demo"
)


def _locate_repo_root() -> Path:
    """Walk up from this file to the ``bim-orchestrator/`` checkout root.

    src layout: ``src/bim_orchestrator/demo/clients.py`` -> parents[3] is the
    checkout root (the dir holding ``pyproject.toml`` + ``tests/``). Verified
    against both markers so a mis-laid-out install fails loudly instead of
    silently importing the wrong ``tests`` package from elsewhere on
    ``sys.path``.
    """
    candidate = Path(__file__).resolve().parents[3]
    if not (candidate / "pyproject.toml").exists():
        raise RuntimeError(_FRIENDLY_ERROR)
    if not (candidate / "tests" / "_mocks.py").exists():
        raise RuntimeError(_FRIENDLY_ERROR)
    return candidate


def _import_mocks() -> tuple[type, type]:
    """Add the repo root to ``sys.path`` (idempotent) and import the mocks.

    Returns ``(MockRevitMCPClient, MockFormaMCPClient)`` — the exact classes
    ``tests/test_mock_parity.py`` pins against ``RevitMCPClient`` /
    ``FormaMCPClient``. Any failure (missing checkout, import error) raises
    with the friendly guidance above rather than a bare traceback.
    """
    try:
        repo_root = _locate_repo_root()
    except RuntimeError:
        raise
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    try:
        from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
    except ImportError as exc:  # pragma: no cover — defensive, see docstring
        raise RuntimeError(_FRIENDLY_ERROR) from exc
    return MockRevitMCPClient, MockFormaMCPClient


__all__ = ["_import_mocks", "_locate_repo_root"]
