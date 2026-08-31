"""Unit tests for GeometricQueryAgent.

Tests cover:
- Basic clearance_min rule → findings mapping
- Severity classification (high / medium / low)
- Link discovery via revit_get_linked_files (auto-detect + explicit id)
- clearance_max rule
- Unsupported check_type warning (no findings, no crash)
- check_clearance failure → empty findings (no exception propagated)
- Batching: same-key clearance_min rules → 1 revit_check_clearance call
- Dedup: multiple findings for same element → 1 merged Finding
- view_id auto-resolution: active 3D view → named keyword → first 3D → None
"""

from __future__ import annotations

import asyncio

import pytest

from bim_orchestrator.agents.geometry_query import GeometricQueryAgent
from bim_orchestrator.policies.rules_schema import GeometryRule, RuleSet
from tests._mocks import MockRevitMCPClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clearance_rule(
    rule_id: str = "ducts.floor_clearance",
    check_type: str = "clearance_min",
    threshold_mm: float = 2400.0,
    direction: str = "below",
    reference_source: str = "linked_arch",
    reference_category: str = "Floors",
    reference_link_hint: str | None = None,
) -> GeometryRule:
    return GeometryRule(
        id=rule_id,
        category="Ducts",
        check_type=check_type,  # type: ignore[arg-type]
        description="Duct clearance to floor slab",
        threshold_mm=threshold_mm,
        clearance_direction=direction,  # type: ignore[arg-type]
        reference_category=reference_category,
        reference_source=reference_source,  # type: ignore[arg-type]
        reference_link_hint=reference_link_hint,
    )


# Link names as loaded live on R27 (Snowdon Towers sample): MEP models are
# named by discipline, NOT "MEP".
_R27_LINKS = [
    {"id": 1001, "name": "Snowdon Towers Sample HVAC.rvt"},
    {"id": 1002, "name": "Snowdon Towers Sample Plumbing.rvt"},
    {"id": 1003, "name": "Snowdon Towers Sample Electrical.rvt"},
    {"id": 1004, "name": "Snowdon Towers Sample Structural.rvt"},
    {"id": 1005, "name": "Snowdon Towers Sample Site.rvt"},
]


def _clash(element_id: int, clearance_mm: float, element_name: str = "", ref_name: str = "") -> dict:
    """Build a clash dict matching the revit_check_clearance response shape."""
    clash: dict = {
        "elementA": {"id": element_id, "name": element_name, "category": "Ducts", "source": "host"},
        "elementB": {"id": 9000, "name": ref_name, "category": "Floors", "source": "link"},
        "type": "clearance_violation",
        "clearanceActualMm": clearance_mm,
    }
    return clash


def _bbox_clash(element_id: int, element_name: str = "", ref_name: str = "") -> dict:
    """A clash as ``RunBboxClash`` emits it — a bare pair, NO measured distance.

    ``MakeClashResult`` writes elementA/elementB/type only; ``clearanceActualMm``
    is set exclusively by ``RunRaycastClash``. Fixtures that hand bbox rows a
    distance are the reason the bbox-refilter bug hid: the Python refilter
    looked like it worked.
    """
    return {
        "elementA": {"id": element_id, "name": element_name,
                     "category": "Ducts", "source": "host"},
        "elementB": {"id": 9000, "name": ref_name,
                     "category": "Floors", "source": "link"},
        "type": "clearance_violation",
    }


# ---------------------------------------------------------------------------
# Basic mapping — clearance_min
# ---------------------------------------------------------------------------


class TestBasicMapping:
    @pytest.mark.asyncio
    async def test_single_violation_becomes_finding(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1384728, 47.6, element_name="Duct A", ref_name="Floor 1")],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert len(findings) == 1
        f = findings[0]
        assert f["rule_id"] == "ducts.floor_clearance"
        assert f["element_id"] == "1384728"
        assert f["element_name"] == "Duct A"
        assert f["parameter"] == "clearance_mm"
        assert f["severity_tag"] == "geometric_violation"
        assert f["status"] == "non_compliant"
        assert "47.6" in f["message"]
        assert "2400" in f["message"]

    @pytest.mark.asyncio
    async def test_no_violations_returns_empty(self):
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert findings == []

    @pytest.mark.asyncio
    async def test_multiple_violations(self):
        violations = [
            _clash(100, 10.0, "Duct 1"),
            _clash(101, 1500.0, "Duct 2"),
            _clash(102, 2350.0, "Duct 3"),
        ]
        client = MockRevitMCPClient(clearance_violations=violations)
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert len(findings) == 3
        ids = [f["element_id"] for f in findings]
        assert "100" in ids and "101" in ids and "102" in ids


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------


