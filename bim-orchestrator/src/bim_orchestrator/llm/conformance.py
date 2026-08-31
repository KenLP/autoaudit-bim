"""L2-13 — make the core↔plugin contract checkable instead of merely written down.

``llm/interfaces.py`` states the socket contract as ``Protocol`` classes, and
its own docstring says core does no runtime conformance check anywhere: "pure
duck typing". That was a deliberate choice, and it held for as long as the two
sides shipped together. They no longer do — v1.5-R3 moved the three agents into
a separate private package with its own repo, its own branch and its own
release cadence, and the 2026-07-26 Phase 2 review measured what that costs:

    a reviewer mutated the REAL plugin three ways — added a required keyword
    argument to a builder, deleted ``propose_batch``, changed a return shape —
    and the core suite stayed at 2052 passed with nothing moving, while all
    three are genuine crashes the first time a flag is on.

So "the core suite is green" said nothing at all about the plugin. This module
is what makes it say something. It is deliberately NOT ``isinstance`` against
the Protocols (those are structural and unenforceable at runtime for methods);
it inspects the exact call surface ``design.py`` / ``graph.py`` use, and can
exercise it against a scripted client.

Two levels, because they catch different drift:

* :func:`check_plugin_module` / :func:`check_agent` — STRUCTURAL, free, no
  model. Do the builders exist, do they accept the keywords core actually
  passes, do they demand any keyword core does NOT pass, are the agent methods
  present and awaitable? ``factory.py`` runs these at wire-up, so a drifted
  plugin degrades to the deterministic path with one log line instead of
  raising ``TypeError`` halfway through a run that has already written to Revit.
* :func:`check_agent_runtime` — BEHAVIOURAL, needs a client but no network
  (see :class:`ScriptedLLMClient`). Do the agents return the shapes core
  destructures, and does a remediation proposal that fails the injected
  validator actually come back as ``None``? Tests run this; wire-up does not,
  because it costs a construction per agent and the structural pass already
  covers the crash-shaped failures.

Both halves live in ``src/`` rather than ``tests/`` on purpose: the plugin's own
suite imports them, so ONE definition of the contract is checked from both
sides. A contract verified only by the party that wrote it is the situation
this module exists to end.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .client import LLMClient

# Bumped whenever the socket's call surface or return shapes change, so a log
# line can say WHICH contract a plugin failed rather than only that it did.
#   1 — v1.5-R3 split (propose / propose_batch -> dict[str, str] / run).
#   2 — L2-09 (2026-07-26): propose_batch entries must carry the current value
#       they were answering for, so a permuted batch answer is detectable.
PLUGIN_CONTRACT_VERSION = 2

# Exactly what ``llm/factory.py`` passes to each builder. A plugin builder that
# requires anything MORE than these is a TypeError waiting for the first run
# with a flag on.
_BUILDER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "make_remediation_agent": ("client", "ctx"),
    "make_diagnostic_agent": ("client", "rules", "ctx"),
    "make_supervisor_agent": ("client", "rules", "ctx"),
}

# Exactly what ``design.py`` / ``graph.py`` call on a BUILT agent:
#   kind -> {method: (positional args, keyword args)}
_AGENT_METHODS: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "remediation": {
        "propose": (("finding",), ("validate", "context", "safety_critical")),
        "propose_batch": (("items",), ("context",)),
    },
    "diagnostic": {"run": (("state",), ())},
    "supervisor": {"run": (("state",), ())},
}


def _check_callable(
    fn: Any,
    *,
    label: str,
    positional: Iterable[str],
    keywords: Iterable[str],
    awaited: bool = False,
) -> list[str]:
    """Can ``fn`` be called the way core calls it, and does it demand more?"""
    problems: list[str] = []
    if fn is None:
        return [f"{label}: missing"]
    if not callable(fn):
        return [f"{label}: not callable (got {type(fn).__name__})"]
    if awaited and not inspect.iscoroutinefunction(fn):
        problems.append(f"{label}: not an async def — core awaits it")
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C callables — nothing to check
        return problems

    params = list(sig.parameters.values())
    has_var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
    has_var_pos = any(p.kind is p.VAR_POSITIONAL for p in params)
    positional = tuple(positional)
    keywords = tuple(keywords)

    slots = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(slots) < len(positional) and not has_var_pos:
        problems.append(
            f"{label}: takes {len(slots)} positional arg(s), core passes "
            f"{len(positional)} ({', '.join(positional)})"
        )

    by_name = {p.name: p for p in params}
    for kw in keywords:
        p = by_name.get(kw)
        if p is None:
            if not has_var_kw:
                problems.append(f"{label}: does not accept keyword {kw!r}, which core passes")
            continue
        if p.kind is p.POSITIONAL_ONLY:
            problems.append(f"{label}: {kw!r} is positional-only; core passes it by keyword")

    passed = set(positional) | set(keywords)
    for p in params:
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        if p.name in passed or p.name == "self":
            continue
        if p.default is inspect.Parameter.empty:
            problems.append(
                f"{label}: requires {p.name!r}, which core never passes — "
                "every call from core would raise TypeError"
            )
    return problems


def check_plugin_module(module: Any) -> list[str]:
    """Structural check of the plugin package's three builders.

    Returns an empty list when the module is usable. Non-empty means core
    should NOT wire it up — every problem listed is a crash core would
    otherwise take mid-run.
    """
    problems: list[str] = []
    for name, keywords in _BUILDER_KEYWORDS.items():
        problems.extend(
            _check_callable(
                getattr(module, name, None), label=name, positional=(), keywords=keywords
            )
        )
    return problems


def check_agent(agent: Any, *, kind: str) -> list[str]:
    """Structural check of ONE built agent against the surface core calls.

    ``kind`` is ``"remediation"`` / ``"diagnostic"`` / ``"supervisor"``.
    ``agent is None`` is not a problem — that is the documented "disabled"
    answer, and core handles it everywhere.
    """
    if agent is None:
        return []
    spec = _AGENT_METHODS.get(kind)
    if spec is None:
        return [f"unknown agent kind {kind!r}"]
    problems: list[str] = []
    for method, (positional, keywords) in spec.items():
        problems.extend(
            _check_callable(
                getattr(agent, method, None),
                label=f"{kind}.{method}",
                positional=positional,
                keywords=keywords,
                awaited=True,
            )
        )
    return problems


# --------------------------------------------------------------------------
# Behavioural half
# --------------------------------------------------------------------------


def _instance_for(schema: Any, overrides: Mapping[str, Any]) -> Any:
    """Smallest value satisfying ``schema`` — a stand-in for a perfectly
    obedient model, so a contract test needs neither a network nor knowledge of
    the plugin's prompts. ``overrides`` substitute by PROPERTY NAME (so a test
    can make the model echo a specific ``element_id``)."""
    if not isinstance(schema, Mapping):
        return "x"
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        out: dict[str, Any] = {}
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props)
        for name in required:
            if name in overrides:
                out[name] = overrides[name]
            else:
                out[name] = _instance_for(props.get(name), overrides)
        return out
    if kind == "array":
        return [_instance_for(schema.get("items"), overrides)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return "x"


class ScriptedLLMClient(LLMClient):
    """An ``LLMClient`` that answers by synthesising a minimal instance of the
    schema it is handed — i.e. a model that always complies, with no network.

    Used only for contract verification: it lets both repos exercise the real
    agent code (prompt building, parsing, the repair loop's plumbing) without
    asserting anything about the prompts themselves, which are the plugin's
    private design and must stay that way.
    """

    model = "scripted"

    def __init__(self, *, overrides: Mapping[str, Any] | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, system: str, prompt: str, max_tokens: int = 512) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        return ""

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        out = _instance_for(schema, self.overrides)
        return out if isinstance(out, dict) else {"value": out}


def _batch_entry_problems(eid: Any, entry: Any) -> list[str]:
    """Contract v2: a batch entry must carry the value AND the current value it
    was answering for (see ``design.py:_prewarm_llm_batches`` — the echo is what
    makes a permuted batch answer detectable)."""
    if not isinstance(entry, Mapping):
        return [
            f"propose_batch[{eid!r}]: contract v{PLUGIN_CONTRACT_VERSION} expects a "
            f"mapping with 'value' + 'for_current_value', got {type(entry).__name__}"
        ]
    problems = []
    if not isinstance(entry.get("value"), str):
        problems.append(f"propose_batch[{eid!r}]: 'value' must be a str")
    if "for_current_value" not in entry:
        problems.append(
            f"propose_batch[{eid!r}]: missing 'for_current_value' — without the echo "
            "core cannot tell this answer apart from another element's"
        )
    return problems


async def check_agent_runtime(
    agent: Any, *, kind: str, state_factory: Callable[[], Any] | None = None
) -> list[str]:
    """Exercise a built agent and check the shapes core destructures.

    Catches the drift a signature cannot: a ``propose`` that returns a dict
    instead of an object with ``.proposed_value``, a ``propose_batch`` that
    returns a list, a supervisor that returns the directive unwrapped — and,
    most importantly, a remediation agent that returns a value its injected
    validator REJECTED (the P2-01 guarantee, checked from outside the plugin
    for the first time).
    """
    if agent is None:
        return []
    problems: list[str] = []
    if kind == "remediation":
        finding = {
            "element_id": "E1",
            "rule_id": "R1",
            "parameter": "Mark",
            "message": "contract probe",
        }
        rejected = await agent.propose(finding, validate=lambda _v: False)
        if rejected is not None:
            problems.append(
                "propose: returned a value its validator REJECTED — the closed-loop "
                "guarantee (P2-01) is the plugin's half of the neuro-symbolic split"
            )
        accepted = await agent.propose(finding, validate=lambda _v: True)
        if accepted is None:
            problems.append("propose: returned None for a value its validator accepted")
        else:
            if not isinstance(getattr(accepted, "proposed_value", None), str):
                problems.append("propose: result has no str .proposed_value")
            if getattr(accepted, "autonomy", None) not in ("approve", "human-only"):
                problems.append(
                    "propose: .autonomy must be 'approve' or 'human-only' — an "
                    "LLM-originated value is never 'auto'"
                )
        batch = await agent.propose_batch(
            [{"element_id": "E1", "current_value": "old"}], context={}
        )
        if not isinstance(batch, Mapping):
            problems.append(
                f"propose_batch: must return a mapping, got {type(batch).__name__}"
            )
        else:
            for eid, entry in batch.items():
                problems.extend(_batch_entry_problems(eid, entry))
    elif kind == "diagnostic":
        state = state_factory() if state_factory else {"findings": [], "iteration": 0}
        out = await agent.run(state)
        if not isinstance(out, Mapping):
            problems.append(f"run: must return the state mapping, got {type(out).__name__}")
    elif kind == "supervisor":
        state = state_factory() if state_factory else {"findings": [], "iteration": 0}
        out = await agent.run(state)
        if not isinstance(out, Mapping):
            problems.append(f"run: must return a mapping, got {type(out).__name__}")
        else:
            extra = set(out) - {"supervisor_directive"}
            if extra:
                problems.append(
                    f"run: returned {sorted(extra)} besides 'supervisor_directive' — "
                    "the advisory contract is one key (core drops the rest)"
                )
            directive = out.get("supervisor_directive")
            if not isinstance(directive, Mapping) or directive.get("action") not in (
                "continue",
                "stop",
            ):
                problems.append("run: 'supervisor_directive' needs action continue|stop")
    else:
        problems.append(f"unknown agent kind {kind!r}")
    return problems
