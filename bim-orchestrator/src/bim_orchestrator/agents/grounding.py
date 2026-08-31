"""Grounding Agent — attaches BEP/IBC citations to each finding via RAG.

Phase 2 Day 2: pure-Python agent enriches each finding with a citation string.
Phase 2 Day 4: per-rule citation policy (hard vs soft) enforced here.

Design:
    * Sync (no MCP I/O — `VectorStore.search` is synchronous)
    * Embedder injected via the store; tests use bag-of-words, prod uses
      sentence-transformers (or eventually Claude embed)
    * `min_score` filter avoids attaching low-confidence chunks
    * Citation policy comes from the QC RuleSet (passed in via constructor).
      The policy is per-rule, not global — matches the reality that some
      rules come from uploaded documents (hard) while others come from
      free-text user input (soft).

Policy enforcement (Day 4):
    * mode=soft: attach citation if found, leave None otherwise (Phase 1 default)
    * mode=hard: attach citation if found; otherwise:
        - on_missing=warn  → set citation_missing=True on the finding
        - on_missing=downgrade → lower severity by one level + flag missing
    * source_filter: restrict store search to listed sources

Empty-store-with-hard-rules (option B per user decision 2026-05-22):
    Don't refuse to run. Log a clear warning, mark every hard-rule finding's
    citation as missing, let the BIM Manager see the gap in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from bim_orchestrator.agents.qc import CitationPolicy, Rule, RuleSet
from bim_orchestrator.rag.store import Chunk, VectorStore
from bim_orchestrator.state import Finding, OrchestratorState, Severity

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Citation:
    """A grounded reference attached to a finding."""

    source: str            # e.g. "BEP.pdf"
    section: str | None    # e.g. "§4.2"
    page: int | None       # 1-indexed page number
    snippet: str           # first ~200 chars of the chunk text
    score: float           # cosine similarity in [0, 1]


# Severity ordering for downgrade
_SEVERITY_ORDER: tuple[Severity, ...] = (
    "severity_low",
    "severity_medium",
    "severity_high",
)


def _downgrade_severity(current: Severity) -> Severity:
    """Lower severity by one level. severity_low stays at low."""
    try:
        idx = _SEVERITY_ORDER.index(current)
    except ValueError:
        return current
    if idx == 0:
        return current
    return _SEVERITY_ORDER[idx - 1]


class GroundingAgent:
    def __init__(
        self,
        store: VectorStore,
        *,
        rules: RuleSet | None = None,
        top_k: int = 2,
        min_score: float = 0.05,
        snippet_chars: int = 200,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._min_score = min_score
        self._snippet_chars = snippet_chars
        # Map rule_id → Rule for fast lookup. Empty when no RuleSet given,
        # in which case every finding falls back to the default policy (soft).
        self._rules_by_id: dict[str, Rule] = (
            {r.id: r for r in rules.rules} if rules else {}
        )
        # Pre-compute whether ANY rule requires hard citation — drives the
        # "empty store + hard rules" warning behavior (option B).
        self._has_hard_rule = any(
            r.citation.mode == "hard" for r in self._rules_by_id.values()
        )

    def run(self, state: OrchestratorState) -> OrchestratorState:
        # v1 task BB: enrich all 3 finding buckets (non_compliant + manual_review
        # + missing_data). Citation policy applies regardless of which bucket
        # a finding lands in -- "this parameter is required per BEP Sec.X.Y" is
        # the same truth whether the value is missing or wrong.
        all_findings: list[Finding] = (
            list(state.get("findings", []))
            + list(state.get("manual_review_items", []))
            + list(state.get("missing_data_items", []))
        )
        store_empty = self._store.count == 0
        log.info(
            "grounding_agent.start",
            findings=len(state.get("findings", [])),
            manual_review=len(state.get("manual_review_items", [])),
            missing_data=len(state.get("missing_data_items", [])),
            total=len(all_findings),
            store_size=self._store.count,
            top_k=self._top_k,
            min_score=self._min_score,
            has_hard_rule=self._has_hard_rule,
        )

        if store_empty and self._has_hard_rule:
            # Option B: log but proceed. Every hard-rule finding will be
            # marked citation_missing=True so the BIM Manager sees the gap.
            log.warning(
                "grounding_agent.empty_store_with_hard_rules",
                note="hard-mode rules require citations but store is empty — "
                "findings will be flagged citation_missing=True",
            )

        cited = 0
        flagged_missing = 0
        downgraded = 0
        for finding in all_findings:
            rule = self._rules_by_id.get(finding["rule_id"])
            policy = rule.citation if rule is not None else CitationPolicy()
            citations = [] if store_empty else self._cite(finding, policy)

            if citations:
                finding["citation"] = self._format(citations)
                finding["citation_refs"] = [
                    {
                        "source": c.source,
                        "section": c.section,
                        "page": c.page,
                        "snippet": c.snippet,
                        "score": c.score,
                    }
                    for c in citations
                ]
                # Explicit False when a hard rule was successfully cited
                if policy.mode == "hard":
                    finding["citation_missing"] = False
                cited += 1
            else:
                finding["citation"] = None
                if policy.mode == "hard":
                    finding["citation_missing"] = True
                    flagged_missing += 1
                    if policy.on_missing == "downgrade":
                        old = finding["severity"]
                        new = _downgrade_severity(old)
                        if new != old:
                            finding["severity"] = new
                            downgraded += 1
                            log.info(
                                "grounding_agent.downgraded",
                                rule_id=finding["rule_id"],
                                from_severity=old,
                                to_severity=new,
                            )

        log.info(
            "grounding_agent.done",
            cited=cited,
            flagged_missing=flagged_missing,
            downgraded=downgraded,
            total=len(all_findings),
        )
        return state

    # ---- public helper for UI consumers --------------------------------

    def cite(self, finding: Finding) -> list[Citation]:
        """Return structured Citation list for a single finding.

        Uses the rule's policy if a RuleSet was provided; otherwise defaults
        to soft mode with no source filter.
        """
        rule = self._rules_by_id.get(finding["rule_id"])
        policy = rule.citation if rule is not None else CitationPolicy()
        return self._cite(finding, policy)

    # ---- internals -----------------------------------------------------

    def _cite(self, finding: Finding, policy: CitationPolicy) -> list[Citation]:
        if self._top_k <= 0:
            return []
        query = _build_query(finding)
        if not query.strip():
            return []
        where = self._build_where(policy)
        chunks = self._store.search(query, k=self._top_k, where=where)
        filtered = [c for c in chunks if c.score >= self._min_score]
        return [self._to_citation(c) for c in filtered]

    @staticmethod
    def _build_where(policy: CitationPolicy) -> dict[str, Any] | None:
        if not policy.source_filter:
            return None
        sources = list(policy.source_filter)
        if len(sources) == 1:
            return {"source": sources[0]}
        return {"source": {"$in": sources}}

    def _to_citation(self, chunk: Chunk) -> Citation:
        return Citation(
            source=str(chunk.metadata.get("source", "?")),
            section=chunk.metadata.get("section"),
            page=chunk.metadata.get("page"),
            snippet=chunk.text[: self._snippet_chars].strip(),
            score=chunk.score,
        )

    def _format(self, citations: list[Citation]) -> str:
        """Human-readable citation string for `finding.citation`.

        Example: "BEP.pdf §4.2 p.12 (0.87) · IBC.pdf §711.2 p.45 (0.72)"
        """
        parts = []
        for c in citations:
            ref = [c.source]
            if c.section:
                ref.append(c.section)
            if c.page is not None:
                ref.append(f"p.{c.page}")
            ref.append(f"({c.score:.2f})")
            parts.append(" ".join(ref))
        return " · ".join(parts)


def _build_query(finding: Finding) -> str:
    """Construct a retrieval query from a finding.

    Combines the parameter name with rule_id tokens. Element id is excluded —
    it's high-entropy noise for retrieval.
    """
    parameter = finding.get("parameter") or ""
    rule_id = finding.get("rule_id") or ""
    rule_tokens = rule_id.replace(".", " ")
    return f"{parameter} {rule_tokens}".strip()
