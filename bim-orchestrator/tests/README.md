# Test layout

## Files

| File | What it tests |
|---|---|
| `_mocks.py` | `MockFormaMCPClient` — in-memory drop-in for `FormaMCPClient`, plus realistic `SAMPLE_ROOMS` + `SAMPLE_SUBTYPES` fixtures |
| `test_rules_engine.py` | Pure evaluators: `present_and_nonempty`, `positive_number`, `matches_regex`, `not_matches_regex`, `infer_from_name` |
| `test_qc_agent.py` | `QCAgent` rule application + both shipped rules YAML files (parameter_completeness, naming) |
| `test_query_agent.py` | `QueryAgent.run()` + `_attach_params` property flattener |
| `test_design_agent.py` | `DesignAgent` trust pipeline: dry-run → autonomy → execute, subtype filtering |
| `test_graph.py` | `build_graph()` convergence + max-iteration logic with fake agents |
| `test_integration.py` | **End-to-end** — real agents + real graph + real rules YAML, only MCP boundary mocked |
| `test_smoke.py` | Module-load smoke + autonomy YAML loading |

## Running

```bash
uv run pytest -v               # all tests
uv run pytest tests/test_integration.py -v   # just integration
uv run pytest -q --tb=no       # fast feedback loop
```

## LLM-in-tests policy

The runtime graph runs **no LLM** by default — `QueryAgent`, `QCAgent`,
and `DesignAgent` are all deterministic. The only optional LLM in the
graph is `GroundingAgent` (RAG citation enrichment, Phase 2). The
Streamlit Rule Builder tab calls the Anthropic API at edit time, not
at QC time, so the agent contracts under test stay LLM-free.

The previous `LLMQueryAgent` (free-text `--ask` flag) was removed in
as superseded by the Rule Builder. Its vcrpy cassettes and
fake-client tests were deleted with it.

## What's deterministic

- `QueryAgent` / `RevitQueryAgent` are pure MCP tool calls (no LLM)
- `QCAgent` is rule-based (pydantic + evaluators, no LLM)
- `DesignAgent` builds issue payloads from templates (no LLM)
- `GroundingAgent` (optional) does call the Anthropic API; covered by
  Phase 2 integration tests with mocked retrieval
- The orchestrator's autonomy resolver is pure YAML lookup

Real test coverage comes from:

1. Mocking the MCP boundary (this is where the network goes)
2. Running the full graph in-process against the mock
3. The deterministic agents do their actual work — no behaviour is faked

## Adding a new test file

If it tests a single function/class, name it `test_<module>.py` mirroring
`src/bim_orchestrator/<module>.py`. If it crosses module boundaries, put
it in `test_integration.py`.

Use `MockFormaMCPClient` from `tests._mocks` — do NOT spin up the real
MCP server in tests (slow, requires credentials, polluting audit log).