class TestSeverityClassification:
    @pytest.mark.asyncio
    async def test_high_severity_when_below_5pct(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 10.0)],  # 10 / 2400 = 0.4 %
        )
        agent = GeometricQueryAgent(
            mcp=client, geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert findings[0]["severity"] == "severity_high"

    @pytest.mark.asyncio
    async def test_medium_severity_when_between_5_and_50_pct(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 600.0)],  # 600 / 2400 = 25 %
        )
        agent = GeometricQueryAgent(
            mcp=client, geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert findings[0]["severity"] == "severity_medium"

    @pytest.mark.asyncio
    async def test_low_severity_when_between_50_and_100_pct(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 2350.0)],  # 2350 / 2400 = 97.9 %
        )
        agent = GeometricQueryAgent(
            mcp=client, geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert findings[0]["severity"] == "severity_low"

    @pytest.mark.asyncio
    async def test_zero_clearance_is_high_severity_not_swallowed(self):
        """Low1: an ``or``-coalesce (``c.get("clearanceActualMm") or ... or 0``)
        treats 0.0 (falsy) as "missing" and falls through to the next key /
        default — for a clash that is DIRECTLY TOUCHING (the worst case, not
        a missing reading), this must still classify as a real 0.0 mm
        violation (severity_high), not silently coalesce to some other
        field or default in a way that could change the outcome."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 0.0)],
        )
        agent = GeometricQueryAgent(
            mcp=client, geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert findings[0]["severity"] == "severity_high"
        assert "actual 0.0 mm" in findings[0]["message"]

    @pytest.mark.asyncio
    async def test_zero_clearance_mm_key_preferred_over_actual_mm_fallback(self):
        """Low1: when ``clearanceActualMm`` is absent but ``clearanceMm`` is
        explicitly 0.0, the coalesce must read that real 0.0 — not treat it
        as absent and fall through to a non-zero fallback key."""
        clash = {
            "elementA": {"id": 2, "name": "Duct B", "category": "Ducts", "source": "host"},
            "elementB": {"id": 9000, "name": "Floor 1", "category": "Floors", "source": "link"},
            "type": "clearance_violation",
            "clearanceMm": 0.0,
            "actualMm": 999.0,  # decoy — must NOT be picked when clearanceMm is present
        }
        client = MockRevitMCPClient(clearance_violations=[clash])
        agent = GeometricQueryAgent(
            mcp=client, geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert findings[0]["severity"] == "severity_high"
        assert "actual 0.0 mm" in findings[0]["message"]


# ---------------------------------------------------------------------------
# Link auto-discovery
# ---------------------------------------------------------------------------


class TestLinkDiscovery:
    @pytest.mark.asyncio
    async def test_explicit_link_id_skips_discovery(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 100.0)],
            # No linked_files seeded — if discovery runs it finds nothing
            linked_files=[],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="linked_arch")],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        # Should still work because we gave an explicit id
        assert len(findings) == 1
        # Verify get_linked_files was NOT called
        linked_calls = client.calls_to("revit_get_linked_files")
        assert len(linked_calls) == 0

    @pytest.mark.asyncio
    async def test_auto_discovers_arch_link(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(2, 200.0)],
            linked_files=[
                {"id": 9999, "name": "Snowdon Towers Sample Architectural.rvt"},
                {"id": 8888, "name": "Snowdon Towers Sample Structural.rvt"},
            ],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="linked_arch")],
            # No explicit linked_file_ids — agent must discover
        )
        findings = await agent.run()
        assert len(findings) == 1
        # Check clearance was called with the discovered linkId
        cc_calls = client.calls_to("revit_check_clearance")
        assert len(cc_calls) == 1
        assert cc_calls[0]["setB"]["linkId"] == 9999
        assert cc_calls[0]["setB"]["source"] == "link"

    @pytest.mark.asyncio
    async def test_same_model_uses_no_link_id(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(3, 300.0)],
        )
        rule = _clearance_rule(reference_source="same_model")
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[rule],
        )
        findings = await agent.run()
        assert len(findings) == 1
        cc_calls = client.calls_to("revit_check_clearance")
        assert "linkId" not in cc_calls[0]["setB"]
        assert cc_calls[0]["setB"].get("source") == "host"

    @pytest.mark.asyncio
    async def test_linked_mep_resolves_discipline_named_link(self):
        """linked_mep must match discipline-named links (HVAC/Plumbing/...),
        not only a literal 'MEP' — the live R27 regression that returned 0."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(5, 50.0)],
            linked_files=list(_R27_LINKS),
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="linked_mep")],
        )
        findings = await agent.run()
        assert len(findings) == 1
        cc_calls = client.calls_to("revit_check_clearance")
        # Resolved to a real MEP link instead of falling back to host.
        assert cc_calls[0]["setB"].get("source") == "link"
        assert cc_calls[0]["setB"]["linkId"] in {1001, 1002, 1003}

    @pytest.mark.asyncio
    async def test_reference_link_hint_disambiguates_among_mep_links(self):
        """A reference_link_hint picks the exact link when several MEP links
        are loaded (HVAC vs Plumbing vs Electrical)."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(6, 60.0)],
            linked_files=list(_R27_LINKS),
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[
                _clearance_rule(reference_source="linked_mep", reference_link_hint="HVAC"),
            ],
        )
        findings = await agent.run()
        assert len(findings) == 1
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0]["setB"]["linkId"] == 1001  # the HVAC link, specifically

    @pytest.mark.asyncio
    async def test_linked_struct_still_resolves(self):
        """linked_struct must keep working — 'Structural' is in the link name."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(7, 70.0)],
            linked_files=list(_R27_LINKS),
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="linked_struct")],
        )
        findings = await agent.run()
        assert len(findings) == 1
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0]["setB"]["linkId"] == 1004

    @pytest.mark.asyncio
    async def test_two_mep_rules_with_distinct_hints_resolve_separately(self):
        """Two linked_mep rules with different hints resolve to different links
        and are NOT collapsed into one batch by reference_source alone."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(8, 80.0)],
            linked_files=list(_R27_LINKS),
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[
                _clearance_rule("r.hvac", reference_source="linked_mep",
                                reference_link_hint="HVAC", reference_category="Ducts"),
                _clearance_rule("r.plumb", reference_source="linked_mep",
                                reference_link_hint="Plumbing", reference_category="Pipes"),
            ],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        link_ids = {c["setB"].get("linkId") for c in cc_calls}
        assert link_ids == {1001, 1002}  # HVAC + Plumbing, two separate calls


# ---------------------------------------------------------------------------
# clearance_max rule
# ---------------------------------------------------------------------------


class TestClearanceMax:
    """H-01 (2026-08-01 review): a max rule flags the pairs the tool DOESN'T
    return. check_clearance only emits pairs CLOSER than the clearanceMm it
    was called with, so the old implementation (call with the rule's own
    threshold, flag everything returned) was a dead check that also cried
    wolf: real violations never came back, compliant pairs got flagged. These
    tests run against a mock that enforces the addin's actual filter — the
    old code turns every one of them red.
    """

    @staticmethod
    def _max_rule(threshold_mm: float = 100.0, direction: str = "below"):
        return _clearance_rule(
            check_type="clearance_max",
            threshold_mm=threshold_mm,
            direction=direction,
            reference_source="same_model",
        )

    @pytest.mark.asyncio
    async def test_a_pair_farther_than_the_threshold_is_flagged(self):
        client = MockRevitMCPClient(clearance_violations=[_clash(10, 150.0)])
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        findings = await agent.run()
        assert len(findings) == 1
        assert findings[0]["rule_id"] == "ducts.floor_clearance"
        # The message states the comparison the element failed, with the
        # measured distance — this is what lands in the ACC issue body.
        assert "150.0 mm" in findings[0]["message"]
        assert "exceeds maximum 100 mm" in findings[0]["message"]
        # 150/100 = 1.5× the allowed gap → medium on the EXCESS scale.
        assert findings[0]["severity"] == "severity_medium"

    @pytest.mark.asyncio
    async def test_the_probe_is_wider_than_the_threshold(self):
        """The heart of H-01: called with clearanceMm == the rule's own
        threshold, the addin filters every violation out of the response
        (mock now enforces that), so the probe MUST be wider."""
        client = MockRevitMCPClient(clearance_violations=[_clash(10, 150.0)])
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        findings = await agent.run()
        call = client.calls_to("revit_check_clearance")[0]
        assert call["clearanceMm"] > 100.0, (
            "probing with the rule's own threshold makes every violation "
            "invisible — the addin only returns pairs CLOSER than clearanceMm"
        )
        assert findings, "the 150 mm violation must survive the probe round-trip"

    @pytest.mark.asyncio
    async def test_a_pair_within_the_threshold_is_not_flagged(self):
        """The old code's false-positive half: the only pairs a
        threshold-sized call DID return were the compliant ones — and it
        flagged them all."""
        client = MockRevitMCPClient(clearance_violations=[_clash(10, 40.0)])
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        assert await agent.run() == []

    @pytest.mark.asyncio
    async def test_the_nearest_reference_decides_the_verdict(self):
        """An element within the threshold of ANY reference complies — a
        second, farther reference must not flag it."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(10, 150.0), _clash(10, 90.0)],
        )
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        assert await agent.run() == []

    @pytest.mark.asyncio
    async def test_verdicts_are_per_element(self):
        client = MockRevitMCPClient(
            clearance_violations=[_clash(10, 150.0), _clash(11, 60.0)],
        )
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        findings = await agent.run()
        assert [f["element_id"] for f in findings] == ["10"]

    @pytest.mark.asyncio
    async def test_severity_grades_the_excess_not_the_shortfall(self):
        """2.5× the allowed gap is the WORST offender. The min-rule scale
        (fraction of required clearance present) runs the wrong way here —
        it graded exactly this case severity_low."""
        client = MockRevitMCPClient(clearance_violations=[_clash(10, 250.0)])
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[self._max_rule()])
        findings = await agent.run()
        assert findings[0]["severity"] == "severity_high"

    @pytest.mark.asyncio
    async def test_horizontal_max_fails_closed_not_silently_clean(self):
        """bbox mode reports no measured distance, so a horizontal max rule
        cannot be judged — it must land in rules_failed (P1-GEO-01: 'could
        not check' must never render as 'checked, clean')."""
        client = MockRevitMCPClient(clearance_violations=[_clash(10, 150.0)])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[self._max_rule(direction="horizontal")],
        )
        findings = await agent.run()
        assert findings == []
        assert client.calls_to("revit_check_clearance") == []
        cov = agent.coverage
        assert cov["verdict"] == "no_audit"
        failed = {f["rule_id"]: f for f in cov["rules_failed"]}
        assert failed["ducts.floor_clearance"]["reason"] == "unsupported_direction"


