"""M-04/M-03 — a failed stdio handshake must not leak, and must not hang.

Both stdio clients entered `stdio_client(...)` (which SPAWNS the server
process) into an `AsyncExitStack`, then awaited `initialize()` — with nothing
between them and a failure. Two consequences, both invisible in a CLI run
where process exit cleans up regardless, and both real under the AuditHub
service, which stays up for weeks:

  * `__aenter__` raising means `__aexit__` never runs, so the stack is never
    closed: the child process and its anyio reader tasks leak, one per failed
    attempt (a forma-mcp.exe that spawns but dies on a missing .env does this
    on every POST /audits).
  * a server that spawns and then says nothing at all leaves `initialize()`
    awaiting forever — the job never finishes, never releases the single-run
    lock, and every later audit request 409s.

`mcp_clients/lod_validator.py` had the cleanup half but NOT the timeout
ceiling, and its cleanup ran only on `Exception`, not `BaseException` — found
by review round 7 (D-1, 2026-08-16): a mute satellite venv would hang the
nightly `--audit` forever with no outer timeout to save it. These tests now
pin all three stdio clients (spatial_qc inherits `_StdioVenvClient`, so the
lod tests cover it too).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
from bim_orchestrator.mcp_clients.lod_validator import AuditAxisError, LODValidatorClient
from bim_orchestrator.mcp_clients.revit import RevitMCPClient, RevitMCPConfig


class _SpyStdio:
    """Stands in for `stdio_client(params)`: records whether the context it
    handed out was ever exited (i.e. whether the spawned process was reaped)."""

    def __init__(self, tracker: dict[str, Any]) -> None:
        self._tracker = tracker

    async def __aenter__(self) -> tuple[object, object]:
        self._tracker["entered"] = True
        return (object(), object())

    async def __aexit__(self, *exc: object) -> None:
        self._tracker["exited"] = True


class _Session:
    """A ClientSession whose `initialize()` fails, or never returns."""

    def __init__(self, *, mode: str) -> None:
        self._mode = mode

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> None:
        if self._mode == "raise":
            raise RuntimeError("server died during handshake")
        await asyncio.Event().wait()  # mute server: never resolves


def _patch(monkeypatch: pytest.MonkeyPatch, module: str, tracker: dict[str, Any],
           *, mode: str) -> None:
    monkeypatch.setattr(
        f"bim_orchestrator.mcp_clients.{module}.stdio_client",
        lambda params: _SpyStdio(tracker),
    )
    monkeypatch.setattr(
        f"bim_orchestrator.mcp_clients.{module}.ClientSession",
        lambda read, write: _Session(mode=mode),
    )


def _forma() -> FormaMCPClient:
    return FormaMCPClient(
        FormaMCPConfig(command="forma-mcp.exe", args=[], cwd=None, env={})
    )


def _revit() -> RevitMCPClient:
    return RevitMCPClient(
        RevitMCPConfig(command="node", args=["server.js"], cwd=None)
    )


def _lod() -> LODValidatorClient:
    return LODValidatorClient(python_exe="python", cwd=".")


class TestAFailedHandshakeIsCleanedUp:
    async def test_forma_closes_the_stack_when_initialize_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "forma", tracker, mode="raise")

        with pytest.raises(RuntimeError):
            async with _forma():
                pass  # pragma: no cover — never reached

        assert tracker.get("entered") is True, "test did not exercise the spawn"
        assert tracker.get("exited") is True, (
            "the spawned server was never reaped — one leaked process per "
            "failed connect attempt"
        )

    async def test_revit_closes_the_stack_when_initialize_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "revit", tracker, mode="raise")

        with pytest.raises(RuntimeError):
            async with _revit():
                pass  # pragma: no cover — never reached

        assert tracker.get("exited") is True


class TestAMuteServerTimesOutInsteadOfHanging:
    async def test_forma_handshake_has_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "forma", tracker, mode="hang")
        monkeypatch.setattr(
            "bim_orchestrator.mcp_clients.forma._HANDSHAKE_TIMEOUT_S", 0.05
        )

        # `asyncio.wait_for` raises TimeoutError (TimeoutError IS
        # asyncio.TimeoutError on 3.11+). Without the ceiling this test would
        # hang the suite rather than fail it — which is the production symptom.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_forma().__aenter__(), timeout=5.0)

        assert tracker.get("exited") is True, (
            "a timed-out handshake must still reap the spawned process"
        )

    async def test_revit_handshake_has_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "revit", tracker, mode="hang")
        monkeypatch.setattr(
            "bim_orchestrator.mcp_clients.revit._HANDSHAKE_TIMEOUT_S", 0.05
        )

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_revit().__aenter__(), timeout=5.0)

        assert tracker.get("exited") is True

    async def test_lod_handshake_has_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D-1: the mute-satellite case. The internal ceiling fires as a
        # TimeoutError (an Exception) → converted to the axis-scoped
        # AuditAxisError, so the audit degrades that ONE axis instead of
        # hanging the whole nightly.
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "lod_validator", tracker, mode="hang")
        monkeypatch.setattr(
            "bim_orchestrator.mcp_clients.lod_validator._HANDSHAKE_TIMEOUT_S", 0.05
        )

        with pytest.raises(AuditAxisError):
            await asyncio.wait_for(_lod().__aenter__(), timeout=5.0)

        assert tracker.get("exited") is True, (
            "a timed-out satellite handshake must still reap the spawned venv"
        )


class TestLodCancellationReapsTheVenv:
    async def test_cancellation_during_handshake_closes_the_stack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D-1, second half: cancellation is a BaseException — the old handler
        # (`except Exception`) let it fly past WITHOUT closing the stack, so a
        # cancelled audit leaked the satellite process. It must clean up AND
        # re-raise CancelledError raw (a cancel is not an axis failure).
        tracker: dict[str, Any] = {}
        _patch(monkeypatch, "lod_validator", tracker, mode="hang")

        task = asyncio.ensure_future(_lod().__aenter__())
        await asyncio.sleep(0.05)  # let it reach the hanging initialize()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert tracker.get("exited") is True, (
            "a cancelled handshake must still reap the spawned venv"
        )
