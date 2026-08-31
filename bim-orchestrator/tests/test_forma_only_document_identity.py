"""SPEC_DOCUMENT_IDENTITY_STAMP, integration level (v1.7-3bP3 follow-up).

The unit tests in test_run_recorder.py cover ``write_metadata`` and
test_audit_cli.py covers ``_fetch_document_identity`` in isolation. What was
missing (2026-07 session review): proof that the Forma-only entry points
``check()`` / ``run()`` — which have NO Revit client and therefore never call
``_fetch_document_identity`` — still land ``"document": null`` (present, not
omitted) in the run folder's metadata.json.

The ``run()`` test doubles as the regression net for the delta-report status
gate: a graph run finishes with status "converged" (not "completed"), and the
delta hook must fire for it (post-ship fix of v1.7-3bP1's ``completed``-only
gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bim_orchestrator.orchestrator as orchestrator
from tests._mocks import MockFormaMCPClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PARAM_RULES = REPO_ROOT / "config" / "rules.parameter_completeness.yaml"
AUTONOMY = REPO_ROOT / "config" / "autonomy.yaml"


@pytest.fixture()
def forma_only_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Mock the Forma boundary + isolate the runs dir; no Revit anywhere."""
    monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        orchestrator, "FormaMCPClient", lambda config: MockFormaMCPClient()
    )
    monkeypatch.setenv("DEMO_ELEMENT_GROUP_ID", "eg-test")
    monkeypatch.setenv("DEMO_PROJECT_ID", "b.test-project")
    return tmp_path


def _metadata(folder_holder: list) -> dict:
    assert folder_holder, "on_folder hook never fired"
    meta_path = folder_holder[0].root / "metadata.json"
    return json.loads(meta_path.read_text(encoding="utf-8")), folder_holder[0].root


@pytest.mark.asyncio
async def test_check_forma_only_metadata_document_is_null(
    forma_only_env: Path,
) -> None:
    holder: list = []
    rc = await orchestrator.check(
        PARAM_RULES,
        AUTONOMY,
        forma_only_env / "findings.json",
        on_folder=holder.append,
    )
    assert rc == 0
    meta, _root = _metadata(holder)
    assert meta["status"] == "completed"
    assert "document" in meta  # key present, never omitted
    assert meta["document"] is None  # Forma-only -> no Revit identity


@pytest.mark.asyncio
async def test_run_forma_only_metadata_document_is_null_and_delta_written(
    forma_only_env: Path,
) -> None:
    holder: list = []
    rc = await orchestrator.run(
        PARAM_RULES,
        AUTONOMY,
        forma_only_env / "findings.json",
        limit=5,
        rule_filter=None,
        dry_run_only=True,
        published=False,
        issue_subtype_id="subtype-quality",
        max_iterations=2,
        checkpoint_dir=forma_only_env / "checkpoints",
        on_folder=holder.append,
    )
    assert rc == 0
    meta, root = _metadata(holder)
    assert meta["status"] == "converged"  # graph modes never say "completed"
    assert "document" in meta
    assert meta["document"] is None
    # Regression: the delta hook must fire on "converged" too (the
    # completed-only gate made delta.md unreachable from --run/--run-revit).
    assert (root / "delta.md").exists()
    delta = json.loads((root / "delta.json").read_text(encoding="utf-8"))
    assert delta["document"] is None
    assert delta["baseline_run_id"] is None  # first comparable run