# ---------------------------------------------------------------------------
# Unsupported check_type
# ---------------------------------------------------------------------------


class TestUnsupportedCheckType:
    @pytest.mark.asyncio
    async def test_spatial_containment_skipped_no_crash(self):
        client = MockRevitMCPClient()
        rule = GeometryRule(
            id="test.containment",
            category="Spaces",
            check_type="spatial_containment",
            description="Space containment",
        )
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        findings = await agent.run()
        assert findings == []
        # No check_clearance calls should have been made
        assert len(client.calls_to("revit_check_clearance")) == 0

    @pytest.mark.asyncio
    async def test_min_spacing_skipped_no_crash(self):
        client = MockRevitMCPClient()
        rule = GeometryRule(
            id="test.spacing",
            category="Structural Columns",
            check_type="min_spacing",
            description="Column spacing",
            threshold_mm=1000.0,
        )
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        findings = await agent.run()
        assert findings == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_check_failure_returns_empty_findings(self):
        client = MockRevitMCPClient(fail_on={"revit_check_clearance"})
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1},
        )
        # Should not raise — failure is logged and returns []
        findings = await agent.run()
        assert findings == []
        # P1-GEO-01: `findings == []` alone is NOT the contract — that
        # ambiguity is exactly what let a crashed check exit 0 as a clean
        # audit. The run must also be able to say it checked NOTHING.
        cov = agent.coverage
        assert cov["verdict"] == "no_audit"
        assert cov["rules_executed"] == []
        assert cov["rules_failed"][0]["rule_id"] == "ducts.floor_clearance"
        assert cov["rules_failed"][0]["reason"] == "mcp_error"

    @pytest.mark.asyncio
    async def test_successful_check_with_no_clashes_is_not_a_failure(self):
        """The other half of the pair: empty because it LOOKED and found
        nothing. Same `findings` value, opposite meaning — telling those two
        apart is the entire purpose of the coverage record."""
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1},
        )
        findings = await agent.run()
        assert findings == []
        cov = agent.coverage
        assert cov["verdict"] == "ok"
        assert cov["rules_executed"] == ["ducts.floor_clearance"]
        assert cov["rules_failed"] == []

    @pytest.mark.asyncio
    async def test_link_discovery_failure_blocks_the_rule(self):
        """Renamed from `..._still_proceeds` — P1-GEO-02 changed the contract.

        The old name and its lone `findings == []` assertion described the
        defect: with discovery failed the rule ran anyway, and the client reads
        `link_id=None` as `setB.source="host"`. So a rule asking about the
        linked architectural model silently answered a question about the host
        instead, and reported that answer as if it were the one asked. Refusing
        to run is the only honest option — the rule's scope does not exist.
        """
        client = MockRevitMCPClient(
            fail_on={"revit_get_linked_files"},
            clearance_violations=[],
        )
        rule = _clearance_rule(reference_source="linked_arch")
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        findings = await agent.run()
        assert findings == []
        # The load-bearing assertion: no clearance call was made AT ALL, so it
        # cannot have been made against the host.
        assert client.calls_to("revit_check_clearance") == []
        cov = agent.coverage
        assert cov["verdict"] == "no_audit"
        failed = cov["rules_failed"][0]
        assert failed["rule_id"] == "ducts.floor_clearance"
        assert failed["reason"] == "link_discovery_failed"
        assert failed["reference_source"] == "linked_arch"

    @pytest.mark.asyncio
    async def test_unmatched_link_blocks_the_rule_and_names_what_was_available(self):
        # The commoner case: discovery works, but no link matches the rule's
        # discipline. The author needs to see what the model DID offer.
        client = MockRevitMCPClient(clearance_violations=[], linked_files=[])
        rule = _clearance_rule(reference_source="linked_arch")
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        await agent.run()
        assert client.calls_to("revit_check_clearance") == []
        failed = agent.coverage["rules_failed"][0]
        assert failed["reason"] == "link_not_found"
        assert "available" in failed

    @pytest.mark.asyncio
    async def test_same_model_rule_is_never_blocked(self):
        # Guard: `same_model` legitimately means the host. Blocking it would
        # break every non-federated geometry rule.
        client = MockRevitMCPClient(clearance_violations=[])
        rule = _clearance_rule(reference_source="same_model")
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        await agent.run()
        assert client.calls_to("revit_check_clearance")
        assert agent.coverage["verdict"] == "ok"


