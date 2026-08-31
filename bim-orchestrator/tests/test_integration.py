"""End-to-end integration tests with mock MCP.

These tests exercise the full LangGraph (Query → QC → Route → Design → Bump)
against `MockFormaMCPClient` — no real ACC, no LLM. They verify that the
production code path (real agents + real graph + real rules YAML) actually
works wired together, just with the network boundary mocked out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.grounding import GroundingAgent
from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.query import QueryAgent
from bim_orchestrator.graph import build_graph
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.rag.store import VectorStore
from bim_orchestrator.state import OrchestratorState
from bim_orchestrator.rag.fixtures import ingest_ibc_chapter_7
from tests._mocks import SAMPLE_WALLS, MockFormaMCPClient
from tests.test_grounding_agent import fake_embed as grounding_fake_embed

REPO_ROOT = Path(__file__).resolve().parents[1]
PARAM_RULES = REPO_ROOT / "config" / "rules.parameter_completeness.yaml"
NAMING_RULES = REPO_ROOT / "config" / "rules.naming.yaml"
FIRE_RATING_RULES = REPO_ROOT / "config" / "rules.fire_rating.yaml"
AUTONOMY = REPO_ROOT / "config" / "autonomy.yaml"


def _initial_state(max_iterations: int = 3) -> OrchestratorState:
    return {
        "project_id": "b.test-project",
        "iteration": 0,
        "max_iterations": max_iterations,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


@pytest.mark.asyncio
async def test_full_graph_param_completeness_converges_phase1_pattern():
    """The full Phase 1 scenario: AECDM read-only ≡ findings unchanged ≡ converge after 1 iteration.

    v1 task BB: switched filter from room.department.required (now routed to
    missing_data) to room.number.format (still non_compliant). The trust
    pipeline behavior is identical; we just exercise a rule that emits
    non_compliant findings under the new 4-state outcomes schema.
    """
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=2, rule_filter="room.number.format",
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=5))

    # Converged because findings didn't decrease (Phase 1 pattern)
    assert result["status"] == "converged"
    assert result["iteration"] == 1  # bumped once after iteration 0
    # SAMPLE_ROOMS: 3 rooms with Numbers "11A", "07", "01" — all fail \d{3}
    # plus 6 missing-data items (Dept+Occupancy on each room) routed elsewhere
    assert len(result["findings"]) >= 3  # at least the 3 number.format violations
    assert result["outcomes_summary"]["missing_data"] >= 6
    # v1.4-K18: 3 number.format violations (one rule) → ONE grouped ACC issue
    executed = [f for f in result["proposed_fixes"] if f["executed"]]
    assert len(executed) == 1
    # Verify each executed fix has an issue_id
    for fix in executed:
        issue = (fix.get("preview") or {}).get("executed_issue") or {}
        assert issue.get("id", "").startswith("issue-mock-")


@pytest.mark.asyncio
async def test_full_graph_call_sequence_matches_trust_pipeline():
    """Verify the exact MCP call sequence matches the trust pipeline contract."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=2, rule_filter="room.number.format",
    )
    app = build_graph(query, qc, design)
    await app.ainvoke(_initial_state())

    call_names = [name for name, _ in mcp.calls]
    # Expected sequence (v1.4-K18 — 3 same-rule violations → ONE grouped issue):
    #   - 2× aecdm_query_elements (iter 0 + iter 1 verification)
    #   - 1× issues_list_types (subtype discovery, once)
    #   - 2× create_issue (1 grouped issue × [dry-run + execute])
    assert call_names.count("aecdm_query_elements") == 2
    assert call_names.count("issues_list_types") == 1
    assert call_names.count("create_issue") == 2

    # The execute call must come AFTER a preview call (trust pipeline)
    create_calls = [args for name, args in mcp.calls if name == "create_issue"]
    assert create_calls[0]["dry_run"] is True
    assert create_calls[1]["dry_run"] is False
    assert create_calls[1]["approval_token"] is not None


@pytest.mark.asyncio
async def test_full_graph_writes_checkpoints(tmp_path):
    """Graph should write JSON checkpoints to checkpoint_dir between iterations."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1, rule_filter="room.department.required",
    )
    app = build_graph(query, qc, design, checkpoint_dir=tmp_path)
    await app.ainvoke(_initial_state())

    json_files = list(tmp_path.rglob("iteration_*.json"))
    assert len(json_files) >= 1
    import json
    cp = json.loads(json_files[0].read_text())
    assert "findings" in cp
    # v1.5-R6 (3.4 hygiene): `elements` (and `check_trace`) are dropped from
    # the checkpoint payload — they dwarf everything else and are rebuilt
    # fresh by the query/QC agents every iteration, so they add nothing a
    # resume needs.
    assert "elements" not in cp
    assert "check_trace" not in cp
    assert "proposed_fixes" in cp
    assert cp["iteration"] == 0  # checkpoint of iteration 0's final state


@pytest.mark.asyncio
async def test_full_graph_dry_run_creates_zero_issues():
    """With dry_run_only=True, the trust pipeline previews but never executes."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=2, rule_filter="room.number.format",
        dry_run_only=True,
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    assert result["status"] == "converged"
    executed = [f for f in result["proposed_fixes"] if f["executed"]]
    assert len(executed) == 0
    # Pending fixes should have approval tokens (preview ran). v1.4-K18: the 3
    # same-rule violations preview as ONE grouped issue.
    pending = [f for f in result["proposed_fixes"] if not f["executed"]]
    assert len(pending) == 1
    assert all(f["approval_token"] is not None for f in pending)
    # Mock should have NO execute calls
    assert len(mcp.execute_calls()) == 0


