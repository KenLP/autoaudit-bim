"""M10 mock parity guardrail.

Walks the public async surface of the REAL MCP clients (``RevitHTTPClient``
— the transport `make_revit_client()` picks in production, per
``docs/PRODUCTION_PACKAGING.md`` — and ``FormaMCPClient``) via ``inspect`` and
asserts every public method also exists on the matching test double in
``tests/_mocks.py``, with a signature that's at least as permissive (same
required positional/keyword param names; the mock may add extra params with
defaults).

This exists so a future "add a client method, forget the mock" drift (the
M10 finding: ``list_families(limit=...)`` / ``list_issues(assigned_to=...)``
silently accepted-then-dropped) fails CI instead of surfacing at runtime in
whatever test happens to exercise the new method first.

Deliberately NOT bidirectional: mock-only test helpers (``calls_to``,
``execute_calls``, ``make_test_ruleset``, dunder methods) are not required to
exist on the real client.
"""

from __future__ import annotations

import inspect

import pytest

from bim_orchestrator.mcp_clients.forma import FormaMCPClient
from bim_orchestrator.mcp_clients.lod_validator import LODValidatorClient
from bim_orchestrator.mcp_clients.revit import RevitHTTPClient
from bim_orchestrator.mcp_clients.spatial_qc import SpatialQCClient

from tests._mocks import (
    MockFormaMCPClient,
    MockLODValidatorClient,
    MockRevitMCPClient,
    MockSpatialQCClient,
)

# Methods that exist on the real client but are pure-transport or pure-compute
# helpers the in-memory mock legitimately doesn't need to mirror 1:1. Each
# entry documents WHY it's exempt so the list can't silently grow.
REVIT_ALLOWLIST: dict[str, str] = {
    # Envelope-unwrapping plumbing — the mock's own call_envelope/call_data
    # already provide an equivalent low-level interface; every real wrapper
    # method routes through it, so there's nothing extra to parity-check.
    "call_envelope": "low-level transport primitive, mock provides its own",
    "call_data": "low-level transport primitive, mock provides its own",
}

FORMA_ALLOWLIST: dict[str, str] = {
    # Async context manager protocol — the mock implements these directly
    # (returns self / no-op) rather than mirroring signatures via inspect.
    "__aenter__": "context-manager protocol, mock has its own no-op impl",
    "__aexit__": "context-manager protocol, mock has its own no-op impl",
    # Pure client-side computation over an already-fetched `elements` list —
    # never calls `self.call`/`self.call_structured`, so it isn't part of the
    # MCP protocol surface the mock needs to simulate. Exercised directly
    # against the real FormaMCPClient in test_forma_linked_documents.py.
    "get_object_id_map": "pure helper over caller-supplied data, no MCP call",
}


def _public_methods(cls: type) -> dict[str, inspect.Signature]:
    out: dict[str, inspect.Signature] = {}
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        out[name] = inspect.signature(member)
    return out


def _all_methods(cls: type) -> set[str]:
    """Every function member, including dunders — used only to validate that
    allowlist entries (which may legitimately name a dunder) still exist."""
    return {
        name for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
    }


def _param_names(sig: inspect.Signature, *, required_only: bool) -> list[str]:
    names = []
    for pname, p in sig.parameters.items():
        if pname == "self":
            continue
        if required_only and p.default is not inspect.Parameter.empty:
            continue
        names.append(pname)
    return names