# ---------------------------------------------------------------------------
# Multiple rules fan-out
# ---------------------------------------------------------------------------


class TestMultipleRules:
    @pytest.mark.asyncio
    async def test_two_rules_same_key_batched_and_deduped(self):
        """Two same-key clearance_min rules → 1 MCP call, deduped to 1 Finding."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 100.0)],
        )
        rules = [
            _clearance_rule("rule.a", threshold_mm=2400.0, reference_source="same_model"),
            _clearance_rule("rule.b", threshold_mm=500.0, reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        findings = await agent.run()
        # Both rules fire on the same element → deduped to 1 Finding
        assert len(findings) == 1
        f = findings[0]
        assert "rule.a" in f["rule_id"]
        assert "rule.b" in f["rule_id"]
        # Only 1 MCP call (batched); threshold = max(2400, 500) = 2400
        cc_calls = client.calls_to("revit_check_clearance")
        assert len(cc_calls) == 1
        assert cc_calls[0]["clearanceMm"] == 2400.0

    @pytest.mark.asyncio
    async def test_two_violations_different_elements(self):
        """Two violations on different elements → 2 separate Findings (no merge)."""
        client = MockRevitMCPClient(
            clearance_violations=[
                _clash(1, 100.0, "Duct A"),
                _clash(2, 1000.0, "Duct B"),
            ],
        )
        rules = [
            _clearance_rule("rule.a", threshold_mm=2400.0, reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        findings = await agent.run()
        assert len(findings) == 2
        ids = {f["element_id"] for f in findings}
        assert ids == {"1", "2"}


# ---------------------------------------------------------------------------
# MCP call payload validation
# ---------------------------------------------------------------------------


class TestMCPPayload:
    @pytest.mark.asyncio
    async def test_check_clearance_payload_structure(self):
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(threshold_mm=2400.0)],
            linked_file_ids={"linked_arch": 1362429},
            view_id=1551372,
            sample_count=5,
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert len(cc_calls) == 1
        payload = cc_calls[0]
        assert payload["setA"]["categories"] == ["OST_DuctCurves"]
        assert payload["setA"]["source"] == "host"
        assert payload["setB"]["categories"] == ["OST_Floors"]
        assert payload["setB"]["linkId"] == 1362429
        assert payload["setB"]["source"] == "link"
        assert payload["axis"] == "Z"
        assert payload["direction"] == "below"
        assert payload["clearanceMm"] == 2400.0
        assert payload["viewId"] == 1551372
        assert payload["sampleCount"] == 5


# ---------------------------------------------------------------------------
# Batching — same geometry key → 1 MCP call, per-rule Python filtering
# ---------------------------------------------------------------------------


class TestBatching:
    @pytest.mark.asyncio
    async def test_different_thresholds_filtered_per_rule(self):
        """Batch with max threshold; lower-threshold rule doesn't fire on marginal violators."""
        client = MockRevitMCPClient(
            clearance_violations=[
                _clash(1, 100.0, "Duct A"),   # violates both rules
                _clash(2, 1000.0, "Duct B"),  # violates rule.a (2400) but NOT rule.b (500)
            ],
        )
        rules = [
            _clearance_rule("rule.a", threshold_mm=2400.0, reference_source="same_model"),
            _clearance_rule("rule.b", threshold_mm=500.0, reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        findings = await agent.run()

        # 1 MCP call (batched)
        assert len(client.calls_to("revit_check_clearance")) == 1
        # Element 1 violates both → deduped to 1; element 2 violates only rule.a → 1
        assert len(findings) == 2
        element_ids = {f["element_id"] for f in findings}
        assert element_ids == {"1", "2"}
        # Element 1's merged finding carries both rule ids
        elem1 = next(f for f in findings if f["element_id"] == "1")
        assert "rule.a" in elem1["rule_id"]
        assert "rule.b" in elem1["rule_id"]
        # Element 2 only violates rule.a
        elem2 = next(f for f in findings if f["element_id"] == "2")
        assert elem2["rule_id"] == "rule.a"

    @pytest.mark.asyncio
    async def test_different_direction_not_batched(self):
        """Rules with different clearance_direction get separate MCP calls."""
        client = MockRevitMCPClient(clearance_violations=[_clash(1, 100.0)])
        rules = [
            _clearance_rule("rule.below", threshold_mm=2400.0, direction="below",
                            reference_source="same_model"),
            _clearance_rule("rule.horiz", threshold_mm=500.0, direction="horizontal",
                            reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        await agent.run()
        # Different direction → different _ClearanceKey → 2 calls
        assert len(client.calls_to("revit_check_clearance")) == 2

    @pytest.mark.asyncio
    async def test_bbox_rules_with_different_thresholds_get_their_own_call(self):
        """bbox-refilter bug: two horizontal rules of different thresholds must
        NOT share a call.

        Batching relies on re-applying each rule's threshold in Python, which
        needs a measured distance per hit — and bbox mode reports none. Batched,
        the 100 mm rule inherited every pair the 500 mm call returned. The
        threshold is now part of the key, so each bbox rule gets a call carrying
        ITS OWN clearanceMm and the addin's AABB inflation does the filtering.
        """
        client = MockRevitMCPClient(clearance_violations=[_bbox_clash(1, "Duct A")])
        rules = [
            _clearance_rule("rule.tight", threshold_mm=100.0, direction="horizontal",
                            reference_source="same_model"),
            _clearance_rule("rule.loose", threshold_mm=500.0, direction="horizontal",
                            reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        await agent.run()

        calls = client.calls_to("revit_check_clearance")
        assert len(calls) == 2
        assert all(c["axis"] == "bbox" for c in calls)
        assert {c["clearanceMm"] for c in calls} == {100.0, 500.0}

    @pytest.mark.asyncio
    async def test_z_rules_with_different_thresholds_still_share_one_call(self):
        """The Z raycast DOES measure per hit, so its batching is untouched.

        Pins the other end of the bbox-refilter fix: it must not cost the
        vertical path its
        batching (that would be N calls where 1 was correct).
        """
        client = MockRevitMCPClient(clearance_violations=[_clash(1, 50.0)])
        rules = [
            _clearance_rule("rule.a", threshold_mm=2400.0, direction="below",
                            reference_source="same_model"),
            _clearance_rule("rule.b", threshold_mm=500.0, direction="below",
                            reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        await agent.run()

        calls = client.calls_to("revit_check_clearance")
        assert len(calls) == 1
        assert calls[0]["axis"] == "Z"
        assert calls[0]["clearanceMm"] == 2400.0

    @pytest.mark.asyncio
    async def test_bbox_hits_are_not_refiltered_against_a_missing_distance(self):
        """bbox-refilter bug, other half: a hard-clash bbox rule (threshold 0)
        reported NOTHING.

        The refilter asked ``_clearance_actual_mm(c) < 0.0`` of rows that carry
        no distance — 0.0 < 0.0 is False — so every real hard clash was dropped
        on the client side. bbox results are no longer refiltered at all.
        """
        client = MockRevitMCPClient(
            clearance_violations=[_bbox_clash(1, "Duct A"), _bbox_clash(2, "Duct B")],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule("rule.hard", threshold_mm=0.0,
                                            direction="horizontal",
                                            reference_source="same_model")],
        )
        findings = await agent.run()

        assert {f["element_id"] for f in findings} == {"1", "2"}
        # …and the message must not present the 0.0 default as a reading.
        assert "0.0 mm" not in findings[0]["message"]
        assert "not measured" in findings[0]["message"]

    @pytest.mark.asyncio
    async def test_clearance_max_not_batched_with_min(self):
        """clearance_max rules always run individually, never batched with min."""
        client = MockRevitMCPClient(clearance_violations=[_clash(1, 150.0)])
        rules = [
            _clearance_rule("rule.min", check_type="clearance_min", threshold_mm=2400.0,
                            reference_source="same_model"),
            _clearance_rule("rule.max", check_type="clearance_max", threshold_mm=100.0,
                            reference_source="same_model"),
        ]
        agent = GeometricQueryAgent(mcp=client, geometry_rules=rules)
        await agent.run()
        # min batch + max singleton = 2 calls
        assert len(client.calls_to("revit_check_clearance")) == 2


# ---------------------------------------------------------------------------
# Dedup — multiple findings for same element_id → 1 merged Finding
# ---------------------------------------------------------------------------


class TestDedup:
    @pytest.mark.asyncio
    async def test_worst_severity_wins(self):
        """When merging, the most severe finding's severity is used."""
        # Two rules, different directions to prevent batching
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 10.0)],  # < 5 % of 2400 → high
        )
        rules = [
            _clearance_rule("rule.high", threshold_mm=2400.0, direction="below",
                            reference_source="same_model"),
            _clearance_rule("rule.medium", threshold_mm=2400.0, direction="horizontal",
                            reference_source="same_model"),
        ]
        # Seed 600 mm for horizontal (600/2400 = 25% → medium)
        # But mock always returns same violations; override per call via clearance_mm
        # Simpler: use one direction and let batching merge the two violations for element 1
        # into one merged finding. Use _clash with the actual field set intentionally.
        client2 = MockRevitMCPClient(
            clearance_violations=[
                _clash(1, 10.0),   # 10/2400 = 0.4% → severity_high
                _clash(1, 600.0),  # 600/2400 = 25% → severity_medium (same element!)
            ],
        )
        # single rule → no dedup via rule merge, but same element appears twice in clashes
        agent = GeometricQueryAgent(
            mcp=client2,
            geometry_rules=[_clearance_rule("rule.a", threshold_mm=2400.0,
                                            reference_source="same_model")],
        )
        findings = await agent.run()
        # Two clashes on element "1" → deduped
        assert len(findings) == 1
        assert findings[0]["severity"] == "severity_high"
        assert "2 violations" in findings[0]["message"]

    @pytest.mark.asyncio
    async def test_dedup_message_contains_count(self):
        """Merged finding's message reports the violation count."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(42, 100.0), _clash(42, 200.0)],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model",
                                            threshold_mm=2400.0)],
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert "2 violations" in findings[0]["message"]
        assert "[1]" in findings[0]["message"]
        assert "[2]" in findings[0]["message"]

    @pytest.mark.asyncio
    async def test_single_finding_not_modified_by_dedup(self):
        """A single finding for an element passes through dedup unchanged."""
        client = MockRevitMCPClient(
            clearance_violations=[_clash(1, 100.0, "Duct A")],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert "violations on this element" not in findings[0]["message"]


# ---------------------------------------------------------------------------
# view_id auto-resolution
# ---------------------------------------------------------------------------


class TestViewIdResolution:
    @pytest.mark.asyncio
    async def test_constructor_view_id_takes_priority(self):
        """Constructor view_id is used without calling get_active_view."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 99, "viewType": "ThreeD"},
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
            view_id=42,
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0].get("viewId") == 42
        # get_active_view must NOT have been called
        assert len(client.calls_to("revit_get_active_view")) == 0

    @pytest.mark.asyncio
    async def test_active_3d_view_used(self):
        """Active ThreeDimensional view ID is picked up automatically."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 12345, "name": "3D View", "viewType": "ThreeD"},
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0].get("viewId") == 12345

    @pytest.mark.asyncio
    async def test_threedimensional_alias_still_accepted(self):
        """Defensive: the spelled-out 'ThreeDimensional' is accepted too."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 7777, "viewType": "ThreeDimensional"},
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        assert client.calls_to("revit_check_clearance")[0].get("viewId") == 7777

    @pytest.mark.asyncio
    async def test_non_3d_active_view_falls_through_to_named(self):
        """Active view is a floor plan → searches all_views for a named 3D view."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 99, "name": "Level 1", "viewType": "FloorPlan"},
            all_views=[
                {"id": 55555, "name": "3D Check Clearance", "viewType": "ThreeD"},
            ],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0].get("viewId") == 55555

    @pytest.mark.asyncio
    async def test_keyword_priority_coordination_over_plain_3d(self):
        """'Coordination' keyword view picked over an unnamed 3D view listed first."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 1, "viewType": "FloorPlan"},
            all_views=[
                {"id": 1001, "name": "Generic 3D", "viewType": "ThreeD"},
                {"id": 1002, "name": "Coordination View", "viewType": "ThreeD"},
            ],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        # "3d" keyword matches "Generic 3D" (id 1001) before "Coordination" keyword
        # because "3d" is first in _VIEW_KEYWORDS and "generic 3d" contains "3d"
        assert cc_calls[0].get("viewId") == 1001

    @pytest.mark.asyncio
    async def test_first_3d_fallback_when_no_keyword_match(self):
        """When no view name matches a keyword, the first 3D view is used."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 1, "viewType": "FloorPlan"},
            all_views=[
                {"id": 9001, "name": "Structural Analysis", "viewType": "ThreeD"},
                {"id": 9002, "name": "Fire Escape", "viewType": "ThreeD"},
            ],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0].get("viewId") == 9001

    @pytest.mark.asyncio
    async def test_no_3d_view_available_sends_no_view_id(self):
        """No 3D views at all → viewId omitted from MCP payload."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 1, "viewType": "FloorPlan"},
            all_views=[
                {"id": 9001, "name": "Level 1", "viewType": "FloorPlan"},
            ],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert "viewId" not in cc_calls[0]

    @pytest.mark.asyncio
    async def test_per_rule_view_id_overrides_agent_view(self):
        """Rule-level view_id takes priority over agent-level active-view resolution."""
        client = MockRevitMCPClient(
            clearance_violations=[],
            active_view={"id": 12345, "viewType": "ThreeD"},
        )
        rule = GeometryRule(
            id="test.rule",
            category="Ducts",
            check_type="clearance_min",
            description="Test",
            threshold_mm=500.0,
            clearance_direction="below",
            reference_source="same_model",
            view_id=99999,  # per-rule override
        )
        agent = GeometricQueryAgent(mcp=client, geometry_rules=[rule])
        await agent.run()
        cc_calls = client.calls_to("revit_check_clearance")
        assert cc_calls[0].get("viewId") == 99999


# ---------------------------------------------------------------------------
# P0 element cap + axis/direction mapping
# ---------------------------------------------------------------------------


class TestElementCap:
    @pytest.mark.asyncio
    async def test_default_caps_reach_payload(self):
        """Default max_elements=300 → setA.limit; max_clashes=500 → maxResults."""
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        await agent.run()
        payload = client.calls_to("revit_check_clearance")[0]
        assert payload["setA"]["limit"] == 300
        assert payload["maxResults"] == 500

    @pytest.mark.asyncio
    async def test_custom_caps_reach_payload(self):
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
            max_elements=200,
            max_clashes=250,
        )
        await agent.run()
        payload = client.calls_to("revit_check_clearance")[0]
        assert payload["setA"]["limit"] == 200
        assert payload["maxResults"] == 250


class TestAxisDirectionMapping:
    @pytest.mark.asyncio
    async def test_below_maps_to_z_axis(self):
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(direction="below", reference_source="same_model")],
        )
        await agent.run()
        payload = client.calls_to("revit_check_clearance")[0]
        assert payload["axis"] == "Z"
        assert payload["direction"] == "below"
        assert "sampleCount" in payload

    @pytest.mark.asyncio
    async def test_horizontal_maps_to_bbox_no_direction(self):
        """horizontal → axis='bbox'; direction + sampleCount omitted (Z-only)."""
        client = MockRevitMCPClient(clearance_violations=[])
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[
                _clearance_rule(direction="horizontal", reference_source="same_model"),
            ],
        )
        await agent.run()
        payload = client.calls_to("revit_check_clearance")[0]
        assert payload["axis"] == "bbox"
        assert "direction" not in payload
        assert "sampleCount" not in payload


