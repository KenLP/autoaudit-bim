"""Environment-gated construction of the Phase 2 LLM agents (lazy plugin socket).

This is the single feature-flag layer that activates runtime LLM judgment.
Phase 1 behaviour is the default: with a flag unset (and no ``client``
injected), the corresponding ``make_*_agent`` returns ``None`` and
DesignAgent/graph never call a model (``llm_propose`` rules degrade to Path A
ACC issues; no Diagnostic/Supervisor node is added). So the deterministic
pipeline — and the full offline test posture — is unchanged unless an
operator explicitly opts in.

SPEC_LLM_PLUGIN_SPLIT (v1.5-R3, 2026-07-07): the 3 agent CLASSES
(``RemediationLLMAgent`` / ``DiagnosticAgent`` / ``SupervisorAgent`` — prompts,
JSON schemas, repair loop, batching, memoisation) no longer live in this repo.
They ship in a separate private extension package (installed editable into
this project's venv when you want the seam to do anything). This module is
the SOCKET: it owns the flags + shared client/budget plumbing (unchanged) and
lazily imports the extension ONLY when a flag is on (or a ``client`` is
injected directly, e.g. by a test) — see ``_load_plugin`` below and
``llm/interfaces.py`` for the Protocols the extension's agents satisfy. If the
extension isn't installed, every ``make_*_agent`` logs ``llm.plugin_missing``
and returns ``None`` — the run continues fully deterministic, never crashes.

Flags (independent — each agent can be toggled on its own):
  ``BIM_LLM_REMEDIATION``  truthy ("1"/"true"/"yes"/"on") → build agent #1.
  ``BIM_LLM_DIAGNOSTIC``   truthy → build the advisory agent #2.
  ``BIM_LLM_SUPERVISOR``   truthy → build the advisory loop-control agent #3.
  ``BIM_LLM_PROVIDER``     "anthropic" (default) | "ollama" | "fake" — backend selector.
  ``BIM_LLM_MODEL``        optional model id (provider-specific default below).
  ``BIM_LLM_BASE_URL``     optional Ollama base URL (default http://localhost:11434).

Cloud vs local is a single env switch (`BIM_LLM_PROVIDER=ollama`) — the agents,
the orchestrator, and the Rule Builder all go through `build_llm_client()`, so
no agent code changes when you move on-prem / air-gapped.

``BIM_LLM_PROVIDER=fake`` is the offline escape hatch: an empty ``FakeLLMClient``
whose ``complete_json`` raises ``LLMError`` and whose ``complete`` returns "". Every
agent already degrades on ``LLMError`` (Remediation → Path A, Diagnostic → skip,
Supervisor → continue), so flipping to ``fake`` when the venue Wi-Fi dies makes the
whole system fall back to the deterministic Phase-1 pipeline with the flags still on.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Any

import structlog

from .anthropic_client import AnthropicLLMClient
from .client import LLMClient, LLMError
from .conformance import PLUGIN_CONTRACT_VERSION, check_agent, check_plugin_module
from .interfaces import (
    DiagnosticAgentProtocol,
    RemediationAgentProtocol,
    SupervisorAgentProtocol,
)
from .ollama_client import OllamaLLMClient
from .usage import LLMRunContext, MeteredLLMClient, UsageRecorder

log = structlog.get_logger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
# P2-02 (2026-07-25 live review): pinned to the DATED snapshot, not the
# floating `claude-haiku-4-5` alias. A provider can re-point an alias at a new
# checkpoint, and every run afterwards behaves differently with nothing in the
# record to show why — a silent behaviour change is the one thing an audit tool
# must not ship. Override with BIM_LLM_MODEL if you deliberately want "latest".
# Not every model publishes a dated snapshot; where none exists the mitigation
# is the RECORDED model id (UsageRecorder.record's `model`), never a guessed
# date — inventing a snapshot id would be worse than the drift it hides.
_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
# Low P2 (docs/reviews/REVIEW_MULTI_2026-07-04.md): once any LLM agent is on,
# an operator who forgets to set BIM_LLM_MAX_CALLS previously got an UNLIMITED
# budget. Default it instead — this is a circuit breaker, not a UX nicety.
_DEFAULT_MAX_CALLS_WHEN_FLAG_ON = 200

# The private extension package's import name (not published — installed
# editable; see CLAUDE.md / this project's README "LLM extension" note).
_PLUGIN_MODULE = "bim_orchestrator_llm"


def _load_plugin(flag: str) -> ModuleType | None:
    """Import the private agent-runtime extension, or ``None`` if absent OR
    incompatible.

    Mirrors the ``rules_extractor`` optional-dependency posture (M14): a
    missing plugin is NOT an error, just a degrade-to-deterministic. ``flag``
    is only for the log line (which flag/caller triggered this attempt).

    L2-13 (2026-07-26 Phase 2 review): this function's promise of "never
    crashes" only ever covered ABSENCE — a clean ``ImportError``. Present-but-
    drifted was a different story: the plugin lives in its own repo with its own
    release cadence, and a builder that grew a required keyword (or lost one)
    raised ``TypeError`` from here, mid-run, on a run that may already have
    written to Revit. So the structural contract is now CHECKED, and a plugin
    that fails it is treated exactly like a missing one: one log line, degrade
    to the deterministic path, no crash and no silent half-wiring.
    """
    try:
        plugin = importlib.import_module(_PLUGIN_MODULE)
    except ImportError:
        log.warning(
            "llm.plugin_missing",
            flag=flag,
            hint=f"module {_PLUGIN_MODULE!r} not importable — install the "
            "extension package editable into this venv (see its own README "
            "for the exact command; not published, so it isn't a pinned dep)",
        )
        return None
    problems = check_plugin_module(plugin)
    if problems:
        log.error(
            "llm.plugin_incompatible",
            flag=flag,
            contract_version=PLUGIN_CONTRACT_VERSION,
            problems=problems,
            hint=f"{_PLUGIN_MODULE!r} is installed but does not match the socket "
            "contract (bim_orchestrator.llm.conformance) — refusing to wire it up; "
            "this run stays fully deterministic",
        )
        return None
    return plugin


def _max_calls_from_env() -> int | None:
    """Optional hard per-run LLM call budget (`BIM_LLM_MAX_CALLS`).

    NOTE this budget counts LOGICAL calls (one `complete`/`complete_json`
    invocation as the AGENT sees it), NOT raw HTTP requests or tokens: the
    Anthropic SDK's own retries (`max_retries`, see anthropic_client.py) and
    the structured→legacy fallback path (`complete_json` calling itself again
    on a structured-output failure) can each turn one logical call into 2-3
    network requests. Budget accordingly — it bounds "how many judgment calls
    did the loop make", not "how many dollars/tokens did this cost".

    Invalid / non-positive env value → no budget (explicit `0` or garbage is
    treated as "don't cap", same as before). Unset (`""`) → ``None`` here; the
    default-200-when-a-flag-is-on behaviour is applied by the caller
    (`build_llm_run_context`), NOT here, so this function still means "what did
    the user ask for" and stays easy to unit-test in isolation.
    """
    raw = os.environ.get("BIM_LLM_MAX_CALLS", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        log.warning("llm.bad_max_calls", value=raw)
        return None
    if n <= 0:
        # L-15: `0` reads as "allow nothing" to most people, and this switch is
        # surfaced in the AuditHub settings UI where someone will type it. The
        # semantics stay as they are — an explicit non-positive value means NO
        # cap, the same as it has always meant, and flipping that would both
        # break anyone relying on it and turn a typo into a run that dies on
        # its first model call (UsageRecorder raises at zero). What changes is
        # that it stops happening in silence: a garbage value already warned,
        # while `0` — the one people actually type — went through mute.
        # To forbid model calls entirely, leave the BIM_LLM_* agent flags off;
        # that is the supported "off", and it is the default.
        log.error(
            "llm.max_calls_non_positive",
            value=raw,
            effect="no budget cap applied",
            hint="a non-positive budget means UNLIMITED here, not zero calls; "
            "to disable the LLM seam, unset the BIM_LLM_* agent flags instead",
        )
        return None
    return n


def any_llm_agent_enabled() -> bool:
    return (
        llm_remediation_enabled()
        or llm_diagnostic_enabled()
        or llm_supervisor_enabled()
    )


def build_llm_run_context() -> LLMRunContext | None:
    """One shared client + usage recorder for a run, built ONLY when some LLM
    agent is enabled. Returns None in pure Phase-1 runs (no accounting overhead).
    Every ``make_*_agent`` below takes this ``ctx`` so all three agents share the
    single client + budget and roll up into one per-run usage summary."""
    if not any_llm_agent_enabled():
        return None
    max_calls = _max_calls_from_env()
    if max_calls is None and not os.environ.get("BIM_LLM_MAX_CALLS", "").strip():
        # Flag is on and the operator did not set a budget at all → default
        # 200 rather than unlimited (see _DEFAULT_MAX_CALLS_WHEN_FLAG_ON above).
        # An explicit-but-invalid value (e.g. "0" or "abc") still means "no
        # budget" verbatim — only a genuinely UNSET env applies the default.
        max_calls = _DEFAULT_MAX_CALLS_WHEN_FLAG_ON
    return LLMRunContext(
        client=build_llm_client(),
        recorder=UsageRecorder(max_calls=max_calls),
    )


_FALSY = {"0", "false", "no", "n", "off", "disable", "disabled"}
# L2-12: values an operator plainly means as "on" that `_TRUTHY` rejected.
# Widening the set is half the fix; the other half is that anything OUTSIDE
# both sets is now said out loud instead of quietly meaning "off".
_TRUTHY_EXTRA = {"y", "t", "enable", "enabled"}

_AGENT_FLAGS = ("BIM_LLM_REMEDIATION", "BIM_LLM_DIAGNOSTIC", "BIM_LLM_SUPERVISOR")


def _enabled(env_var: str) -> bool:
    """Is this agent flag on?

    Unset is off — the documented default and the whole Phase-1 posture. An
    UNRECOGNISED value is also off, because off is the safe direction here:
    unlike ``BIM_LLM_PROVIDER``, where the wrong answer ships data to a third
    party and L2-04 made it raise, the wrong answer here is a correct
    deterministic audit. What changed is that it stops happening in silence —
    ``BIM_LLM_REMEDIATION=y`` used to produce a run indistinguishable from
    Phase 1 with nothing anywhere saying why.
    """
    raw = os.environ.get(env_var, "").strip().lower()
    if not raw:
        return False
    if raw in _TRUTHY or raw in _TRUTHY_EXTRA:
        return True
    if raw in _FALSY:
        return False
    log.error(
        "llm.bad_flag",
        flag=env_var, value=raw,
        hint="unrecognised value — treating as OFF, so this run stays "
        f"deterministic. On: {', '.join(sorted(_TRUTHY | _TRUTHY_EXTRA))}. "
        f"Off: {', '.join(sorted(_FALSY))}.",
    )
    return False


def llm_flag_problems() -> list[dict[str, str]]:
    """Agent flags set to something this module does not recognise.

    Returned rather than only logged, so the run record can carry it: the R7
    rule applies here too — an operator who asked for Phase 2 and got Phase 1
    should be able to learn that from the artifact, not only from a log line
    that scrolled past.
    """
    problems: list[dict[str, str]] = []
    known = _TRUTHY | _TRUTHY_EXTRA | _FALSY
    for flag in _AGENT_FLAGS:
        raw = os.environ.get(flag, "").strip().lower()
        if raw and raw not in known:
            problems.append({"flag": flag, "value": raw})
    return problems


def llm_remediation_enabled() -> bool:
    return _enabled("BIM_LLM_REMEDIATION")


def llm_diagnostic_enabled() -> bool:
    return _enabled("BIM_LLM_DIAGNOSTIC")


def llm_supervisor_enabled() -> bool:
    return _enabled("BIM_LLM_SUPERVISOR")


# L2-04: the only providers that exist. Anything else is a typo, and a typo on
# THIS switch used to fall through to the cloud.
_PROVIDERS = ("anthropic", "ollama", "fake")


def llm_provider() -> str:
    """The configured backend. Unset means ``anthropic`` (documented default);
    an UNRECOGNISED value raises (L2-04).

    This switch is not just a backend selector — it is the data-egress control.
    `ollama` is how an operator keeps element names, parameter values and
    NDA'd spec text on their own hardware, and `fake` is the offline escape
    hatch for a venue with no network. Falling through to the cloud on
    `ollamma` / `local` / `fakee` sent that data to a third-party API with
    nothing on screen to notice, and the failure is not recoverable once the
    request has left. So this fails CLOSED: the operator gets a loud error and
    fixes one character, instead of silently getting the thing they were
    configuring their way out of.
    """
    raw = os.environ.get("BIM_LLM_PROVIDER", "").strip().lower()
    if not raw:
        return "anthropic"
    if raw not in _PROVIDERS:
        raise LLMError(
            f"BIM_LLM_PROVIDER={raw!r} is not a known provider "
            f"({', '.join(_PROVIDERS)}). Refusing to fall back to the cloud: "
            "on a switch that controls where your model data goes, a typo must "
            "not silently pick the remote option."
        )
    return raw


def build_llm_client() -> LLMClient:
    """Construct the configured backend (cloud Anthropic or local Ollama).

    The single place provider selection lives — every agent factory and the
    Rule Builder call this, so flipping `BIM_LLM_PROVIDER=ollama` moves the whole
    system on-prem with no code change.
    """
    provider = llm_provider()
    model = os.environ.get("BIM_LLM_MODEL", "").strip()
    if provider == "fake":
        from .fake import FakeLLMClient

        log.info("llm.provider", provider="fake", note="offline escape hatch — agents degrade to Phase-1")
        return FakeLLMClient()
    if provider == "ollama":
        base_url = os.environ.get("BIM_LLM_BASE_URL", "").strip() or "http://localhost:11434"
        kwargs = {"base_url": base_url}
        if model:
            kwargs["model"] = model
        log.info("llm.provider", provider="ollama", model=model or "(default)", base_url=base_url)
        return OllamaLLMClient(**kwargs)  # type: ignore[arg-type]
    log.info("llm.provider", provider="anthropic", model=model or _DEFAULT_ANTHROPIC_MODEL)
    return AnthropicLLMClient(model=model or _DEFAULT_ANTHROPIC_MODEL)


def _metered(
    client: LLMClient | None, ctx: LLMRunContext | None, agent: str
) -> LLMClient | None:
    """L2-11: an injected ``client`` must still be counted against the run budget.

    The plugin's builders take ``client`` OR ``ctx`` and, when handed a
    ``client``, use it verbatim — dropping the metering wrapper. So a caller that
    injects a client (the service and the Streamlit surfaces do; tests always
    do) got an agent that issued unbudgeted, unattributed calls. Worse, the
    bypass is QUIETER than the correct path: the recorder sees zero calls, so
    ``format_line()`` returns ``None`` and nothing is printed at all — the run
    looks like it never asked a model.

    Core resolves this itself rather than asking the plugin to, because the
    budget is core's circuit breaker and this whole review round is about what
    core enforces versus what it trusts. ``ctx`` absent → nothing to meter
    against; ``client`` absent → the plugin builds from ``ctx`` as before.
    """
    if client is None or ctx is None:
        return client
    return MeteredLLMClient(inner=client, recorder=ctx.recorder, agent=agent)


def _checked(agent: Any, *, kind: str, flag: str) -> Any:
    """Structural check of a BUILT agent; ``None`` (degrade) if it doesn't match.

    The module-level check in ``_load_plugin`` covers the builders; this covers
    what they return — a renamed or deleted method (``propose_batch`` was the
    reviewer's example) only shows up on the instance.
    """
    problems = check_agent(agent, kind=kind)
    if problems:
        log.error(
            "llm.plugin_agent_incompatible",
            flag=flag, kind=kind,
            contract_version=PLUGIN_CONTRACT_VERSION,
            problems=problems,
            hint="built agent does not match the socket contract — degrading to "
            "the deterministic path rather than crashing mid-run",
        )
        return None
    return agent


def _build(builder: Any, *, kind: str, flag: str, **kwargs: Any) -> Any:
    """Call a plugin builder defensively and conformance-check what comes back.

    A plugin builder that raises used to abort the whole run from inside a
    factory whose documented answer to "not available" is ``None``.
    """
    try:
        agent = builder(**kwargs)
    except Exception as exc:  # degrade, never abort the audit
        log.error(
            "llm.plugin_build_failed",
            flag=flag, kind=kind, error=str(exc),
            hint="plugin builder raised — this run stays deterministic",
        )
        return None
    return _checked(agent, kind=kind, flag=flag)


def make_remediation_agent(
    *, client: LLMClient | None = None, ctx: LLMRunContext | None = None
) -> RemediationAgentProtocol | None:
    """Return the plugin's configured remediation agent, or ``None``.

    ``None`` when: disabled (no ``client`` injected and ``BIM_LLM_REMEDIATION``
    is unset) — no import attempted, matching Phase 1 exactly; OR the flag/
    client path is live but the ``bim_orchestrator_llm`` extension isn't
    installed (logs ``llm.plugin_missing``, degrades to Path A, never crashes).

    The actual agent (prompt/schema/repair-loop design) lives in that private
    extension — this is only the socket: gate, lazily import, delegate
    construction to the extension's own ``make_remediation_agent`` with the
    SAME args callers already pass (``client`` bypasses the flag, exactly as
    before the split — test injection doesn't need the flag on).
    """
    if client is None and not llm_remediation_enabled():
        return None
    plugin = _load_plugin("BIM_LLM_REMEDIATION")
    if plugin is None:
        return None
    return _build(
        plugin.make_remediation_agent,
        kind="remediation",
        flag="BIM_LLM_REMEDIATION",
        client=_metered(client, ctx, "remediation"),
        ctx=ctx,
    )


def make_diagnostic_agent(
    *,
    client: LLMClient | None = None,
    rules: object | None = None,
    ctx: LLMRunContext | None = None,
) -> DiagnosticAgentProtocol | None:
    """Return the plugin's configured (advisory) diagnostic agent, or ``None``.

    Same socket pattern as ``make_remediation_agent`` — see its docstring.
    ``rules`` (a RuleSet) is forwarded verbatim for rule description /
    requirement context in the plugin's explanations.
    """
    if client is None and not llm_diagnostic_enabled():
        return None
    plugin = _load_plugin("BIM_LLM_DIAGNOSTIC")
    if plugin is None:
        return None
    return _build(
        plugin.make_diagnostic_agent,
        kind="diagnostic",
        flag="BIM_LLM_DIAGNOSTIC",
        client=_metered(client, ctx, "diagnostic"),
        rules=rules,
        ctx=ctx,
    )


def make_supervisor_agent(
    *,
    client: LLMClient | None = None,
    rules: object | None = None,
    ctx: LLMRunContext | None = None,
) -> SupervisorAgentProtocol | None:
    """Return the plugin's configured (advisory) supervisor agent, or ``None``.

    Same socket pattern as ``make_remediation_agent`` — see its docstring.
    ``rules`` lets the plugin's agent know which findings are auto-fixable.
    """
    if client is None and not llm_supervisor_enabled():
        return None
    plugin = _load_plugin("BIM_LLM_SUPERVISOR")
    if plugin is None:
        return None
    return _build(
        plugin.make_supervisor_agent,
        kind="supervisor",
        flag="BIM_LLM_SUPERVISOR",
        client=_metered(client, ctx, "supervisor"),
        rules=rules,
        ctx=ctx,
    )