@pytest.mark.asyncio
async def test_full_graph_naming_scenario_swaps_rules_only():
    """Same Query/QC/Design with naming rules → different findings, no code changes."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=NAMING_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1,
        rule_filter="room.number.strict_three_digit",
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    # Sample data: 3 rooms with Numbers "11A", "07", "01" — all fail \d{3}
    assert result["status"] == "converged"
    number_findings = [f for f in result["findings"] if f["rule_id"] == "room.number.strict_three_digit"]
    assert len(number_findings) == 3
    # 1 issue created (limit=1)
    assert len([f for f in result["proposed_fixes"] if f["executed"]]) == 1


@pytest.mark.asyncio
async def test_full_graph_with_grounding_enriches_findings(tmp_path):
    """Phase 2: graph with GroundingAgent attaches citation strings to findings."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1, rule_filter="room.department.required",
    )

    # Seed a vector store with realistic BEP / IBC chunks for the QC rules we use
    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="bep-test",
        embed_fn=grounding_fake_embed,
    )
    store.ingest_text(
        "Every room shall declare a department for cost allocation. "
        "Occupancy type drives egress and ventilation calculations.",
        source="BEP.pdf", section="§4.2", page=12,
    )
    store.ingest_text(
        "Area parameters on rooms must be positive — non-positive area "
        "indicates a placeholder room awaiting geometric definition.",
        source="BEP.pdf", section="§4.3", page=13,
    )
    store.ingest_text(
        "Room numbering follows the firm standard 2024: a single capital "
        "letter optional, followed by three digits, optional trailing letter.",
        source="BEP.pdf", section="§5.1", page=15,
    )

    grounding = GroundingAgent(store=store, top_k=2, min_score=0.0)

    app = build_graph(query, qc, design, grounding_agent=grounding)
    result = await app.ainvoke(_initial_state(max_iterations=5))

    assert result["status"] == "converged"
    # At least the Department + Occupancy findings should carry a BEP citation
    cited = [f for f in result["findings"] if f.get("citation")]
    assert len(cited) > 0
    # All citations reference BEP.pdf (the only source in the store)
    for f in cited:
        assert "BEP.pdf" in (f["citation"] or "")


@pytest.mark.asyncio
async def test_full_graph_grounding_none_is_phase1_behavior(tmp_path):
    """Backward compat: grounding_agent=None should leave every citation untouched."""
    mcp = MockFormaMCPClient()
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1, rule_filter="room.department.required",
    )

    app = build_graph(query, qc, design, grounding_agent=None)
    result = await app.ainvoke(_initial_state())

    assert result["status"] == "converged"
    # No grounding ran → all citations remain None
    assert all(f.get("citation") is None for f in result["findings"])


@pytest.mark.asyncio
async def test_fire_rating_e2e_with_ibc_grounding(tmp_path):
    """Phase 2 W4 D4: full grounded pipeline using real rules.fire_rating.yaml + IBC fixture.

    Verifies the killer Phase 2 demo path:
        Walls → QC(fire_rating.yaml) → Grounding(IBC §711.x) → Findings cite IBC
    """
    # MCP returns 5 walls (mix of valid/missing/wrong FireRating)
    mcp = MockFormaMCPClient(
        elements_by_category={"Walls": list(SAMPLE_WALLS)},
    )
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=FIRE_RATING_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-walls", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1, rule_filter="wall.fire_rating.required",
    )

    # Seed IBC §7 fixture into store (source="IBC.txt" matches rule's source_filter)
    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="ibc-fire-rating",
        embed_fn=grounding_fake_embed,
    )
    chunks = ingest_ibc_chapter_7(store)
    assert chunks >= 8, "IBC fixture should ingest ≥8 chunks"

    grounding = GroundingAgent(store=store, rules=qc.rules, top_k=2, min_score=0.0)

    app = build_graph(query, qc, design, grounding_agent=grounding)
    result = await app.ainvoke(_initial_state())

    assert result["status"] == "converged"
    # v1 task BB: present_and_nonempty rules now route to missing_data_items.
    # Combine all 3 buckets to inspect rule coverage; grounding enriches all of them.
    all_findings = (
        list(result.get("findings", []))
        + list(result.get("manual_review_items", []))
        + list(result.get("missing_data_items", []))
    )
    assert len(all_findings) > 0

    # Findings from the 3 fire_rating rules:
    #   - wall.fire_rating.required (hard, source_filter=[BEP.pdf, IBC.pdf]) -> missing_data
    #   - wall.fire_rating.format   (hard, source_filter=[BEP.pdf])          -> non_compliant
    #   - wall.assembly_type.present (soft, no source_filter)                -> missing_data
    by_rule: dict[str, list] = {}
    for f in all_findings:
        by_rule.setdefault(f["rule_id"], []).append(f)

    assert "wall.fire_rating.required" in by_rule  # 2 missing walls (101, 201)
    assert "wall.assembly_type.present" in by_rule  # 1 missing (201)
    # Note: wall.fire_rating.format hard-mode source_filter is [BEP.pdf] only.
    # Our store has IBC.txt, not BEP.pdf, so those findings get citation_missing=True

    # Required rule: hard mode → IBC.txt not in source_filter → citation_missing
    # (the rule says [BEP.pdf, IBC.pdf] not [IBC.txt])
    required_findings = by_rule["wall.fire_rating.required"]
    for f in required_findings:
        # Hard rule, no matching source → flagged missing
        assert f.get("citation_missing") is True
        assert f["citation"] is None

    # Soft rule: no citation_missing flag regardless of result
    assembly_findings = by_rule["wall.assembly_type.present"]
    for f in assembly_findings:
        # Soft mode never sets citation_missing
        assert "citation_missing" not in f or f.get("citation_missing") is False