# ---------------------------------------------------------------------------
# Severity of unmeasured bbox hits (owner decision 2026-08-02)
# ---------------------------------------------------------------------------


class TestUnmeasuredBboxSeverity:
    """A bbox hit carries no measured distance, so the fraction scale cannot
    grade it — it was reading the 0.0 default as "measured zero", which graded
    every proximity hit HIGH and (because its threshold<=0 branch was written
    for the measured path) every hard clash LOW: the two elements physically
    intersecting came out milder than a near-miss. Owner decision 2026-08-02:

      * hard clash (threshold <= 0, the addin's own ``hard_clash``
        classification boundary) → severity_high — the worst outcome this
        check can find;
      * unmeasured proximity (threshold > 0) → severity_medium — the
        violation is certain (the addin applied THIS rule's threshold via
        AABB inflation) but its magnitude is unknown, and an unknown gap is
        not evidence of the worst gap.

    The measured (Z) scale is pinned by TestSeverityClassification and must
    not move.
    """

    @pytest.mark.asyncio
    async def test_hard_clash_is_high_not_low(self):
        client = MockRevitMCPClient(
            clearance_violations=[_bbox_clash(1, "Duct A", ref_name="Beam 9")],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule("rule.hard", threshold_mm=0.0,
                                            direction="horizontal",
                                            reference_source="same_model")],
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert findings[0]["severity"] == "severity_high"
        assert "elements intersect" in findings[0]["message"]
        assert "not measured" in findings[0]["message"]
        assert "0.0 mm" not in findings[0]["message"]

    @pytest.mark.asyncio
    async def test_unmeasured_proximity_is_medium_not_high(self):
        client = MockRevitMCPClient(
            clearance_violations=[_bbox_clash(1, "Duct A")],
        )
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule("rule.near", threshold_mm=500.0,
                                            direction="horizontal",
                                            reference_source="same_model")],
        )
        findings = await agent.run()
        assert len(findings) == 1
        assert findings[0]["severity"] == "severity_medium"
        assert "within 500 mm" in findings[0]["message"]
        assert "not measured" in findings[0]["message"]


