"""Phase 2 scaffold test — prove the LLM CLIENT seam (FakeLLMClient) in isolation.

Deterministic, offline. Keeps the Phase 1 test posture: no network, no model
download.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): this file used to also pin
``RemediationLLMAgent``'s own closed-loop guardrail (accepts/rejects a
proposal, safety-critical→human-only, LLM-failure degrades to None). That
agent moved to the private ``bim-orchestrator-llm`` plugin — its scaffold
tests now live there as
``tests/test_remediation_llm_scaffold.py``. What's left here needs no agent,
only the client seam.
"""

from __future__ import annotations

from bim_orchestrator.llm.fake import FakeLLMClient


async def test_fake_records_and_returns_canned():
    fake = FakeLLMClient(
        text_responses={"family": "VALUE: ADSK_Furniture_Table_Round | WHY: restructured"}
    )
    out = await fake.complete(system="s", prompt="rename this family ...")
    assert out.startswith("VALUE:")
    assert len(fake.calls_to("complete")) == 1