@pytest.mark.asyncio
async def test_fire_rating_with_ibc_source_in_store_cites_correctly(tmp_path):
    """If we add IBC.pdf as a source (matching the rule's filter), citations appear."""
    mcp = MockFormaMCPClient(elements_by_category={"Walls": list(SAMPLE_WALLS)})
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=FIRE_RATING_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-walls", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=0, rule_filter="wall.fire_rating.required",  # no execute
        dry_run_only=True,
    )

    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="ibc-pdf-source",
        embed_fn=grounding_fake_embed,
    )
    # Ingest IBC fixture but tag source as "IBC.pdf" (matches rule's source_filter)
    ingest_ibc_chapter_7(store, source="IBC.pdf")

    grounding = GroundingAgent(store=store, rules=qc.rules, top_k=2, min_score=0.0)

    app = build_graph(query, qc, design, grounding_agent=grounding)
    result = await app.ainvoke(_initial_state())

    # v1 task BB: wall.fire_rating.required is present_and_nonempty -> missing_data
    all_findings = (
        list(result.get("findings", []))
        + list(result.get("manual_review_items", []))
        + list(result.get("missing_data_items", []))
    )
    required = [f for f in all_findings if f["rule_id"] == "wall.fire_rating.required"]
    assert len(required) > 0
    cited = [f for f in required if f.get("citation")]
    # At least some required findings should now carry IBC.pdf citations
    assert len(cited) > 0
    for f in cited:
        assert "IBC.pdf" in (f["citation"] or "")
        assert f.get("citation_missing") is False


@pytest.mark.asyncio
async def test_source_filter_excludes_bep_when_rule_says_ibc_only(tmp_path):
    """Rule with source_filter=[BEP.pdf] must NEVER cite an IBC.txt chunk even if relevant."""
    mcp = MockFormaMCPClient(elements_by_category={"Walls": list(SAMPLE_WALLS)})
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=FIRE_RATING_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=0,
        rule_filter="wall.fire_rating.format", dry_run_only=True,
    )

    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="mixed-sources",
        embed_fn=grounding_fake_embed,
    )
    # Add IBC.txt content (relevant to fire ratings) and BEP.pdf (off-topic)
    ingest_ibc_chapter_7(store, source="IBC.txt")
    store.ingest_text(
        "Door hardware specifications.", source="BEP.pdf",
        section="§6", page=22,
    )

    grounding = GroundingAgent(store=store, rules=qc.rules, top_k=3, min_score=0.0)
    app = build_graph(query, qc, design, grounding_agent=grounding)
    result = await app.ainvoke(_initial_state())

    # wall.fire_rating.format has source_filter=[BEP.pdf] only
    format_findings = [f for f in result["findings"] if f["rule_id"] == "wall.fire_rating.format"]
    for f in format_findings:
        # Either cited from BEP.pdf only OR missing (NEVER IBC.txt)
        citation = f.get("citation")
        if citation is not None:
            assert "IBC.txt" not in citation
            assert "BEP.pdf" in citation


@pytest.mark.asyncio
async def test_full_graph_handles_inactive_subtypes():
    """Mock has 2 inactive subtypes first — DesignAgent must skip them."""
    mcp = MockFormaMCPClient()  # SAMPLE_SUBTYPES: 2 inactive Design, 2 active Quality+General
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=PARAM_RULES, autonomy=autonomy)
    query = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=qc.rules)
    design = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test-project",
        max_issues=1, rule_filter="room.number.format",
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    # If DesignAgent picked the inactive Design subtype, the mock's create_issue
    # would raise. Reaching converged means it picked Quality (active, preferred).
    assert result["status"] == "converged"
    executed = [f for f in result["proposed_fixes"] if f["executed"]]
    assert len(executed) == 1
    # Verify it used the Quality subtype id (preference)
    create_calls = mcp.calls_to("create_issue")
    assert all(c["issue_subtype_id"] == "subtype-quality" for c in create_calls)