# ---------------------------------------------------------------------------
# Cancellation (L-09)
# ---------------------------------------------------------------------------


class TestCancellationPropagates:
    @pytest.mark.asyncio
    async def test_a_cancelled_task_is_not_swallowed_as_a_rule_failure(self):
        """L-09: `gather(return_exceptions=True)` also captures BaseExceptions.

        `CancelledError` is not an `Exception`, so the per-task error branch
        skipped it and the result fell through to `all_findings.extend(...)`,
        which raised `TypeError: 'CancelledError' object is not iterable` —
        the run dying of a confusing type error instead of stopping, with the
        other tasks' rules missing from coverage because the loop never
        finished. A cancellation must keep propagating as the control-flow
        signal it is.
        """
        client = MockRevitMCPClient(clearance_violations=[])

        async def cancelled(*a, **kw):
            raise asyncio.CancelledError

        client.check_clearance = cancelled  # type: ignore[method-assign]
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        with pytest.raises(asyncio.CancelledError):
            await agent.run()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_is_still_recorded_not_raised(self):
        """The other end: a real MCP error stays a per-rule coverage entry, so
        the cancellation fix must not turn every failure into a crash."""
        client = MockRevitMCPClient(clearance_violations=[])

        async def boom(*a, **kw):
            raise RuntimeError("addin exploded")

        client.check_clearance = boom  # type: ignore[method-assign]
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_clearance_rule(reference_source="same_model")],
        )
        assert await agent.run() == []
        cov = agent.coverage
        assert cov["verdict"] == "no_audit"
        assert cov["rules_failed"][0]["reason"] == "mcp_error"