def _assert_signature_compatible(
    method_name: str, real_sig: inspect.Signature, mock_sig: inspect.Signature
) -> None:
    """Mock must accept every required param the real method requires (by
    name), and may add extra params as long as they carry a default (so
    existing callers using positional/keyword real-shaped calls still work
    against the mock)."""
    real_required = set(_param_names(real_sig, required_only=True))
    mock_all = set(_param_names(mock_sig, required_only=False))
    missing = real_required - mock_all
    assert not missing, (
        f"{method_name}: mock is missing required param(s) {sorted(missing)} "
        f"present on the real client (real sig: {real_sig}, mock sig: {mock_sig})"
    )

    mock_required = set(_param_names(mock_sig, required_only=True))
    real_all = set(_param_names(real_sig, required_only=False))
    extra_required = mock_required - real_all
    assert not extra_required, (
        f"{method_name}: mock requires param(s) {sorted(extra_required)} that "
        f"aren't optional-or-present on the real client — a caller using the "
        f"real client's call shape would fail against the mock "
        f"(real sig: {real_sig}, mock sig: {mock_sig})"
    )


class TestRevitMockParity:
    real_methods = _public_methods(RevitHTTPClient)
    mock_methods = _public_methods(MockRevitMCPClient)

    @pytest.mark.parametrize("name", sorted(real_methods.keys()))
    def test_real_method_exists_on_mock(self, name: str) -> None:
        if name in REVIT_ALLOWLIST:
            pytest.skip(REVIT_ALLOWLIST[name])
        assert name in self.mock_methods, (
            f"RevitHTTPClient.{name} has no counterpart on MockRevitMCPClient "
            f"— either implement it on the mock or add it to REVIT_ALLOWLIST "
            f"with a reason."
        )
        _assert_signature_compatible(
            name, self.real_methods[name], self.mock_methods[name]
        )

    def test_allowlist_entries_are_still_real_methods(self) -> None:
        """Catch a stale allowlist entry (method renamed/removed upstream)."""
        stale = set(REVIT_ALLOWLIST) - _all_methods(RevitHTTPClient)
        assert not stale, f"REVIT_ALLOWLIST has stale entries: {sorted(stale)}"


class TestLODValidatorMockParity:
    """P3-1: same guardrail for the lod-validator satellite client."""

    real_methods = _public_methods(LODValidatorClient)
    mock_methods = _public_methods(MockLODValidatorClient)

    @pytest.mark.parametrize("name", sorted(real_methods.keys()))
    def test_real_method_exists_on_mock(self, name: str) -> None:
        assert name in self.mock_methods, (
            f"LODValidatorClient.{name} has no counterpart on "
            f"MockLODValidatorClient — implement it on the mock."
        )
        _assert_signature_compatible(
            name, self.real_methods[name], self.mock_methods[name]
        )


class TestSpatialQCMockParity:
    """P3-1: same guardrail for the spatial-qc satellite client."""

    real_methods = _public_methods(SpatialQCClient)
    mock_methods = _public_methods(MockSpatialQCClient)

    @pytest.mark.parametrize("name", sorted(real_methods.keys()))
    def test_real_method_exists_on_mock(self, name: str) -> None:
        assert name in self.mock_methods, (
            f"SpatialQCClient.{name} has no counterpart on "
            f"MockSpatialQCClient — implement it on the mock."
        )
        _assert_signature_compatible(
            name, self.real_methods[name], self.mock_methods[name]
        )


class TestFormaMockParity:
    real_methods = _public_methods(FormaMCPClient)
    mock_methods = _public_methods(MockFormaMCPClient)

    @pytest.mark.parametrize("name", sorted(real_methods.keys()))
    def test_real_method_exists_on_mock(self, name: str) -> None:
        if name in FORMA_ALLOWLIST:
            pytest.skip(FORMA_ALLOWLIST[name])
        assert name in self.mock_methods, (
            f"FormaMCPClient.{name} has no counterpart on MockFormaMCPClient "
            f"— either implement it on the mock or add it to FORMA_ALLOWLIST "
            f"with a reason."
        )
        _assert_signature_compatible(
            name, self.real_methods[name], self.mock_methods[name]
        )

    def test_allowlist_entries_are_still_real_methods(self) -> None:
        stale = set(FORMA_ALLOWLIST) - _all_methods(FormaMCPClient)
        assert not stale, f"FORMA_ALLOWLIST has stale entries: {sorted(stale)}"