class TestCategoryResolution:
    """`_to_ost` must resolve any category the OST catalog knows, not just the
    fourteen labels this module used to hardcode.

    Found by probing live on Snowdon, 2026-08-03. The map was the WHOLE
    resolution and ended in `.get(label, label)`, so a rule on any other
    category handed the addin a display name and came back
    ``[invalid_parameter] Unknown BuiltInCategory 'Doors'``. Fifty of the
    catalog's sixty-three categories were affected — the whole geometry
    feature was unusable outside the MEP labels it shipped with. Nothing
    caught it because no shipped config carries a geometry rule (so the
    nightly never exercised this) and every test here used those same labels.

    The failure was at least LOUD: the rule failed `mcp_error`, geometry
    coverage recorded `no_audit`, and the run exited 1 rather than reporting
    zero clashes. That honesty is what made the bug findable in one run — see
    TestGeometryNoAudit above.
    """

    def test_a_category_outside_the_override_map_resolves_via_the_catalog(self):
        from bim_orchestrator.agents.geometry_query import _OST_BY_LABEL, _to_ost

        # Precondition: these are exactly the labels the old map could NOT do.
        for label in ("Doors", "Furniture", "Lighting Fixtures", "Windows"):
            assert label not in _OST_BY_LABEL

        assert _to_ost("Doors") == "OST_Doors"
        assert _to_ost("Furniture") == "OST_Furniture"
        assert _to_ost("Lighting Fixtures") == "OST_LightingFixtures"
        assert _to_ost("Windows") == "OST_Windows"

    def test_every_override_still_answers_exactly_as_before(self):
        """The map is now overrides, so it must still WIN. Two of its entries
        disagree with the catalog on purpose — `Columns` most of all: the
        catalog lists "Columns" as an alias of Structural Columns, so deleting
        this line would silently retarget an architectural-column rule at the
        structural category (on Snowdon: 140 instances versus a category with
        none, i.e. a clean bill of health from an empty set)."""
        from bim_orchestrator.agents.geometry_query import _OST_BY_LABEL, _to_ost

        for label, expected in _OST_BY_LABEL.items():
            assert _to_ost(label) == expected, (
                f"{label} no longer resolves to {expected} — the override map "
                "must take priority over the catalog"
            )
        # Spelled out, because this is the one the catalog would answer
        # differently and a future 'simplification' would take out first.
        assert _to_ost("Columns") == "OST_Columns"

    def test_an_unknown_label_is_passed_through_not_raised(self):
        """Passing the label through reaches the addin as an unknown category
        and fails the rule loudly, which is what the coverage record needs.
        Raising here would lose the whole batch instead of naming the rule."""
        from bim_orchestrator.agents.geometry_query import _to_ost

        assert _to_ost("Not A Real Category") == "Not A Real Category"

    @pytest.mark.asyncio
    async def test_the_agent_actually_sends_the_resolved_category(self):
        """Pin the WIRE, not just the helper — the repeated lesson of this
        repo. A correct `_to_ost` nobody called would pass every test above
        while the addin still received "Doors"."""
        client = MockRevitMCPClient(clearance_violations=[])
        rule = GeometryRule(
            id="doors.column_clearance",
            category="Doors",
            check_type="clearance_min",
            description="Door leaf must clear a column",
            threshold_mm=100.0,
            clearance_direction="horizontal",
            reference_category="Windows",
            reference_source="same_model",
        )
        await GeometricQueryAgent(mcp=client, geometry_rules=[rule]).run()

        payload = client.calls_to("revit_check_clearance")[0]
        assert payload["setA"]["categories"] == ["OST_Doors"]
        assert payload["setB"]["categories"] == ["OST_Windows"]


# ---------------------------------------------------------------------------
# spatial_filter (2026-08-26) — schema + Builder UI carried it since 3b, but
# NOTHING consumed it: a scoped rule silently behaved unscoped. Motivating
# case: scope the headroom rule to the Parking space, because mech-shaft
# service platforms legitimately sit 300-700 mm under duct runs and drown the
# walkable-space story.
# ---------------------------------------------------------------------------


def _spatial_fixture() -> MockRevitMCPClient:
    """Two ducts, two spaces: duct 101 inside 'Parking Garage P01', duct 102
    inside 'Corridor 200'. Both violate the threshold."""
    return MockRevitMCPClient(
        clearance_violations=[
            _clash(101, 500.0, element_name="Parking duct"),
            _clash(102, 480.0, element_name="Corridor duct"),
        ],
        spaces=[
            {"id": 9001, "name": "Parking Garage P01", "volume": 121043.0},
            {"id": 9002, "name": "Corridor 200", "volume": 4985.0},
        ],
        element_info={
            9001: {"name": "Parking Garage P01", "boundingBox": {
                "min": {"x": 0, "y": 0, "z": -20}, "max": {"x": 100, "y": 100, "z": -5}}},
            9002: {"name": "Corridor 200", "boundingBox": {
                "min": {"x": 200, "y": 0, "z": 0}, "max": {"x": 300, "y": 100, "z": 12}}},
            101: {"name": "Parking duct", "boundingBox": {
                "min": {"x": 10, "y": 10, "z": -10}, "max": {"x": 12, "y": 12, "z": -9}}},
            102: {"name": "Corridor duct", "boundingBox": {
                "min": {"x": 250, "y": 50, "z": 8}, "max": {"x": 252, "y": 52, "z": 9}}},
        },
    )


def _scoped_rule(**filter_kwargs) -> GeometryRule:
    # NOT model_copy(update=...) — that bypasses validation and smuggles a
    # bare dict where the agent expects the model (the YAML path validates).
    from bim_orchestrator.policies.rules_schema import GeometryRuleSpatialFilter

    rule = _clearance_rule(rule_id="ducts.parking_headroom")
    return rule.model_copy(
        update={"spatial_filter": GeometryRuleSpatialFilter(**filter_kwargs)}
    )


class TestSpatialFilter:
    @pytest.mark.asyncio
    async def test_name_contains_keeps_only_matching_space(self):
        agent = GeometricQueryAgent(
            mcp=_spatial_fixture(),
            geometry_rules=[_scoped_rule(name_contains="Parking")],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert [f["element_id"] for f in findings] == ["101"]

    @pytest.mark.asyncio
    async def test_name_exact_is_case_insensitive(self):
        agent = GeometricQueryAgent(
            mcp=_spatial_fixture(),
            geometry_rules=[_scoped_rule(name_exact="parking garage p01")],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert [f["element_id"] for f in findings] == ["101"]

    @pytest.mark.asyncio
    async def test_no_filter_is_untouched(self):
        agent = GeometricQueryAgent(
            mcp=_spatial_fixture(),
            geometry_rules=[_clearance_rule()],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert sorted(f["element_id"] for f in findings) == ["101", "102"]

    @pytest.mark.asyncio
    async def test_no_spaces_fails_open_with_all_findings(self):
        # A failed/empty space lookup must NOT silently drop real violations:
        # extra out-of-scope findings are visible noise, a hole is not.
        client = _spatial_fixture()
        client.spaces = []
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_scoped_rule(name_contains="Parking")],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert sorted(f["element_id"] for f in findings) == ["101", "102"]

    @pytest.mark.asyncio
    async def test_duct_in_no_space_is_dropped_by_a_scoped_rule(self):
        # Outside every space -> containing space is None -> cannot match the
        # scope -> excluded. (The duct is not "innocent until proven": the rule
        # asked for Parking ducts specifically.)
        client = _spatial_fixture()
        client.element_info[102]["boundingBox"] = {
            "min": {"x": 900, "y": 900, "z": 90}, "max": {"x": 902, "y": 902, "z": 91}}
        agent = GeometricQueryAgent(
            mcp=client,
            geometry_rules=[_scoped_rule(name_contains="Parking")],
            linked_file_ids={"linked_arch": 1362429},
        )
        findings = await agent.run()
        assert [f["element_id"] for f in findings] == ["101"]
