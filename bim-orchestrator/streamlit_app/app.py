"""v1 task UI: Streamlit POC for the BIM Orchestrator.

Six tabs mirror the BIM Manager workflow end-to-end:

  Rule Builder Describe a compliance requirement in plain language → AI
               (Claude API) extracts a rule draft → editable form →
               save as config/rules.<scenario>.yaml ready for QC.
  Setup        Pick Forma project + model, configure rules + run options.
  Run          Trigger orchestrator CLI (check / apply / run / run-revit),
               stream stdout live, surface connection-status badges.
  Results      4-state outcomes summary + filterable findings table loaded
               from the most-recent (or selected) runs/run-<id>/outcomes.json.
  Trend        Compliance % over recent runs + cross-run diff cards from
               run_recorder.diff_outcomes; mirrors runs/trend.md.
  Run History  Table of every run from list_runs(); click a row to load
               that run's Results.

The app is a thin presentation layer over the existing CLI -- it spawns
the orchestrator as a subprocess so no orchestration code is duplicated
here. All Streamlit-specific code stays in this folder; the bim_orchestrator
package never imports streamlit.

Launch:
    cd bim-orchestrator
    uv run streamlit run streamlit_app/app.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# Load .env BEFORE any st.session_state defaults read os.environ -- the
# orchestrator CLI does this internally on every invocation, but the
# Streamlit script is a separate process that has to opt in. Without
# this, the Setup tab IDs would silently appear blank even when .env
# is correctly populated (visible bug reported during Stage 1 dogfood
# prep on 2026-06-02). load_dotenv(override=False) preserves any
# already-set process-env values (e.g. when launched via `BIM_LOG_FORMAT=json
# streamlit run ...`).
_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_DOTENV_PATH if _DOTENV_PATH.exists() else None, override=False)

# ── Module-level constants ──────────────────────────────────────────────────

# Where this app lives -- used for relative file resolution + subprocess cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
CONFIG_DIR = REPO_ROOT / "config"
# Optional PRIVATE advanced rule library (gitignored — not in the public AU share).
# Self-contained packs (rules + their reference/lookup data); the engine resolves a
# pack's data relative to its rules file, falling back to config/ for shared tables.
RULE_LIBRARY_DIR = REPO_ROOT / "rule-library"
RULE_BUILDER_HISTORY_PATH = RUNS_DIR / "rule_builder_history.jsonl"

# How long the most-recent-run cache stays warm before list_runs() refetches.
RUNS_CACHE_TTL_SECONDS = 5


# ── Page config (must be first Streamlit call) ──────────────────────────────

st.set_page_config(
    page_title="BIM Orchestrator",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Session-state defaults ──────────────────────────────────────────────────

# Centralised defaults so every tab reads the same shape. Each is the
# canonical "blank" value -- tabs read from session_state and never mutate
# these defaults in place.
_DEFAULTS = {
    # Setup tab outputs
    "hub_id": os.environ.get("DEMO_HUB_ID", ""),
    "project_id": os.environ.get("DEMO_PROJECT_ID", ""),
    "aecdm_project_id": os.environ.get("DEMO_AECDM_PROJECT_ID", ""),
    "element_group_id": os.environ.get("DEMO_ELEMENT_GROUP_ID", ""),
    # Display names resolved from Forma API (or left blank when not yet fetched)
    "project_name": "",
    "element_group_name": "",
    # v1.4-K22: start with NO rule selected — the user picks explicitly (else the
    # Setup tab silently showed a default scenario's rules as "4 rule sẽ chạy").
    "rules_path": "",
    # v1.4-K6 multi-scenario: the full selection (1+ YAMLs merged into one run).
    # `rules_path` stays the single primary (= first entry) for back-compat
    # displays; `rules_paths` is the source of truth the argv builder emits.
    "rules_paths": [],
    "run_mode": "run-revit",        # apply | run-revit (Check dropped — v1.4-K18)
    # v1.4-K22: max ACC ISSUES per run (proposal issues + Path A groups). Now that
    # each issue groups by rule and lists ALL its elements, a handful of issues
    # covers a run; 5 is a sane default (0 = ∞). Was 25.
    "limit": 5,
    "dry_run": False,
    "max_iterations": 1,            # --max-iterations (single-pass converge OK for room compliance)
    # v1.4-K14: default to CREATING issues. apply/run-revit modes are named
    # "→ ACC Issues" / "+ ACC Issues" so issues (incl. approve-gated proposal
    # issues for Path B) are the expected behavior. Off-by-default used to leave
    # the Approvals inbox silently empty. The rare no-ACC project flips it back
    # on in Setup → Advanced.
    "no_forma": False,              # --no-forma (only for projects with no linked ACC workspace)
    # Run tab outputs
    "last_run_id": None,
    "is_running": False,
    # M9: live Popen of an in-flight orchestrator subprocess (or None). Guards
    # against a second concurrent launch writing into the same Revit document.
    "_active_run_proc": None,
    # Results tab selection
    "selected_run_id": None,        # None -> most recent
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Module-level helpers (must be defined before sidebar runs) ───────────────


def _detect_vendor_path(server_name: str) -> str | None:
    """Return vendor/<server_name> if dist/index.js or forma-mcp.exe exists there."""
    candidate = REPO_ROOT / "vendor" / server_name
    if (candidate / "dist" / "index.js").exists():
        return str(candidate)
    if (candidate / "forma-mcp.exe").exists():
        return str(candidate)
    return None


def _write_env_values(values: dict[str, str]) -> None:
    """Upsert key=value pairs in the project .env file.

    Creates .env from .env.example if it doesn't exist. Preserves comments
    and blank lines. Appends any new keys that weren't already present.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        example = REPO_ROOT / ".env.example"
        if example.exists():
            shutil.copy(example, env_path)
        else:
            env_path.touch()
    lines = env_path.read_text(encoding="utf-8").splitlines()
    written: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in values:
            result.append(f"{key}={values[key]}")
            written.add(key)
        else:
            result.append(line)
    for key, val in values.items():
        if key not in written:
            result.append(f"{key}={val}")
    env_path.write_text("\n".join(result) + "\n", encoding="utf-8")
    load_dotenv(dotenv_path=env_path, override=True)


@st.cache_data(ttl=300)
def _sync_remote_rules() -> list[str]:
    """Fetch rule YAMLs from RULES_REMOTE_MANIFEST if configured.

    TTL-cached for 5 minutes. Returns list of updated scenario names, or []
    on error / when RULES_REMOTE_MANIFEST is not set.
    Manifest format: {"rules": [{"scenario": "name", "url": "https://..."}]}
    """
    manifest_url = os.environ.get("RULES_REMOTE_MANIFEST", "").strip()
    if not manifest_url:
        return []
    try:
        import httpx
        resp = httpx.get(manifest_url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        updated: list[str] = []
        for entry in resp.json().get("rules", []):
            scenario = entry.get("scenario", "")
            url = entry.get("url", "")
            if not scenario or not url:
                continue
            r = httpx.get(url, timeout=10.0, follow_redirects=True)
            r.raise_for_status()
            (CONFIG_DIR / f"rules.{scenario}.yaml").write_text(r.text, encoding="utf-8")
            updated.append(scenario)
        return updated
    except Exception:
        return []


# ── Sidebar ─────────────────────────────────────────────────────────────────

def _recent_runs(n: int = 3) -> list[tuple[str, str]]:
    """Return (run_id, started_at) for the N most recent runs in runs/.

    Used by the sidebar quick-link list. Reads metadata.json from each
    folder; tolerates missing/corrupt metadata by skipping the entry.
    """
    if not RUNS_DIR.exists():
        return []
    rows: list[tuple[str, str]] = []
    for folder in RUNS_DIR.iterdir():
        if not folder.is_dir() or not folder.name.startswith("run-"):
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append((folder.name, meta.get("started_at") or ""))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:n]


def _resolve_rules_selection() -> list[str]:
    """The active rules-YAML selection: ``rules_paths`` (v1.4-K6 multi-select,
    the source of truth) falling back to the legacy single ``rules_path`` key.

    Shared by the sidebar status badge and the Run tab's pre-flight check so
    a session that only ever set the legacy key (pre-K6, or restored from an
    older save) shows as "selected" in BOTH places instead of the sidebar
    alone reporting "chưa chọn" while Run tab's pre-flight passed (Low,
    REVIEW_MULTI M11).
    """
    return [
        p for p in (
            st.session_state.get("rules_paths")
            or [st.session_state.get("rules_path") or ""]
        )
        if p
    ]


def _pending_approvals_count() -> int:
    """Number of not-yet-applied proposal records in runs/approvals/.

    Sidebar badge only — tolerates a missing dir / corrupt record by skipping.
    """
    approvals_dir = RUNS_DIR / "approvals"
    if not approvals_dir.exists():
        return 0
    n = 0
    for p in approvals_dir.glob("*.json"):
        try:
            if not json.loads(p.read_text(encoding="utf-8")).get("applied"):
                n += 1
        except (OSError, ValueError):
            continue
    return n


@st.cache_data(ttl=60)
def _probe_autoaudit_service() -> bool:
    """P3-4: is the AuditHub service (:8601, `autoaudit-service`) running?

    Sidebar badge only — display, no logic. TTL-cached so the sidebar
    rerender doesn't hit the network on every Streamlit interaction; any
    failure (service down, connection refused, timeout) reads as "not
    running", never raises into the UI.
    """
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:8601/health", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


# Sidebar-local mode labels (the full labels live inside render_setup_tab).
_SIDEBAR_MODE_LABELS = {
    "apply": "📋 Apply — Forma",
    "run-revit": "⚙️ Full run — Revit",
}

with st.sidebar:
    st.title("🏗️ BIM Orchestrator")
    st.caption("Multi-agent BIM QA")
    st.divider()

    # ── Live workflow status ────────────────────────────────────────────────
    # UX: ONE at-a-glance checklist replaces the old trio of red error +
    # yellow warning + stale 5-item tab list (the app has 7 tabs; Rule Builder
    # and Approvals were missing, order was wrong).
    st.subheader("Trạng thái")

    _proj_name = st.session_state.get("project_name", "")
    _proj_id = st.session_state.get("project_id", "")
    _proj_disp = _proj_name or (_proj_id[:20] + "…" if len(_proj_id) > 20 else _proj_id)
    _eg_name = st.session_state.get("element_group_name", "")
    _eg_id = st.session_state.get("element_group_id", "")
    _eg_disp = _eg_name or (_eg_id[:20] + "…" if len(_eg_id) > 20 else _eg_id)
    _rules_sel = _resolve_rules_selection()
    _mode_disp = _SIDEBAR_MODE_LABELS.get(
        st.session_state.get("run_mode", ""), st.session_state.get("run_mode", "?")
    )

    if _proj_disp:
        st.markdown(f"✅ **Project:** {_proj_disp}")
    else:
        st.markdown("⚠️ **Project:** chưa chọn → tab **⚙️ Setup**")
    if st.session_state.get("run_mode") == "apply":
        # Model (element group) only matters for the Forma-query mode.
        if _eg_disp:
            st.markdown(f"✅ **Model:** {_eg_disp}")
        else:
            st.markdown("⚠️ **Model:** chưa chọn (cần cho Apply)")
    if _rules_sel:
        st.markdown(f"✅ **Rules:** {len(_rules_sel)} file")
    else:
        st.markdown("⚠️ **Rules:** chưa chọn → **📋 Rule Builder** / **⚙️ Setup**")
    st.markdown(f"▫️ **Chế độ:** {_mode_disp}")

    _n_pending = _pending_approvals_count()
    if _n_pending:
        st.markdown(f"🕓 **{_n_pending} đề xuất chờ duyệt** → tab **📥 Approvals**")

    # QW-2: recent runs quick-pick. Clicking a button writes the run_id
    # into selected_run_id; Results tab reads this on its next render.
    st.subheader("Run gần đây")
    recent = _recent_runs(3)
    if not recent:
        st.caption("(chưa có run nào)")
    else:
        for run_id, started in recent:
            label = f"📂 `{run_id}`"
            if started:
                # Show just the time portion to keep the button compact
                label += f"  {started[11:19]}"
            if st.button(label, key=f"sidebar_run_{run_id}", use_container_width=True):
                st.session_state["selected_run_id"] = run_id
                st.toast(f"Đã chọn `{run_id}` — mở tab Results để xem.")

    # Remote rules sync status (TTL-cached, non-blocking)
    synced = _sync_remote_rules()
    if synced:
        st.info(f"📡 Rules synced: {', '.join(synced)}")
    elif os.environ.get("RULES_REMOTE_MANIFEST"):
        st.caption("📡 Remote rules: up to date")

    st.divider()
    if _probe_autoaudit_service():
        st.caption("AutoAudit service: ●")
    else:
        st.caption("AutoAudit service: ○ not running")
    st.caption(
        "Quy trình: **📋 Rule Builder** (tạo rule) → **⚙️ Setup** (chọn project + rules) "
        "→ **▶️ Run** → **📊 Results** → **📥 Approvals** (duyệt auto-fix) "
        "· theo dõi: **📈 Trend** / **📜 Run History**"
    )


# ── Tabs ────────────────────────────────────────────────────────────────────


def _list_rules_yamls(*, include_advanced: bool = False) -> list[Path]:
    """Return rules.*.yaml files NEWEST first (v1.4-K17).

    Always the public ``config/`` rules; when ``include_advanced`` (the Setup
    toggle), also the PRIVATE ``rule-library/`` packs. Most-recently-modified at
    the top so the rule you just saved in the Rule Builder is the first option.
    """
    found: list[Path] = []
    if CONFIG_DIR.exists():
        found += CONFIG_DIR.glob("rules.*.yaml")
    if include_advanced and RULE_LIBRARY_DIR.exists():
        found += RULE_LIBRARY_DIR.glob("rules.*.yaml")
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)



def _test_forma_connection() -> tuple[bool, str]:
    """Run the existing CLI smoke test (bim-orchestrator --hello).

    Returns (ok, log_text). Spawning the CLI avoids importing asyncio
    machinery into the Streamlit script, which gets re-executed on every
    interaction and would leak event loops.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "bim_orchestrator.orchestrator", "--hello"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # H7: force UTF-8 both ways. The CLI prints emoji / Vietnamese; on a fresh
        # Windows box the child defaults to cp1252 and crashes on the first non-ASCII
        # byte (the cp1252 gotcha already fixed elsewhere), and text-mode decoding
        # here would raise UnicodeDecodeError too. PYTHONIOENCODING fixes the child;
        # encoding+errors makes our decode robust regardless.
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, log


def _test_revit_connection() -> tuple[bool, str]:
    """Probe the Revit addin HTTP server at 127.0.0.1:{port}/health.

    Uses synchronous httpx — no asyncio needed. Returns (ok, message).
    Auth token is auto-loaded from %APPDATA% token file or REVIT_MCP_AUTH_TOKEN.
    """
    import httpx

    version = os.environ.get("REVIT_MCP_VERSION", "2026")
    try:
        port = int(os.environ.get("REVIT_MCP_PORT") or (7891 + max(0, int(version) - 2026)))
    except ValueError:
        port = 7891

    token = os.environ.get("REVIT_MCP_AUTH_TOKEN", "").strip()
    if not token:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            token_file = (
                Path(appdata) / "Autodesk" / "Revit" / "Addins" / version / "revit-mcp-token.txt"
            )
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass

    url = f"http://127.0.0.1:{port}/health"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        svc = data.get("service", "revit-mcp-addin")
        ver = data.get("version", "?")
        auth_on = data.get("authEnabled", True)
        return True, f"{svc} v{ver} · port {port} · auth {'on' if auth_on else 'off'}"
    except httpx.ConnectError:
        return False, f"No response at {url} — open Revit with RevitMCPServer addin loaded."
    except httpx.HTTPStatusError as exc:
        return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def _get_forma_mcp_dir() -> str | None:
    """Return the resolved CWD for the forma-mcp subprocess, or None if not found.

    Accepts either the SEA exe (forma-mcp.exe) or the Node dist/index.js layout.
    Both use the vendor/forma-mcp/ dir as cwd so dotenv finds .env there.
    """
    env_cwd = os.environ.get("FORMA_MCP_SERVER_CWD", "").strip()
    if env_cwd:
        return env_cwd
    candidate = REPO_ROOT / "vendor" / "forma-mcp"
    if (candidate / "forma-mcp.exe").exists() or (candidate / "dist" / "index.js").exists():
        return str(candidate)
    return None


def _write_forma_env(forma_dir: str, values: dict[str, str]) -> None:
    """Upsert key=value pairs in the forma-mcp .env (NOT the orchestrator .env).

    Writes to {forma_dir}/.env so dotenv loads them when the subprocess
    launches from that CWD. Preserves existing keys not in `values`.
    """
    env_path = Path(forma_dir) / ".env"
    if not env_path.exists():
        example = Path(forma_dir) / ".env.example"
        env_path.write_text(
            example.read_text(encoding="utf-8") if example.exists() else "",
            encoding="utf-8",
        )
    lines = env_path.read_text(encoding="utf-8").splitlines()
    written: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in values:
            result.append(f"{key}={values[key]}")
            written.add(key)
        else:
            result.append(line)
    for key, val in values.items():
        if key not in written:
            result.append(f"{key}={val}")
    env_path.write_text("\n".join(result) + "\n", encoding="utf-8")


def _parse_mcp_name_id_list(raw: object) -> list[tuple[str, str]]:
    """Parse AECDM list tool response into [(id, name)] pairs.

    AECDM tools (list_aecdm_projects, list_element_groups, etc.) return a
    list[TextContent] where each item's .text is a human-readable plain-text
    table, NOT JSON. Live format observed (2026-06-11):

        Found N AEC project(s):

        🏢 Project Name  (ID: urn:adsk.workspace:prod.project:<uuid>)

    We parse the ``(ID: <value>)`` pattern and use the preceding text on the
    same line as the display name (emoji stripped).
    """
    import re

    id_re = re.compile(r'\(ID:\s*([^)]+)\)')
    # Strip leading emoji (Unicode block) + whitespace from name portion
    emoji_re = re.compile(r'^[\U00010000-\U0010ffff\U00002600-\U000027ff\s\xa0]+')

    pairs: list[tuple[str, str]] = []
    items_raw = raw if isinstance(raw, list) else [raw]
    for item in items_raw:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        for line in text.splitlines():
            m = id_re.search(line)
            if not m:
                continue
            eid = m.group(1).strip()
            name_part = line[:m.start()]
            name_part = emoji_re.sub("", name_part).strip().rstrip()
            if not name_part:
                name_part = eid[:40]
            pairs.append((eid, name_part))
    return pairs


def _parse_aecdm_projects(raw: object) -> list[tuple[str, str, str]]:
    """Parse ``aecdm_list_projects`` into ``[(aecdm_id, dm_id, name)]``.

    Forma's dual-id format (2026-06-19) returns BOTH project ids per project,
    so the picker resolves the AECDM URN (for element queries) AND the
    DM/Issues id (``b.<uuid>``, for issues_* / dm_* / reviews_*) from ONE call —
    no name-matching, no hard-coded id. Live shape::

        • <name>
            AECDM id: urn:adsk.workspace:prod.project:<uuid>
            DM/Issues id: b.<uuid>

    ``dm_id`` is ``""`` when a project has no linked DM/Issues container.
    """
    items_raw = raw if isinstance(raw, list) else [raw]
    out: list[tuple[str, str, str]] = []
    name: str | None = None
    aecdm_id = ""
    dm_id = ""

    def _flush() -> None:
        nonlocal name, aecdm_id, dm_id
        if name and aecdm_id:
            out.append((aecdm_id, dm_id, name))
        name, aecdm_id, dm_id = None, "", ""

    for item in items_raw:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("•"):                 # • new project block
                _flush()
                name = s.lstrip("•").strip()
            elif s.startswith("AECDM id:"):
                aecdm_id = s.split(":", 1)[1].strip()
            elif s.startswith("DM/Issues id:"):
                dm_id = s.split(":", 1)[1].strip()
    _flush()
    return out


# UX: hard wall-clock cap on the Forma browse calls. The MCP handshake can hang
# indefinitely (bad creds / server waiting on network) and, because st.tabs runs
# every tab top-down in ONE script, a hung spinner here freezes the WHOLE app —
# including tabs that don't need Forma at all.
_FORMA_BROWSE_TIMEOUT_S = 20.0
_FORMA_TIMEOUT_MSG = (
    f"Forma MCP không phản hồi sau {_FORMA_BROWSE_TIMEOUT_S:.0f}s. "
    "Kiểm tra 🔑 credentials + 🔌 MCP Server Paths bên dưới, "
    "hoặc nhập ID thủ công (✏️)."
)


def _call_with_hard_timeout(fn, timeout_s: float):
    """Run ``fn()`` in a daemon thread; raise TimeoutError if it overruns.

    Second line of defence behind asyncio.wait_for: if the MCP client's
    cancellation/cleanup itself hangs (subprocess refusing to die), the worker
    thread is abandoned (leaks until process exit) but the UI thread returns.

    Low (REVIEW_MULTI M11): the previous implementation used a
    ThreadPoolExecutor, which is NON-daemon -- Python's interpreter shutdown
    joins every ThreadPoolExecutor worker before exiting, so a hung ``fn`` (the
    exact case this function defends against) blocked process exit entirely.
    A plain ``threading.Thread(daemon=True)`` is abandoned for real at
    interpreter exit, matching the docstring's original claim. Result/exception
    hand-off goes through a small Queue so both a normal return and a raised
    exception propagate to the caller exactly as ``Future.result()`` did.
    """
    import queue as _queue
    import threading as _threading

    q: "_queue.Queue[tuple[bool, object]]" = _queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            q.put((True, fn()))
        except BaseException as exc:                      # noqa: BLE001
            q.put((False, exc))

    _threading.Thread(target=_worker, daemon=True).start()
    try:
        ok, payload = q.get(timeout=timeout_s)
    except _queue.Empty:
        raise TimeoutError(
            f"_call_with_hard_timeout: {getattr(fn, '__name__', fn)!r} exceeded "
            f"{timeout_s}s"
        ) from None
    if ok:
        return payload
    raise payload  # type: ignore[misc]


def _browse_forma_projects() -> tuple[list[tuple[str, str, str]], str, str]:
    """Fetch ``(aecdm_id, dm_id, name)`` triples for all AECDM projects.

    Resolves the AECDM hub URN first (the DM Hub ID in .env uses b.<uuid> which
    the AECDM API rejects — it needs urn:adsk.ace:...), then lists projects with
    BOTH ids (see :func:`_parse_aecdm_projects`).
    Returns (projects, hub_name, error_msg).
    """
    import asyncio
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig

    async def _inner() -> tuple[list[tuple[str, str, str]], str]:
        config = FormaMCPConfig.from_env()
        async with FormaMCPClient(config) as client:
            hubs_raw = await client.list_aecdm_hubs()
            hub_pairs = _parse_mcp_name_id_list(hubs_raw)
            if not hub_pairs:
                raise RuntimeError("No AECDM hubs found — check Forma MCP connection.")
            aecdm_hub_id, hub_name = hub_pairs[0]
            proj_raw = await client.list_aecdm_projects(aecdm_hub_id)
        return _parse_aecdm_projects(proj_raw), hub_name

    try:
        projects, hub_name = _call_with_hard_timeout(
            lambda: asyncio.run(
                asyncio.wait_for(_inner(), timeout=_FORMA_BROWSE_TIMEOUT_S)
            ),
            _FORMA_BROWSE_TIMEOUT_S + 10,
        )
        return projects, hub_name, ""
    except (asyncio.TimeoutError, TimeoutError, FuturesTimeoutError):
        return [], "", _FORMA_TIMEOUT_MSG
    except Exception as exc:
        return [], "", str(exc)


def _browse_element_groups(aecdm_project_id: str) -> tuple[list[tuple[str, str]], str]:
    """Fetch (id, name) pairs for element groups (models) in an AECDM project."""
    import asyncio
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig

    async def _inner() -> list[tuple[str, str]]:
        config = FormaMCPConfig.from_env()
        async with FormaMCPClient(config) as client:
            raw = await client.list_element_groups(aecdm_project_id)
        return _parse_mcp_name_id_list(raw)

    try:
        return _call_with_hard_timeout(
            lambda: asyncio.run(
                asyncio.wait_for(_inner(), timeout=_FORMA_BROWSE_TIMEOUT_S)
            ),
            _FORMA_BROWSE_TIMEOUT_S + 10,
        ), ""
    except (asyncio.TimeoutError, TimeoutError, FuturesTimeoutError):
        return [], _FORMA_TIMEOUT_MSG
    except Exception as exc:
        return [], str(exc)


def _validate_extracted_yaml(yaml_text: str) -> tuple["RuleSet | None", str]:
    """M13: parse+validate extracted YAML through the SAME schema loader the
    engine uses (``RuleSet.model_validate``), never trust rules_extractor's own
    'executable' classification as proof the YAML is schema-valid for THIS
    app's pydantic version. Returns ``(ruleset, error)`` -- error is "" on
    success, ruleset is None on failure.
    """
    import yaml as _yaml

    from bim_orchestrator.policies.rules_schema import RuleSet

    try:
        data = _yaml.safe_load(yaml_text) or {}
        return RuleSet.model_validate(data), ""
    except Exception as exc:                                    # noqa: BLE001
        return None, str(exc)


def _ruleset_grounding_warnings(ruleset: "RuleSet") -> list[str]:
    """M13: per-rule category/parameter grounding check against the SAME
    catalogs the live run uses (OSTCatalog + param_catalog). A rule whose
    category doesn't resolve to an OST runs 0 checks (query_specs silently
    drops it) -- schema-valid but a false assurance ("run xanh"); a param
    that isn't in the category's catalog is equally silent for a Revit
    built-in. Returns one human-readable message per unresolved rule, or []
    when every rule grounds cleanly.
    """
    target = ruleset.target_category
    default_categories = target if isinstance(target, list) else [target]

    warnings: list[str] = []
    for rule in ruleset.rules:
        categories = [rule.category] if rule.category else default_categories
        cat_resolved = False
        param_resolved = False
        # Mirrors policies.rules_schema.fetch_name: the bound Revit name wins.
        param_name = rule.bound_parameter or rule.parameter
        checked_cats: list[str] = []
        for cat in categories:
            checked_cats.append(cat)
            ost = _ost_for_display(cat)
            if ost is None:
                continue
            cat_resolved = True
            specs = _catalog_params_for(cat)
            # An empty catalog for a resolved OST means the category isn't
            # probed yet (loadable families carry only common built-ins) --
            # don't flag the param in that case, only a genuinely-unresolved
            # category or a non-empty catalog that's missing the param.
            if not specs or any(s.name == param_name for s in specs):
                param_resolved = True
        if not cat_resolved:
            warnings.append(
                f"rule {rule.id}: category '{', '.join(checked_cats)}' không có trong "
                "OST catalog — rule sẽ chạy 0 check."
            )
        elif not param_resolved:
            warnings.append(
                f"rule {rule.id}: parameter '{param_name}' không có trong param_catalog "
                f"của category '{', '.join(checked_cats)}' — có thể chạy 0 check nếu đây "
                "không phải built-in hợp lệ."
            )
    return warnings


def _run_pdf_extraction(upload, scenario_prefix: str, *, tables: bool) -> None:
    """C1.2: run the governed rules_extractor pipeline on an uploaded doc and write
    executable YAML into config/. Extraction goes through the phase2 seam (provider
    switch + usage recorder); conversion is rules_extractor's deterministic converter.

    M13: before writing, each scenario's YAML is (a) re-validated through the
    engine's own schema loader (RuleSet.model_validate) -- a schema-invalid
    YAML is NEVER written, even if rules_extractor classified it "executable";
    (b) grounded against OSTCatalog/param_catalog so an invented category/param
    surfaces as a warning instead of silently running 0 checks; (c) gated by an
    overwrite confirmation when the target file already exists (mirrors the
    Rule Builder's reference-table overwrite guard).
    """
    import os as _os
    import tempfile

    from rules_extractor import convert_envelope, extract_sections, load_contract

    from bim_orchestrator.llm.extraction_bridge import (
        ExtractionUnavailable,
        extraction_model,
        make_extraction_client,
    )
    from bim_orchestrator.llm.usage import UsageRecorder

    recorder = UsageRecorder()
    try:
        client = make_extraction_client(recorder=recorder)
    except ExtractionUnavailable as exc:
        st.error(f"Không trích được: {exc}")
        return

    suffix = Path(upload.name).suffix.lower() or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        tf.write(upload.getbuffer())
        tmp_path = tf.name

    try:
        with st.spinner("Đang cắt section + trích rules…"):
            contract = load_contract()
            envelope, coverage = extract_sections(
                tmp_path, client=client, model=extraction_model(),
                tables=tables, contract=contract,
            )
            result = convert_envelope(envelope, contract=contract)
    except Exception as exc:                                    # noqa: BLE001
        st.error(f"Lỗi trích xuất: {exc}")
        return
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    written: list[str] = []
    skipped: list[str] = []
    prefix = _slugify(scenario_prefix) if scenario_prefix else ""
    for s in result.scenarios:
        if not s.rules_yaml:
            continue
        name = f"{prefix}_{s.scenario}" if prefix else s.scenario
        fname = f"rules.{name}.yaml"
        out_path = CONFIG_DIR / fname

        # M13a: validate through the SAME schema loader the engine uses.
        # rules_extractor's own "executable" classification is a DIFFERENT
        # (and possibly drifted) code path -- never trust it as proof the
        # YAML is schema-valid for this app. Failure -> don't write, ever.
        ruleset, schema_err = _validate_extracted_yaml(s.rules_yaml)
        if ruleset is None:
            st.error(f"⛔ `{fname}` không được ghi — lỗi schema: {schema_err}")
            skipped.append(fname)
            continue

        # M13b: grounding — a schema-valid rule can still cite a category or
        # parameter absent from the live catalogs, in which case it runs 0
        # checks and the run looks "green" with no assurance behind it.
        grounding_issues = _ruleset_grounding_warnings(ruleset)
        if grounding_issues:
            st.warning(
                f"⚠️ `{fname}` — grounding chưa khớp catalog:\n"
                + "\n".join(f"- {w}" for w in grounding_issues)
            )

        # M13c: overwrite guard — mirrors the Rule Builder's reference-table
        # guard (_reference_needs_overwrite): an existing file with DIFFERENT
        # content requires an explicit confirmation checkbox before clobbering
        # (it may be a hand-curated rules file the extraction would stomp on).
        needs_confirm = out_path.exists() and out_path.read_text(encoding="utf-8") != s.rules_yaml
        overwrite_ok = True
        if needs_confirm:
            overwrite_ok = st.checkbox(
                f"⚠️ `{fname}` đã tồn tại với nội dung KHÁC — tích để **ghi đè**.",
                key=f"rx_overwrite_{fname}",
                value=False,
            )
        if needs_confirm and not overwrite_ok:
            st.error(
                f"⛔ Chưa ghi `{fname}`: file đã tồn tại và khác nội dung. Tích ô "
                "ghi đè ở trên rồi bấm **🔍 Trích xuất** lại."
            )
            skipped.append(fname)
            continue

        out_path.write_text(s.rules_yaml, encoding="utf-8")
        written.append(fname)

    st.markdown("**Coverage (mỗi section = 1 lần trích):**")
    st.table([
        {"section": c.title, "vị trí": c.location, "rules": c.rules, "trạng thái": c.status}
        for c in coverage
    ])
    usage_line = recorder.format_line()
    if usage_line:
        st.caption(usage_line)
    if written:
        st.success(
            f"✅ Ghi {len(written)} file vào config/: {', '.join(written)}. "
            "Chọn ở danh sách **Rules YAML** bên dưới rồi ▶️ Run."
        )
    elif not skipped:
        st.warning("Không có rule executable nào (xem review report bên dưới).")
    if skipped:
        st.info(f"Bỏ qua (chưa ghi): {', '.join(skipped)} — xem lý do ở trên.")
    for s in result.scenarios:
        if s.review_md and (s.review or s.invalid or s.warnings):
            with st.expander(f"📋 Review — {s.scenario} "
                             f"(review {s.review} · invalid {s.invalid} · warn {len(s.warnings)})"):
                st.markdown(s.review_md)


def _render_pdf_extraction_block() -> None:
    """C1.2 (bản gọn): PDF/BEP quy chuẩn → rules YAML, ngay trong app."""
    from bim_orchestrator.llm.extraction_bridge import rules_extractor_available

    with st.expander("📄 Trích rules từ PDF quy chuẩn / BEP (thử nghiệm)", expanded=False):
        if not rules_extractor_available():
            st.info(
                "Chưa cài `rules-extractor`. Cài (dev): "
                "`uv pip install -e <path-to>/ExtractionAgents`, rồi tải lại trang."
            )
            return
        st.caption(
            "Tải PDF/txt quy chuẩn → tự cắt section → trích rules (Anthropic, "
            "`BIM_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`) → ghi vào `config/`."
        )
        up = st.file_uploader("PDF hoặc .txt", type=["pdf", "txt"], key="rx_pdf_upload")
        c1, c2 = st.columns([3, 2])
        with c1:
            scenario = st.text_input(
                "Tên scenario (prefix cho file)",
                value=(Path(up.name).stem[:40] if up else ""),
                key="rx_scenario",
            )
        with c2:
            tables = st.checkbox(
                "Đọc bảng (pdfplumber)", key="rx_tables",
                help="Bật cho quy chuẩn có bảng ruled (occupancy / fire-rating).",
            )
        if up and st.button("🔍 Trích xuất", key="rx_extract_btn", type="primary"):
            _run_pdf_extraction(up, scenario or Path(up.name).stem, tables=tables)


def render_setup_tab() -> None:
    """Setup tab: project/model selection + rules + run options.

    All three sections render as a seamless flow (no st.form border).
    Projects auto-load on first visit; models load on demand after project
    selection.
    """
    st.header("📋 Setup")
    st.caption(
        "Configure which Forma project + Revit model + rules YAML the next run targets."
    )

    # ── Section 1: Forma project ─────────────────────────────────────────────
    st.subheader("1. Forma project")

    # Auto-load projects once per session — only when a live Streamlit server is
    # running (st.runtime.exists() is falsy with the test mock, skipping the call).
    # UX: skip the auto-load entirely when no forma-mcp server is configured —
    # spawning it would just burn the browse timeout on a machine that can't
    # succeed; the manual-ID expander below is the intended path there.
    if "_browse_projects" not in st.session_state and st.runtime.exists():
        if _get_forma_mcp_dir() is None:
            st.session_state["_browse_projects"] = []
            st.session_state["_browse_projects_err"] = (
                "Chưa cấu hình forma-mcp server (xem 🔌 MCP Server Paths bên dưới) "
                "— hoặc nhập ID thủ công (✏️)."
            )
        else:
            with st.spinner(
                f"Đang tải danh sách project từ Forma… (tối đa {_FORMA_BROWSE_TIMEOUT_S:.0f}s)"
            ):
                projects, hub_name_loaded, err = _browse_forma_projects()
            st.session_state["_browse_projects"] = projects  # [] on error — prevents retry loop
            if hub_name_loaded:
                st.session_state["_aecdm_hub_name"] = hub_name_loaded
            if err:
                st.session_state["_browse_projects_err"] = err

    hub_name = st.session_state.get("_aecdm_hub_name", "")
    if hub_name:
        st.caption(f"Hub: **{hub_name}**")
    else:
        hub_env = st.session_state.get("hub_id", "") or os.environ.get("DEMO_HUB_ID", "")
        hub_label = (hub_env[:24] + "…") if len(hub_env) > 24 else (hub_env or "not configured")
        st.caption(f"Hub: `{hub_label}`")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Project Name**")
        projects_list: list[tuple[str, str, str]] = st.session_state.get("_browse_projects", [])
        if projects_list:
            proj_options = [n for _, _, n in projects_list]
            cur_proj_name = st.session_state.get("project_name", "")
            cur_pidx = proj_options.index(cur_proj_name) if cur_proj_name in proj_options else 0
            sel_proj_name = st.selectbox(
                "Project", proj_options, index=cur_pidx,
                key="proj_sel", label_visibility="collapsed",
            )
            sel = next(
                ((a, d) for a, d, n in projects_list if n == sel_proj_name), None
            )
            if sel and sel[0] != st.session_state.get("aecdm_project_id"):
                # Forma dual-id (2026-06-19): ONE aecdm_list_projects call yields
                # BOTH ids per project — the AECDM URN (element-group queries) AND
                # the DM/Issues id `b.<guid>` (issues_* / dm_* / reviews_*). Set
                # each to its own purpose so any project resolves correctly with no
                # hard-coded id. (Falls back to the seeded DEMO_PROJECT_ID only if a
                # project reports no linked DM container.)
                aecdm_id, dm_id = sel
                st.session_state["aecdm_project_id"] = aecdm_id
                st.session_state["project_id"] = dm_id or st.session_state.get("project_id", "")
                st.session_state["project_name"] = sel_proj_name
                st.session_state["_browse_element_groups"] = []
                # Medium: switching project MUST clear the previously-selected model
                # (element group). Otherwise a run against project B would query
                # project A's model — wrong-model results with no warning.
                st.session_state["element_group_id"] = ""
                st.session_state["element_group_name"] = ""
        else:
            load_err = st.session_state.get("_browse_projects_err", "")
            st.warning(f"Không tải được danh sách project. {load_err or 'Check Forma MCP connection.'}")
            if st.button("🔄 Thử lại", key="btn_retry_projects"):
                for k in ("_browse_projects", "_browse_projects_err", "_aecdm_hub_name"):
                    st.session_state.pop(k, None)
                st.rerun()

    # UX: manual fallback — when the Forma browse is unavailable (no server,
    # timeout, bad creds) the IDs can be pasted directly; without this the ONLY
    # path was editing .env + restarting Streamlit.
    _ids_set = bool(st.session_state.get("project_id") or st.session_state.get("element_group_id"))
    with st.expander("✏️ Nhập ID thủ công (khi không duyệt được từ Forma)",
                     expanded=not _ids_set and not st.session_state.get("_browse_projects")):
        with st.form("manual_ids_form", border=False):
            m_proj = st.text_input(
                "Project ID (DM/Issues — dạng `b.<uuid>`)",
                value=st.session_state.get("project_id", ""),
                help="Dùng cho issues_* / dm_* API. Lấy từ URL ACC hoặc DEMO_PROJECT_ID trong .env.",
            )
            m_aecdm = st.text_input(
                "AECDM Project ID (dạng `urn:adsk.workspace:prod.project:<uuid>`)",
                value=st.session_state.get("aecdm_project_id", ""),
                help="Dùng cho element-group queries. Bỏ trống nếu chỉ chạy Full run — Revit.",
            )
            m_eg = st.text_input(
                "Element Group ID (model — chỉ cần cho chế độ Apply — Forma)",
                value=st.session_state.get("element_group_id", ""),
            )
            if st.form_submit_button("✔️ Dùng các ID này"):
                st.session_state["project_id"] = m_proj.strip()
                st.session_state["aecdm_project_id"] = m_aecdm.strip()
                st.session_state["element_group_id"] = m_eg.strip()
                # Manual entry has no display names — show the ids' tails instead.
                st.session_state["project_name"] = ""
                st.session_state["element_group_name"] = ""
                st.rerun()

    with col2:
        st.markdown("**Model Name**")
        egroups_list: list[tuple[str, str]] = st.session_state.get("_browse_element_groups", [])
        if egroups_list:
            eg_options = [n for _, n in egroups_list]
            cur_eg_name = st.session_state.get("element_group_name", "")
            cur_eidx = eg_options.index(cur_eg_name) if cur_eg_name in eg_options else 0
            sel_eg_name = st.selectbox(
                "Model", eg_options, index=cur_eidx,
                key="eg_sel", label_visibility="collapsed",
            )
            sel_eg_id = next((eid for eid, n in egroups_list if n == sel_eg_name), None)
            if sel_eg_id and sel_eg_id != st.session_state.get("element_group_id"):
                st.session_state["element_group_id"] = sel_eg_id
                st.session_state["element_group_name"] = sel_eg_name
        else:
            aecdm_id = st.session_state.get("aecdm_project_id") or st.session_state.get("project_id", "")
            if st.button("Load models", key="btn_load_models", disabled=not aecdm_id):
                with st.spinner("Listing models…"):
                    egroups, err = _browse_element_groups(aecdm_id)
                if egroups:
                    st.session_state["_browse_element_groups"] = egroups
                    st.rerun()
                else:
                    st.error(f"No models returned. {err or 'Check Project and Forma MCP connection.'}")

    st.divider()

    # ── Section 2: Rules ─────────────────────────────────────────────────────
    st.subheader("2. Rules")
    _render_pdf_extraction_block()  # C1.2: PDF/BEP → rules YAML, in-app
    _adv = False
    if RULE_LIBRARY_DIR.exists():
        _adv = st.checkbox(
            "📚 Gồm Thư viện nâng cao (`rule-library/`)",
            value=st.session_state.get("rb_include_advanced", False),
            key="rb_include_advanced",
            help="Bật để chọn các pack rule nâng cao trong rule-library/ (private, "
                 "không nằm trong bản share công khai). Mỗi pack tự chứa data; engine "
                 "fallback về config/ cho các bảng dùng chung.",
        )
    rule_files = _list_rules_yamls(include_advanced=_adv)
    rule_options = [str(p.relative_to(REPO_ROOT)) for p in rule_files]
    # v1.4-K6: default the multiselect to the saved selection (rules_paths),
    # falling back to the single rules_path, then to the first option.
    def _rel(p: str) -> str | None:
        pp = Path(p)
        if p and pp.exists() and pp.is_relative_to(REPO_ROOT):
            return str(pp.relative_to(REPO_ROOT))
        return None
    _saved = st.session_state.get("rules_paths") or [st.session_state.get("rules_path", "")]
    # v1.4-K22: NO forced default — when nothing is selected the multiselect (and
    # the "rule sẽ chạy" preview) stay empty, instead of silently showing the first
    # scenario's rules. The user picks explicitly; the run guards on empty below.
    current_rules_rel = [r for r in (_rel(p) for p in _saved) if r in rule_options]
    rules_choice = st.multiselect(
        "Rules YAML (chọn 1 hoặc nhiều để gộp kiểm trong 1 lần chạy)",
        options=rule_options,
        default=current_rules_rel,
        format_func=lambda r: Path(r).name,   # show file name, not the long path
        help="Pick one or more YAML rulesets from config/. Multiple → merged "
        "into a single run (rule ids dedup, first-selected wins). Mới nhất ở trên.",
    )
    # v1.4-K17: a readable summary of what's actually selected — the multiselect
    # chips truncate, so list every rule (id · parameter · requirement · action)
    # for easy review/counting before running.
    if rules_choice:
        import yaml as _y_sum
        _rows: list[dict] = []
        for _rel in rules_choice:
            _fp = REPO_ROOT / _rel
            try:
                _d = _y_sum.safe_load(_fp.read_text(encoding="utf-8")) or {}
            except Exception:                       # noqa: BLE001
                continue
            for _r in (_d.get("rules") or []):
                _rows.append({
                    "file": Path(_rel).name,
                    "rule id": _r.get("id"),
                    "parameter": _r.get("parameter"),
                    "yêu cầu": REQUIREMENT_LABELS_ALL.get(_r.get("requirement"), _r.get("requirement")),
                    "hành động": _rule_action_summary(_r),
                })
            for _g in (_d.get("geometry_rules") or []):
                _rows.append({"file": Path(_rel).name, "rule id": _g.get("id"),
                              "parameter": "(geometry)", "yêu cầu": _g.get("check_type", "geometry"),
                              "hành động": "📐 geometry"})
        if _rows:
            st.caption(f"📋 {len(_rows)} rule sẽ chạy:")
            st.table(_rows)
    else:
        st.caption("⬆️ Chưa chọn rule nào — chọn ít nhất 1 file ở trên để chạy.")

    st.divider()

    # ── Section 3: Run options ────────────────────────────────────────────────
    st.subheader("3. Run options")

    # Geometry rules require Revit MCP — Forma has no geometry query API yet.
    # With several files selected, geometry-mode locking applies if ANY of them
    # carries geometry rules.
    _cur_rules_paths = [str(REPO_ROOT / r) for r in rules_choice] if rules_choice else []
    _has_geo = any(_yaml_has_geometry_rules(p) for p in _cur_rules_paths)

    # v1.4-K18: dropped the read-only "Check — Forma" mode — every run now either
    # creates ACC Issues (Apply) or auto-fixes + creates issues (Full run). The
    # CLI keeps --check for a no-write audit.
    _MODE_LABELS = {
        "apply":     "📋 Apply — Forma → ACC Issues",
        "run-revit": "⚙️ Full run — Revit + ACC Issues",
    }
    _MODE_KEYS = list(_MODE_LABELS.keys())
    cur_mode = st.session_state["run_mode"] if st.session_state["run_mode"] in _MODE_KEYS else "run-revit"

    if _has_geo:
        st.warning(
            "📐 File YAML này chứa **geometry rules** — "
            "geometry check trên Forma chưa hỗ trợ. "
            "Chỉ **Full run — Revit** mới có thể thực thi.",
            icon=None,
        )
        st.markdown(
            "~~📋 Apply — Forma → ACC Issues~~ &nbsp; *(geometry check on Forma chưa hỗ trợ)*  \n"
            "**⚙️ Full run — Revit + ACC Issues** ← bắt buộc khi có geometry rules"
        )
        run_mode = "run-revit"
    else:
        run_mode_label = st.radio(
            "Mode",
            options=list(_MODE_LABELS.values()),
            index=_MODE_KEYS.index(cur_mode),
            horizontal=True,
            help=(
                "**Apply**: queries the Forma (AEC Data Model) model and creates ACC "
                "Issues for violations (manual review, grouped by rule). No Revit, "
                "no auto-fix proposal.\n\n"
                "**Full run**: queries the OPEN Revit document, writes auto-fixes "
                "(Path B, approve-gated proposal issue) AND creates ACC Issues for "
                "manual findings (Path A). Model selection in Section 1 is not used."
            ),
        )
        _label_to_mode = {v: k for k, v in _MODE_LABELS.items()}
        run_mode = _label_to_mode.get(run_mode_label, cur_mode)

    if run_mode == "run-revit":
        st.caption(
            "📌 Data source: open Revit document — "
            "**Project** (Section 1) is used for ACC Issues; **Model** is not queried."
        )
    else:
        st.caption("📌 Data source: Forma model — **Project** and **Model** (Section 1) are both required.")

    # v1.4-K14: ACC Issues are IMPLIED by the mode name ("→/+ ACC Issues") — no
    # opt-in checkbox (off-by-default silently emptied the Approvals inbox). And
    # "Preview only" is dropped: Check mode IS the read-only preview, and Path B
    # is already approval-gated. The rare no-ACC project flips issues off below.
    dry_run = False
    if run_mode == "run-revit":
        st.markdown("**Full-run options**")
        col5, col6 = st.columns(2)
        with col5:
            limit = st.number_input(
                "Số ACC Issue tối đa / lần chạy",
                min_value=0,
                max_value=100,
                value=int(st.session_state["limit"]),
                help=(
                    "Giới hạn **số ACC Issue** tạo ra. Toàn bộ đề xuất tự-sửa "
                    "(Path B) luôn gom vào **1 proposal issue** (không bị cắt, "
                    "duyệt 1 lần) — chiếm 1 suất; phần còn lại dành cho các issue "
                    "**review thủ công** (mỗi element 1 issue). 0 = không giới hạn."
                ),
            )
        with col6:
            max_iterations = st.number_input(
                "Max audit cycles",
                min_value=1,
                max_value=10,
                value=int(st.session_state["max_iterations"]),
                help=(
                    "How many times the Query→QC→Design loop may repeat. "
                    "1 = single pass (fast, good for most audits); "
                    "3 = up to 3 cycles (use when fixing one violation may reveal another)."
                ),
            )
        max_iterations = int(max_iterations)
        with st.expander("⚙️ Tùy chọn nâng cao", expanded=False):
            no_acc = st.checkbox(
                "Project chưa liên kết ACC — bỏ tạo issue (--no-forma)",
                value=bool(st.session_state["no_forma"]),
                help=(
                    "Mặc định TẮT: chế độ này luôn tạo ACC Issues + proposal issue "
                    "để duyệt. Chỉ bật khi project không có ACC workspace."
                ),
            )
        create_issues = not no_acc
    elif run_mode == "apply":
        st.markdown("**Apply options**")
        limit = st.number_input(
            "Số ACC Issue tối đa / lần chạy",
            min_value=0,
            max_value=100,
            value=int(st.session_state["limit"]),
            help="Giới hạn số ACC Issue tạo ra trong 1 lần Apply. 0 = không giới hạn.",
        )
        max_iterations = st.session_state["max_iterations"]
        create_issues = True  # apply always targets Forma Issues
    else:  # defensive fallback (Check mode dropped — v1.4-K18)
        limit = st.session_state["limit"]
        max_iterations = st.session_state["max_iterations"]
        create_issues = not st.session_state["no_forma"]

    if st.button("💾 Save selection", type="primary"):
        if rules_choice:
            abs_paths = [str(REPO_ROOT / r) for r in rules_choice]
            st.session_state["rules_paths"] = abs_paths
            st.session_state["rules_path"] = abs_paths[0]  # primary (back-compat)
        st.session_state["run_mode"] = run_mode
        st.session_state["limit"] = int(limit)
        st.session_state["dry_run"] = bool(dry_run)
        st.session_state["max_iterations"] = int(max_iterations)
        st.session_state["no_forma"] = not bool(create_issues)
        st.success("Saved. Switch to the **Run** tab to launch.")

    # H6: ▶️ Run uses ONLY the SAVED selection/mode. Warn when the current widgets
    # differ from what was saved, so an unsaved edit (rules, mode, dry-run) isn't
    # silently ignored on the next Run — the "N rule sẽ chạy" summary above reflects
    # the CURRENT widgets and can otherwise mislead.
    # (guard on list: st.multiselect returns a list in real Streamlit; under the
    # import-time test stub it's a proxy — skip the diff there.)
    if isinstance(rules_choice, list):
        _saved_rel = [
            str(Path(p).relative_to(REPO_ROOT))
            for p in (st.session_state.get("rules_paths") or [])
            if p and Path(p).is_relative_to(REPO_ROOT)
        ]
        _pending: list[str] = []
        if sorted(rules_choice) != sorted(_saved_rel):
            _pending.append("Rules YAML")
        if run_mode != st.session_state.get("run_mode"):
            _pending.append("chế độ chạy")
        if bool(dry_run) != bool(st.session_state.get("dry_run")):
            _pending.append("dry-run")
        if _pending:
            st.warning(
                "⚠️ Đã đổi **" + " · ".join(_pending) + "** nhưng **CHƯA Save**. "
                "▶️ Run chỉ dùng cấu hình **đã Save** — bấm **💾 Save selection** để áp dụng."
            )

    # ── MCP Server path configuration ───────────────────────────────────────
    forma_env = os.environ.get("FORMA_MCP_SERVER_CWD", "")
    revit_env = os.environ.get("REVIT_MCP_SERVER_CWD", "")
    forma_vendor = _detect_vendor_path("forma-mcp")
    revit_vendor = _detect_vendor_path("revit-mcp")
    _forma_exe = (REPO_ROOT / "vendor" / "forma-mcp" / "forma-mcp.exe").exists()
    paths_configured = bool(forma_env or forma_vendor)
    with st.expander("🔌 MCP Server Paths", expanded=not paths_configured):
        st.caption(
            "Auto-detection looks for `vendor/forma-mcp/forma-mcp.exe` (SEA) or "
            "`vendor/forma-mcp/dist/index.js` (Node). "
            "Override with env vars if the servers are installed elsewhere."
        )
        col_f, col_r = st.columns(2)
        with col_f:
            st.markdown("**Forma MCP**")
            if forma_env:
                st.success(f"`.env` path: `{Path(forma_env).name}`")
            elif _forma_exe:
                st.success("Auto-detected: `vendor/forma-mcp/forma-mcp.exe` (SEA)")
            elif forma_vendor:
                st.success("Auto-detected: `vendor/forma-mcp` (Node)")
            else:
                st.warning("Not found. Set `FORMA_MCP_SERVER_CWD` below or place server in `vendor/forma-mcp/`.")
        with col_r:
            st.markdown("**Revit MCP**")
            if revit_env:
                st.success(f"`.env` path: `{Path(revit_env).name}`")
            elif revit_vendor:
                st.success("🔍 Auto-detected: `vendor/revit-mcp`")
            else:
                st.info("Optional — only needed for `--run-revit` mode.")

        st.divider()
        st.caption("Override paths (saved to `.env`):")
        with st.form("mcp_paths_form"):
            new_forma = st.text_input(
                "FORMA_MCP_SERVER_CWD",
                value=forma_env,
                placeholder="Leave blank to use vendor/forma-mcp auto-detect",
            )
            new_revit = st.text_input(
                "REVIT_MCP_SERVER_CWD",
                value=revit_env,
                placeholder="Leave blank to use vendor/revit-mcp auto-detect",
            )
            if st.form_submit_button("💾 Save to .env"):
                _write_env_values({
                    "FORMA_MCP_SERVER_CWD": new_forma.strip(),
                    "REVIT_MCP_SERVER_CWD": new_revit.strip(),
                })
                st.success("Saved. New paths active on next Streamlit rerun.")

    # ── SSA / APS credential wizard ─────────────────────────────────────────
    # Writes to {forma_mcp_dir}/.env (NOT the orchestrator .env) so the
    # subprocess dotenv picks them up at startup without exposing secrets
    # to the orchestrator process.
    forma_dir = _get_forma_mcp_dir()
    with st.expander("🔑 Forma / APS credentials", expanded=forma_dir is None):
        if forma_dir is None:
            st.warning(
                "No forma-mcp directory found. Configure `FORMA_MCP_SERVER_CWD` "
                "in the MCP Server Paths section above, or place the server in "
                "`vendor/forma-mcp/`."
            )
        else:
            st.caption(
                f"Saved to `{Path(forma_dir).name}/.env` — loaded by the MCP "
                "server subprocess at startup. Never written to the orchestrator `.env`."
            )
            forma_env_path = Path(forma_dir) / ".env"
            _cur: dict[str, str] = {}
            if forma_env_path.exists():
                for _line in forma_env_path.read_text(encoding="utf-8").splitlines():
                    if "=" in _line and not _line.strip().startswith("#"):
                        _k, _, _v = _line.partition("=")
                        _cur[_k.strip()] = _v.strip()

            with st.form("ssa_creds_form"):
                auth_mode = st.selectbox(
                    "APS_AUTH_MODE",
                    ["ssa", "2lo"],
                    index=0 if _cur.get("APS_AUTH_MODE", "ssa") == "ssa" else 1,
                    help="SSA = headless service account (recommended); 2LO = 2-legged app credentials only",
                )
                col_id, col_sec = st.columns(2)
                with col_id:
                    aps_id = st.text_input("APS_CLIENT_ID", value=_cur.get("APS_CLIENT_ID", ""))
                with col_sec:
                    _has_secret = bool(_cur.get("APS_CLIENT_SECRET"))
                    aps_secret = st.text_input(
                        "APS_CLIENT_SECRET",
                        # Medium: never round-trip the STORED secret to the browser
                        # (a type=password field still ships `value` in the DOM).
                        # Blank = keep the existing secret; type to replace it.
                        value="",
                        type="password",
                        placeholder="••••• (unchanged)" if _has_secret else "",
                        help="Leave blank to keep the current secret; type a new value to replace it.",
                    )
                ssa_id = ssa_key_id = ssa_key_path = ""
                if auth_mode == "ssa":
                    st.caption("SSA service account credentials:")
                    col_sid, col_skid = st.columns(2)
                    with col_sid:
                        ssa_id = st.text_input("SSA_ID", value=_cur.get("SSA_ID", ""))
                    with col_skid:
                        ssa_key_id = st.text_input("SSA_KEY_ID", value=_cur.get("SSA_KEY_ID", ""))
                    ssa_key_path = st.text_input(
                        "SSA_KEY_PATH",
                        value=_cur.get("SSA_KEY_PATH", ""),
                        help="Absolute path to the SSA private key .pem file on this machine",
                    )
                if st.form_submit_button("💾 Save to forma-mcp .env"):
                    _cred_vals: dict[str, str] = {
                        "APS_AUTH_MODE": auth_mode,
                        "APS_CLIENT_ID": aps_id.strip(),
                        # Blank field → keep the existing secret (don't wipe it).
                        "APS_CLIENT_SECRET": aps_secret.strip() or _cur.get("APS_CLIENT_SECRET", ""),
                    }
                    if auth_mode == "ssa":
                        _cred_vals.update({
                            "SSA_ID": ssa_id.strip(),
                            "SSA_KEY_ID": ssa_key_id.strip(),
                            "SSA_KEY_PATH": ssa_key_path.strip(),
                        })
                    _write_forma_env(forma_dir, _cred_vals)
                    st.success(
                        f"Saved to `{Path(forma_dir).name}/.env`. "
                        "Restart the forma-mcp server to pick up changes."
                    )

    # ── Connection test ─────────────────────────────────────────────────────
    # QW-2: surface this as a prominent button on the tab (no longer hidden
    # in an expander) so first-launch users see how to verify wiring.
    st.subheader("🔌 Forma MCP connection")
    if not st.session_state["element_group_id"]:
        st.caption("Set Element Group ID above first, then test the connection.")
    else:
        col_btn, col_status = st.columns([1, 3])
        with col_btn:
            do_test = st.button("Run smoke test", help="Spawns `bim-orchestrator --hello`")
        if do_test:
            with st.spinner("Connecting + querying AECDM..."):
                ok, log = _test_forma_connection()
            with col_status:
                (st.success if ok else st.error)(
                    "✅ Forma MCP reachable" if ok else "❌ smoke test failed"
                )
            with st.expander("Smoke test log", expanded=not ok):
                st.code(log, language="text")

    # ── Revit addin connection ───────────────────────────────────────────────
    st.subheader("🔌 Revit addin connection")
    _rv_version = os.environ.get("REVIT_MCP_VERSION", "2026")
    try:
        _rv_port = int(os.environ.get("REVIT_MCP_PORT") or (7891 + max(0, int(_rv_version) - 2026)))
    except ValueError:
        _rv_port = 7891
    st.caption(
        f"Targets Revit {_rv_version} addin at `127.0.0.1:{_rv_port}` via HTTP direct "
        f"(no Node bridge). Set `REVIT_MCP_VERSION` in `.env` for other Revit years."
    )
    col_rvbtn, col_rvstatus = st.columns([1, 3])
    with col_rvbtn:
        do_revit_test = st.button(
            "Test Revit connection",
            help="GET /health on the in-Revit addin — Revit must be open",
        )
    if do_revit_test:
        with st.spinner("Probing addin..."):
            rv_ok, rv_msg = _test_revit_connection()
        with col_rvstatus:
            (st.success if rv_ok else st.warning)(
                f"✅ {rv_msg}" if rv_ok else f"⚠️ {rv_msg}"
            )


_RUN_ID_RE = re.compile(r"run-[0-9a-f]{8}")


def _build_orchestrator_argv(mode: str) -> list[str]:
    """Translate session_state into the orchestrator CLI argv vector.

    Centralised so the Run tab is the only place that needs to know which
    CLI flags map to which session keys.
    """
    base = [sys.executable, "-m", "bim_orchestrator.orchestrator", f"--{mode}"]
    # v1.4-K6: emit every selected ruleset (CLI --rules is nargs="+"). Falls
    # back to the single rules_path for sessions saved before multi-select.
    rules_paths = st.session_state.get("rules_paths") or [
        st.session_state.get("rules_path") or ""
    ]
    valid_rules = [p for p in rules_paths if p and Path(p).exists()]
    if valid_rules:
        base += ["--rules", *valid_rules]
    limit = st.session_state.get("limit")
    if limit is not None and mode in ("apply", "run", "run-revit"):
        base += ["--limit", str(limit)]
    if st.session_state.get("dry_run") and mode in ("apply", "run", "run-revit"):
        base += ["--dry-run"]

    # run-revit specific flags — only emitted when mode matches so they
    # don't pollute --check argv (argparse would reject them there).
    if mode == "run-revit":
        max_iter = st.session_state.get("max_iterations")
        if isinstance(max_iter, int) and max_iter > 1:
            base += ["--max-iterations", str(max_iter)]
        if st.session_state.get("no_forma"):
            base += ["--no-forma"]
    return base


def _build_env() -> dict[str, str]:
    """Merge current process env with the Setup tab's per-session overrides.

    The orchestrator reads DEMO_* env vars; we override them per run so
    multi-project usage on one Streamlit instance is possible without
    restarting.
    """
    env = dict(os.environ)
    if st.session_state["hub_id"]:
        env["DEMO_HUB_ID"] = st.session_state["hub_id"]
    if st.session_state["project_id"]:
        env["DEMO_PROJECT_ID"] = st.session_state["project_id"]
    if st.session_state["aecdm_project_id"]:
        env["DEMO_AECDM_PROJECT_ID"] = st.session_state["aecdm_project_id"]
    if st.session_state["element_group_id"]:
        env["DEMO_ELEMENT_GROUP_ID"] = st.session_state["element_group_id"]
    # Force plain log format so the live stream looks clean in st.code
    env["BIM_LOG_FORMAT"] = "plain"
    # Force the child to emit UTF-8 on Windows so its own prints don't hit a
    # cp1252 crash on emoji / Vietnamese / arrows; the parent also reads the
    # pipe as UTF-8 (see _stream_subprocess).
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _stream_subprocess(
    argv: list[str], env: dict[str, str], box, *, timeout_s: float | None = None,
    on_start=None,
) -> tuple[int, str]:
    """Run argv as a subprocess and tail stdout into a Streamlit placeholder.

    Returns ``(returncode, full_log)``. Streams line-by-line; the placeholder
    shows the last 80 lines so the user sees the latest events without the
    box growing unbounded.

    H5: a wall-clock ``timeout_s`` bounds the run. The blocking ``for line in
    proc.stdout`` loop used to freeze the whole UI forever if the child hung with
    no output (a Revit modal dialog, or an unreachable addin) — the only escape
    was killing the Streamlit server. We now read via a daemon reader thread + a
    queue so the main loop can re-check the deadline every 0.5s and terminate the
    process on timeout, returning the partial log.

    M9: a Streamlit rerun (user touches any widget mid-run) raises a stop
    exception INSIDE a ``st.*`` call — including ``box.code(...)`` in the loop
    below. That used to unwind out of this function without ever reaching
    ``proc.wait()``/``proc.terminate()``, leaving the child (and its Revit
    write session) orphaned; a second Run click then launched a second process
    against the same document. The whole streaming loop is now wrapped in
    try/finally: whatever the exit path (normal completion, timeout, or a
    Streamlit rerun unwinding the stack), the finally clause terminates the
    process if it's still alive, escalating to kill() after a short grace
    period. The caller layer additionally tracks the live Popen in
    ``st.session_state`` to refuse a second concurrent launch (see
    ``_launch_guarded``) — this finally is the last-resort net for orphans
    that guard didn't catch (e.g. a stale record from a prior server restart).
    """
    import queue as _queue
    import threading as _threading
    import time as _time

    proc = subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Decode the child's stdout as UTF-8 regardless of the Windows locale
        # (default cp1252 crashes on the orchestrator's UTF-8 output — arrows,
        # em-dashes, Vietnamese). errors="replace" keeps the stream alive even
        # on an unexpected byte instead of killing the whole run.
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if on_start is not None:
        on_start(proc)
    all_lines: list[str] = []
    assert proc.stdout is not None
    q: "_queue.Queue[str | None]" = _queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:            # type: ignore[union-attr]
                q.put(line.rstrip())
        finally:
            q.put(None)                          # sentinel: stdout closed

    _threading.Thread(target=_reader, daemon=True).start()
    start = _time.monotonic()
    timed_out = False
    try:
        while True:
            if timeout_s is not None and _time.monotonic() - start > timeout_s:
                timed_out = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                all_lines.append(
                    f"... [TIMEOUT after {int(timeout_s)}s — process terminated. A Revit "
                    "modal dialog or an unreachable addin can hang the run; check Revit, "
                    "raise the limit, then retry.] ..."
                )
                box.code("\n".join(all_lines[-80:]), language="text")
                break
            try:
                item = q.get(timeout=0.5)
            except _queue.Empty:
                continue                         # no new line — re-check the deadline
            if item is None:
                break                            # stdout closed → process finished
            all_lines.append(item)
            box.code("\n".join(all_lines[-80:]), language="text")

        if not timed_out:
            proc.wait()
    finally:
        # M9: reached on every exit path, including a Streamlit rerun's stop
        # exception unwinding through box.code(...) above. Never leave the
        # child running against the same Revit document.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, "\n".join(all_lines)


# M9: session-state key holding the live Popen of an in-flight orchestrator
# subprocess (set only by _launch_guarded, cleared in its finally). Distinct
# from "is_running" (a bool the old code reset in a finally that could itself
# be skipped by the same rerun stop-exception) — this is checked BEFORE a new
# launch is allowed, so a second click while a proc handle is still alive is
# refused instead of racing a second write into the same Revit document.
_ACTIVE_PROC_KEY = "_active_run_proc"


def _active_run_still_alive() -> bool:
    """True iff session_state holds a Popen from a prior launch that hasn't
    exited yet. A finished process (poll() is not None) clears the slot."""
    proc = st.session_state.get(_ACTIVE_PROC_KEY)
    if proc is None:
        return False
    if proc.poll() is None:
        return True
    st.session_state[_ACTIVE_PROC_KEY] = None
    return False


def _launch_guarded(
    argv: list[str], env: dict[str, str], box, *, timeout_s: float | None = None
) -> tuple[int, str] | None:
    """Refuse a new launch while a previous one is still alive; otherwise
    delegate to ``_stream_subprocess`` with the Popen tracked in session_state
    for the duration of the call.

    Returns ``None`` (no launch happened) when blocked — callers must check
    for this before reading ``rc``/log. BLOCKING is the safer choice over
    auto-terminating the old run: an unattended terminate of a live Revit
    write session is worse than asking the user to wait or refresh.
    """
    if _active_run_still_alive():
        st.error(
            "⛔ Một run khác đang chạy (cùng phiên). Đợi run đó xong, hoặc tải lại "
            "trang nếu bạn chắc chắn nó đã treo."
        )
        return None

    def _track(proc: subprocess.Popen) -> None:
        st.session_state[_ACTIVE_PROC_KEY] = proc

    try:
        return _stream_subprocess(argv, env, box, timeout_s=timeout_s, on_start=_track)
    finally:
        st.session_state[_ACTIVE_PROC_KEY] = None


def render_run_tab() -> None:
    """Run tab: connection check + Run button + live stdout stream."""
    st.header("▶️ Run")

    # ── Pre-flight checks ───────────────────────────────────────────────────
    mode = st.session_state["run_mode"]
    needs_eg = mode != "run-revit"  # run-revit can skip Forma with --no-forma
    needs_project = mode in ("apply", "run", "run-revit")

    _rules_sel = _resolve_rules_selection()
    _rules_ok = [p for p in _rules_sel if p and Path(p).exists()]
    _rules_detail = (
        ", ".join(Path(p).name for p in _rules_ok) if _rules_ok else "(empty)"
    )
    checks: list[tuple[str, bool, str]] = [
        ("Rules YAML resolved", bool(_rules_ok), _rules_detail),
    ]
    if needs_eg:
        checks.append((
            "Element Group (Forma model) set",
            bool(st.session_state["element_group_id"]),
            st.session_state["element_group_id"] or "(empty)",
        ))
    else:
        checks.append((
            "Data source",
            True,
            "open Revit document",
        ))
    if needs_project:
        checks.append(
            ("Project ID set", bool(st.session_state["project_id"]),
             st.session_state["project_id"] or "(empty)")
        )

    st.subheader("Pre-flight")
    # UX: one row per check so label and value stay aligned (two independent
    # column loops drifted apart — st.code rows are taller than st.write rows).
    for label, ok, value in checks:
        col_a, col_b = st.columns([2, 5], vertical_alignment="center")
        col_a.write(f"{'🟢' if ok else '🔴'} {label}")
        col_b.code(value, language="text")

    ready = all(ok for _, ok, _ in checks)
    if not ready:
        st.warning(
            "Fix the red items in the **Setup** tab before launching."
            " The button below is disabled until pre-flight passes."
        )

    # ── Launch ──────────────────────────────────────────────────────────────
    st.subheader(f"Launch (`--{mode}`)")
    argv_preview = _build_orchestrator_argv(mode)
    with st.expander("Command preview"):
        st.code(" ".join(argv_preview), language="bash")

    # H5: a wall-clock cap so a Revit modal dialog / unreachable addin can't hang
    # the run (and the whole UI) forever — the old blocking stream had no escape
    # but killing the server.
    run_timeout_min = st.number_input(
        "⏱️ Giới hạn thời gian chạy (phút · 0 = không giới hạn)",
        min_value=0, max_value=120, value=10,
        help="Hết giờ → tiến trình bị dừng an toàn, log một phần được giữ. Đặt cao "
             "hơn cho model lớn; 0 để tắt (không khuyến nghị khi demo).",
    )

    launch = st.button(
        f"🚀 Run `--{mode}`",
        type="primary",
        disabled=not ready or st.session_state["is_running"],
    )

    log_box = st.empty()

    if launch:
        st.session_state["is_running"] = True
        with st.spinner(f"Running `--{mode}` ... (live tail below; full log saved to runs/run-<id>/)"):
            try:
                result = _launch_guarded(
                    argv_preview, _build_env(), log_box,
                    timeout_s=(int(run_timeout_min) * 60) or None,
                )
            finally:
                st.session_state["is_running"] = False

        if result is None:
            return  # blocked: another run is still alive (message already shown)
        rc, full_log = result

        # Capture run_id from the log -- final summary always prints it.
        # Take the LAST occurrence to avoid catching any quoted example.
        ids = _RUN_ID_RE.findall(full_log)
        if ids:
            rid = ids[-1]
            st.session_state["last_run_id"] = rid
            st.session_state["selected_run_id"] = rid

        if rc == 0:
            st.success(f"✅ Run completed (exit 0). Run ID: `{st.session_state['last_run_id'] or '(not captured)'}`")
            st.info(
                "Open the **Results** tab to inspect outcomes, or **Trend**"
                " to compare against earlier runs."
            )
        else:
            st.error(f"❌ Run failed (exit {rc}). See log above + runs/run-<id>/trace.md.")


def _most_recent_run_folder() -> Path | None:
    """Return the runs/run-* folder with the newest mtime, or None if empty."""
    if not RUNS_DIR.exists():
        return None
    folders = [
        p for p in RUNS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("run-")
    ]
    if not folders:
        return None
    return max(folders, key=lambda p: p.stat().st_mtime)


def _load_outcomes(run_folder: Path) -> dict | None:
    """Parse outcomes.json inside the run folder. None if missing or bad."""
    path = run_folder / "outcomes.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_metadata(run_folder: Path) -> dict | None:
    path = run_folder / "metadata.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# Medium (i18n): localized bucket labels for the findings table — the raw engine
# key ("non_compliant") used to leak straight into the user-facing "bucket" column.
_BUCKET_LABELS: dict[str, str] = {
    "compliant": "Đạt",
    "non_compliant": "Không đạt",
    "manual_review": "Chờ người duyệt",
    "missing_data": "Thiếu dữ liệu",
}


def _findings_to_rows(items: list[dict], bucket: str) -> list[dict]:
    """Flatten findings into table rows suitable for st.dataframe.

    QW-1: surface element_name (human-readable, e.g. "Closet 11A") as the
    primary "element" column; keep the URN as a secondary column for
    deep-linking. Falls back to truncated URN when name is absent.
    """
    rows = []
    for f in items:
        urn = f.get("element_id") or "?"
        name = f.get("element_name") or urn[:32] + ("..." if len(urn) > 32 else "")
        cur = f.get("current_value")
        proposed = f.get("suggested_value")
        # v1.4-K22: an inherit rule with an empty value shows the host source.
        inh = f.get("inherited_from")
        if inh not in (None, ""):
            cur_disp = f"(trống) ⤺ host: {inh}"
        else:
            cur_disp = "(missing)" if cur in (None, "") else cur
        rows.append({
            # v1.4-K22.1: separate the instance ID from the (type/instance) name.
            # For type-level params the name IS the family type (e.g. doors show
            # "750 x 2000mm"); for instance params it's the instance name.
            "element id": urn,
            "element name": name,
            "parameter": f.get("parameter", "?"),
            # v1.4-K9: show the offending current value + the proposed fix (if any)
            # so a reviewer sees WHY it fails and what would change.
            "current": cur_disp,
            "→ proposed": "" if proposed in (None, "") else proposed,
            "bucket": _BUCKET_LABELS.get(bucket, bucket),
            "severity": f.get("severity", "?").replace("severity_", "").upper(),
            "rule_id": f.get("rule_id", "?"),
            "message": (f.get("message") or "")[:120],
            "citation": f.get("citation") or "",
        })
    return rows


def render_results_tab() -> None:
    """Results tab: 4-state outcome metrics + filterable findings table.

    Loads runs/run-<id>/outcomes.json. By default shows the most recent run
    (or whichever the Run History tab pinned via st.session_state).
    """
    st.header("📊 Results")

    # ── Run selector ────────────────────────────────────────────────────────
    selected = st.session_state.get("selected_run_id")
    if selected:
        folder = RUNS_DIR / selected
        if not folder.exists():
            st.warning(f"Selected run `{selected}` no longer exists. Falling back to latest.")
            folder = _most_recent_run_folder()
    else:
        folder = _most_recent_run_folder()

    if folder is None:
        st.info("No runs found yet. Launch one from the **Run** tab.")
        return

    metadata = _load_metadata(folder) or {}
    outcomes = _load_outcomes(folder) or {}

    # Top-row summary card
    col_id, col_meta = st.columns([1, 2])
    with col_id:
        st.subheader(folder.name)
        st.caption(f"Mode: `{metadata.get('mode','?')}` -- Status: `{metadata.get('status','?')}`")
    with col_meta:
        if metadata.get("started_at"):
            st.caption(f"Started: {metadata['started_at']}")
        if metadata.get("duration_seconds") is not None:
            st.caption(f"Duration: {metadata['duration_seconds']:.2f}s")
        if metadata.get("project_id"):
            st.code(metadata["project_id"], language="text")

    # ── 4-state cards ───────────────────────────────────────────────────────
    summary = outcomes.get("outcomes_summary") or {}
    total = summary.get("total") or 0
    compliant = summary.get("compliant") or 0
    pct = f"{(compliant / total * 100):.1f}%" if total else "n/a"

    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✓ Compliant", value=str(compliant), delta=pct, delta_color="off")
    c2.metric("✗ Non-Compliant", value=str(summary.get("non_compliant", 0)))
    c3.metric("⚑ Manual Review", value=str(summary.get("manual_review", 0)))
    c4.metric("? Missing Data", value=str(summary.get("missing_data", 0)))
    st.caption(f"Total checks evaluated: **{total}**")

    # ── Pending approval banner ──────────────────────────────────────────────
    _pending = [
        f for f in (outcomes.get("proposed_fixes") or [])
        if not f.get("executed")
        and f.get("autonomy") == "approve"
        # v1.4-K11: only writable fixes — a None value can't be set_parameter'd
        # (those now re-route to Path A as issues, but guard defensively).
        and f.get("new_value") is not None
    ]
    if _pending:
        # v1.4-K11: approval is consolidated into ONE place — the 📥 Approvals
        # tab (the governed ACC-status flow). Results only POINTS there now
        # (the old inline "Approve & Execute" / --apply-approved is removed to
        # avoid two competing approval mechanisms).
        _params = sorted({_f.get("parameter", "?") for _f in _pending})
        st.divider()
        st.info(
            f"⏳ Run này có **{len(_pending)}** đề xuất sửa Revit cần duyệt "
            f"({', '.join(_params)}) → sang tab **📥 Approvals** để xem "
            "*current → proposed* và duyệt (đặt issue ACC sang *In progress*, "
            "rồi *Apply approved now*)."
        )

    # ── Findings table (all 3 buckets combined, with filter chips) ──────────
    st.subheader("Findings")
    nc = outcomes.get("non_compliant") or []
    mr = outcomes.get("manual_review_items") or []
    md = outcomes.get("missing_data_items") or []
    rows: list[dict] = []
    rows += _findings_to_rows(nc, "non_compliant")
    rows += _findings_to_rows(mr, "manual_review")
    rows += _findings_to_rows(md, "missing_data")

    # Phase 2 (guarded): surface the advisory LLM diagnosis as an extra column,
    # only when a finding actually carries one. No-op for Phase-1 runs → the
    # table is unchanged. Rows are parallel to (nc + mr + md) by construction.
    from bim_orchestrator.ui_phase2 import attach_diagnosis_column
    attach_diagnosis_column(rows, [*nc, *mr, *md])

    if not rows:
        st.success("🎉 Zero findings across all 3 buckets -- model is fully compliant.")
    else:
        # v1.4-K11: scope the filter keys to THIS run so a stale selection from a
        # previous run (e.g. Severity=MEDIUM) doesn't carry over and hide a new
        # run's findings (which may be HIGH) → "Showing 0 of N". Each run starts
        # with all buckets/severities/rules selected (show everything).
        _rk = folder.name
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            buckets = sorted({r["bucket"] for r in rows})
            bucket_pick = st.multiselect(
                "Bucket", options=buckets, default=buckets, key=f"results_filter_bucket_{_rk}",
            )
        with col_f2:
            severities = sorted({r["severity"] for r in rows})
            sev_pick = st.multiselect(
                "Severity", options=severities, default=severities, key=f"results_filter_sev_{_rk}",
            )
        with col_f3:
            rules = sorted({r["rule_id"] for r in rows})
            rule_pick = st.multiselect(
                "Rule", options=rules, default=rules, key=f"results_filter_rule_{_rk}",
            )

        filtered = [
            r for r in rows
            if r["bucket"] in bucket_pick
            and r["severity"] in sev_pick
            and r["rule_id"] in rule_pick
        ]
        st.caption(f"Showing **{len(filtered)}** of **{len(rows)}** findings.")
        # v1.4-K22.1: tighten the metadata columns (bucket/severity/rule_id) and
        # give room to what the reviewer reads — element id/name, current, proposed.
        _colcfg = {
            "element id":   st.column_config.TextColumn("element id", width="small"),
            "element name": st.column_config.TextColumn("element name", width="medium"),
            "parameter":    st.column_config.TextColumn("parameter", width="small"),
            "current":      st.column_config.TextColumn("current", width="medium"),
            "→ proposed":   st.column_config.TextColumn("→ proposed", width="small"),
            "bucket":       st.column_config.TextColumn("bucket", width="small"),
            "severity":     st.column_config.TextColumn("sev", width="small"),
            "rule_id":      st.column_config.TextColumn("rule_id", width="small"),
            "message":      st.column_config.TextColumn("message", width="medium"),
            "citation":     st.column_config.TextColumn("cite", width="small"),
        }
        st.dataframe(
            filtered, use_container_width=True, height=400,
            column_config=_colcfg,
            column_order=["element id", "element name", "parameter", "current",
                          "→ proposed", "bucket", "severity", "rule_id",
                          "message", "citation"],
        )
        # UX: export the CURRENTLY FILTERED view — BIM managers paste this into
        # coordination reports. utf-8-sig so Excel renders Vietnamese correctly.
        if filtered:
            import csv
            import io

            _buf = io.StringIO()
            _w = csv.DictWriter(_buf, fieldnames=list(filtered[0].keys()))
            _w.writeheader()
            _w.writerows(filtered)
            st.download_button(
                f"⬇️ Tải CSV ({len(filtered)} findings đang lọc)",
                data=_buf.getvalue().encode("utf-8-sig"),
                file_name=f"{folder.name}_findings.csv",
                mime="text/csv",
            )

    # ── File links ──────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Artefacts in this run folder")
    artefacts = [
        ("report.md", "Per-run audit report (human)"),
        # v1.5-R1: trust-but-verify audit — was missing from this list even
        # though the run writes it alongside report.md.
        ("verification_report.md", "Verification report (native re-check recipes)"),
        ("trace.md", "Reasoning trace (debug)"),
        ("findings.json", "Non-compliant findings (machine)"),
        ("outcomes.json", "All 4 buckets + summary"),
        ("review_queue.md", "Manual review items"),
        ("data_quality_report.md", "Missing data report"),
        ("metadata.json", "Run shape + counts"),
    ]
    cols = st.columns(2)
    for idx, (fname, descr) in enumerate(artefacts):
        col = cols[idx % 2]
        path = folder / fname
        if path.exists():
            col.write(f"📄 [`{fname}`]({path.as_uri()}) -- {descr}")
        else:
            col.caption(f"(missing) `{fname}` -- {descr}")


def render_trend_tab() -> None:
    """Trend tab: compliance % line chart + diff cards + trend.md preview."""
    st.header("📈 Trend")

    # Re-use the existing pure-function helpers from the bim_orchestrator package
    from bim_orchestrator.reports import render_trend_report, write_trend_report
    from bim_orchestrator.run_recorder import diff_outcomes, list_runs

    rows = list_runs(RUNS_DIR)
    if not rows:
        st.info("No runs in `runs/` yet. Kick one off from the **Run** tab.")
        return

    # Refresh trend.md when the run set changed (or the file vanished) — NOT on
    # every rerun: st.tabs executes every tab on every widget interaction, so an
    # unconditional write here rewrote the file on every click anywhere in the app.
    _trend_key = f"{rows[0].get('run_id', '')}:{len(rows)}"
    if (
        st.session_state.get("_trend_written_for") != _trend_key
        or not (RUNS_DIR / "trend.md").exists()
    ):
        try:
            write_trend_report(RUNS_DIR)
            st.session_state["_trend_written_for"] = _trend_key
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not refresh trend.md: {exc}")

    st.caption(f"Scanning **{len(rows)}** run(s) from `{RUNS_DIR}` (newest first).")

    # ── Compliance % line chart (plotly) ────────────────────────────────────
    chart_rows: list[dict] = []
    for r in rows:
        summary = r.get("outcomes_summary") or {}
        total = summary.get("total") or 0
        compliant = summary.get("compliant") or 0
        pct = (compliant / total * 100) if total else None
        if pct is not None:
            chart_rows.append({
                "run_id": r.get("run_id", "?"),
                "started_at": r.get("started_at"),
                "compliance_pct": round(pct, 1),
                "non_compliant": summary.get("non_compliant", 0),
                "missing_data": summary.get("missing_data", 0),
            })
    # Plotly needs oldest-first for a left-to-right "progress" reading
    chart_rows.sort(key=lambda r: r.get("started_at") or "")

    if chart_rows:
        try:
            import plotly.express as px

            fig = px.line(
                chart_rows,
                x="started_at",
                y="compliance_pct",
                hover_data=["run_id", "non_compliant", "missing_data"],
                markers=True,
                title="Compliance % over time",
            )
            fig.update_layout(yaxis_range=[0, 100], height=350)
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.warning("plotly not installed -- run `uv sync` to enable the trend chart.")
    else:
        st.caption("Not enough data to chart compliance % yet.")

    # ── Latest vs previous diff cards ───────────────────────────────────────
    # `rows` is newest-first; load outcomes for top two to compute the diff
    def _outcomes_for(run_id: str) -> dict | None:
        return _load_outcomes(RUNS_DIR / run_id) if run_id else None

    latest_outcomes = _outcomes_for(rows[0].get("run_id", ""))
    prev_outcomes = _outcomes_for(rows[1].get("run_id", "")) if len(rows) > 1 else None

    if latest_outcomes is not None:
        diff = diff_outcomes(prev_outcomes, latest_outcomes)
        st.subheader("Latest run vs previous")
        d1, d2, d3 = st.columns(3)
        d1.metric("Resolved", len(diff["resolved"]))
        d2.metric("Newly introduced", len(diff["newly_introduced"]))
        d3.metric("Persistent", len(diff["persistent"]))

    # ── Embedded trend.md preview ───────────────────────────────────────────
    trend_path = RUNS_DIR / "trend.md"
    if trend_path.exists():
        with st.expander("📜 Full trend report (runs/trend.md)", expanded=False):
            st.markdown(trend_path.read_text(encoding="utf-8"))


def render_history_tab() -> None:
    """History tab: list every run + let user pin one to inspect in Results."""
    st.header("📜 Run History")

    from bim_orchestrator.run_recorder import list_runs

    rows = list_runs(RUNS_DIR)
    if not rows:
        st.info("No runs in `runs/` yet. Kick one off from the **Run** tab.")
        return

    # Flatten metadata into a UI-friendly table
    table_rows = [
        {
            "run_id": r.get("run_id", "?"),
            "mode": r.get("mode", "?"),
            "status": r.get("status", "?"),
            "started_at": r.get("started_at", "?"),
            "duration_s": r.get("duration_seconds"),
            "NC": r.get("non_compliant_count"),
            "MR": r.get("manual_review_count"),
            "MD": r.get("missing_data_count"),
        }
        for r in rows
    ]
    st.caption(f"Total: **{len(table_rows)}** run(s). Newest first.")

    selection = st.dataframe(
        table_rows,
        use_container_width=True,
        height=420,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="history_table",
    )
    selected_rows = selection.selection.rows if hasattr(selection, "selection") else []
    if selected_rows:
        idx = selected_rows[0]
        run_id = table_rows[idx]["run_id"]
        if run_id != st.session_state.get("selected_run_id"):
            st.session_state["selected_run_id"] = run_id
            st.success(
                f"Selected `{run_id}` -- switch to the **Results** tab to inspect."
            )


# ──────────────────────────────────────────────────────────────────────────
# v1.4 D0.5: Rule Builder tab — NL → Claude API → editable form → YAML
# ──────────────────────────────────────────────────────────────────────────

# ── Rule Builder constants ─────────────────────────────────────────────────

# v1.4-K22: the OFFERED set (shown in the Rule Builder dropdown). The legacy
# requirements below are NO LONGER offered for new rules — they're fully
# subsumed (positive_number/numeric_min/numeric_min_conditional → numeric_compare
# [+ scope_filter]; fire_rating_ge → relation_compare compare_kind=fire_rating).
# The ENGINE still evaluates them (old YAMLs keep working); the editor preserves
# a legacy value when an old rule is loaded (see the dropdown build below).
REQUIREMENT_LABELS: dict[str, str] = {
    "present_and_nonempty":     "Có giá trị, không được trống",
    "canonical_format":         "Chuẩn hoá định dạng (tự sửa — không cần pattern)",
    "numeric_compare":          "So sánh số (toán tử + ngưỡng)",
    # v1.4-K22: ONE "pattern" entry — the negate / skip-if-empty variants are now
    # two checkboxes under it (folded UI), not separate dropdown options. The
    # engine still has the 3 requirement keys; the editor maps them on save.
    "matches_regex":            "Khớp pattern (regex)",
    "unique_in_set":            "Không trùng nhau trong toàn dự án",
    "relation_compare":         "So sánh với phần tử liên kết",
}
# v1.4-K22: the pattern family — three engine requirements presented as ONE
# "Khớp pattern" option + two checkboxes (negate / only-when-present).
_PATTERN_FAMILY: tuple[str, ...] = (
    "matches_regex", "not_matches_regex", "matches_regex_if_present",
)
# Display labels for the folded variants (lists/summaries still name them).
_REQUIREMENT_FOLDED_LABELS: dict[str, str] = {
    "matches_regex_if_present": "Khớp pattern (chỉ khi có giá trị)",
    "not_matches_regex":        "Khớp pattern (phủ định — KHÔNG khớp)",
}
# Display-only labels for legacy requirements — used to render an old rule's
# requirement in lists + preserve it in the editor, NEVER offered for new rules.
_REQUIREMENT_LEGACY_LABELS: dict[str, str] = {
    "positive_number":          "Là số dương (cũ → 'So sánh số')",
    "numeric_min":              "Số tối thiểu (cũ → 'So sánh số')",
    "numeric_min_conditional":  "Số tối thiểu có lọc (cũ → 'So sánh số' + Lọc phạm vi)",
    "fire_rating_ge":           "≥ Fire rating liên kết (cũ → 'So sánh phần tử liên kết')",
}
# Combined lookup for DISPLAY (lists, summaries) — offered + folded + legacy.
REQUIREMENT_LABELS_ALL: dict[str, str] = {
    **REQUIREMENT_LABELS, **_REQUIREMENT_FOLDED_LABELS, **_REQUIREMENT_LEGACY_LABELS,
}

# v1.4-K10: severity as a plain LEVEL (importance), decoupled from the check
# kind. The Rule Builder writes this to `severity_level`; the category/kind is
# auto-derived from the requirement for reporting.
SEVERITY_LEVEL_LABELS: dict[str, str] = {
    "severity_low":    "🟢 Thấp",
    "severity_medium": "🟡 Trung bình",
    "severity_high":   "🔴 Cao",
}
# v1.4-K13: normalize kinds = unit DIMENSIONS + the non-quantity kinds. The
# kind picks how the value is parsed; the format token picks the OUTPUT unit.
NORMALIZE_KIND_LABELS: dict[str, str] = {
    "auto":        "🪄 Tự động — engine tự thử & chọn (cần pattern)",
    "duration":    "⏱️ Thời gian (min/hr)",
    "length":      "📏 Chiều dài (mm/cm/m)",
    "area":        "🟦 Diện tích (m²/sf)",
    "fire_rating": "🔥 Fire rating (= thời gian)",
    "family_name": "🔤 Tên: gộp separator → _ (đơn giản)",
    "template":    "🧩 Tên: regex→template (đổi cấu trúc)",
    "map":         "🗂️ Bảng ánh xạ cố định (text)",
    "reference":   "🗃️ Bảng giá trị chuẩn (reference set)",
}
# Default output format per kind (was hard-wired to "{h}-hour" for everything).
NORMALIZE_DEFAULT_FMT: dict[str, str] = {
    "duration":    "{h}-hour",
    "fire_rating": "{h}-hour",
    "length":      "{mm} mm",
    "area":        "{m2} m²",
}
# A representative sample input per kind, for the live preview.
NORMALIZE_SAMPLE: dict[str, str] = {
    "duration": "180 MIN", "fire_rating": "180 MIN",
    "length": "2.4 m", "area": "120 sf", "family_name": "ADSK-Fur-Chair",
    "template": "ADSK Fur Chair Viper",
}


def _parse_map_lines(raw: str) -> dict[str, str]:
    """Parse a ``normalize_kind=map`` editor: one ``variant = canonical`` per line.

    Keys are lower-cased (the lookup is case-insensitive); blank lines and lines
    without ``=`` are skipped. ``"NR = Not Rated\\n0 = Not Rated"`` →
    ``{"nr": "Not Rated", "0": "Not Rated"}``.
    """
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            out[k.lower()] = v
    return out


def _parse_reference_lines(raw: str) -> list[dict]:
    """Parse the reference editor: one ``canonical = alias1, alias2`` per line.

    Aliases are optional. ``"Oak = white oak, wood-oak"`` →
    ``{"canonical": "Oak", "aliases": ["white oak", "wood-oak"]}``. Blank lines
    and lines with an empty canonical are skipped.
    """
    out: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        canon, sep, rest = line.partition("=")
        canon = canon.strip()
        if not canon:
            continue
        aliases = [a.strip() for a in rest.split(",") if a.strip()] if sep else []
        out.append({"canonical": canon, "aliases": aliases})
    return out


def _write_reference_file(name: str, entries: list[dict], *, case_sensitive: bool = False):
    """Write ``config/reference.<name>.yaml`` for a Rule Builder reference set."""
    import yaml as _yaml
    data = {"name": name, "case_sensitive": case_sensitive, "entries": entries}
    path = CONFIG_DIR / f"reference.{name}.yaml"
    path.write_text(
        _yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _reference_needs_overwrite(
    name: str, entries: list[dict], *, case_sensitive: bool = False
) -> bool:
    """True iff ``config/reference.<name>.yaml`` exists AND differs from the proposed
    content — so the Save handler can confirm before clobbering a table that other
    rules may already cite (C1). A non-existent file → False (fresh write, no prompt)."""
    path = CONFIG_DIR / f"reference.{name}.yaml"
    if not path.exists():
        return False
    try:
        import yaml as _yaml
        cur = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                                       # noqa: BLE001
        return True  # unreadable/corrupt → treat as a collision, ask first
    return cur != {"name": name, "case_sensitive": case_sensitive, "entries": entries}

# v1.4-K10: comparison operators for numeric_compare / relation_compare.
COMPARISON_OPERATORS: list[str] = [">=", ">", "<=", "<", "==", "!="]
COMPARE_KIND_LABELS: dict[str, str] = {
    "numeric":     "Số",
    "fire_rating": "Fire rating (HR/MIN → phút)",
    "string":      "Chuỗi (==, !=)",
}

SEVERITY_LABELS: dict[str, str] = {
    "missing_required_param": "⛔ Thiếu thông số bắt buộc",
    "value_out_of_range":     "⚠️  Giá trị ngoài ngưỡng",
    "naming_violation":       "📛 Vi phạm quy tắc đặt tên",
    "uniqueness_violation":   "🔁 Trùng lặp (duplicate)",
    "fire_rating_violation":  "🔥 Vi phạm fire rating",
    "missing_data":           "❓ Thiếu dữ liệu",
}

UNIT_OPTIONS = ["m", "mm", "cm", "ft", "m²", "ft²", "m³", "ft³"]

# Autocomplete hints per catalog display name — shown as help text
_PARAM_HINTS: dict[str, list[str]] = {
    "Rooms":               ["Name", "Number", "Department", "Occupancy", "Area", "Unbounded Height", "Level", "Comments"],
    "Walls":               ["Type", "Fire Rating", "Width", "Length", "Area", "Unconnected Height"],
    "Doors":               ["Mark", "Type Mark", "Fire Rating", "Width", "Height", "Level", "Comments"],
    "Windows":             ["Mark", "Type Mark", "Width", "Height", "Sill Height", "Level"],
    "Structural Columns":  ["Mark", "Type", "Family Name", "Level", "Length", "Base Level", "Top Level", "Comments"],
    "Structural Framing":  ["Mark", "Type", "Family Name", "Level", "Length", "Comments"],
    "Floors":              ["Type", "Level", "Thickness", "Area", "Comments"],
    "Ceilings":            ["Type", "Level", "Height Offset From Level", "Area", "Comments"],
    "Generic Models":      ["Mark", "Type", "Family Name", "Level", "Comments"],
    "Furniture":           ["Mark", "Type", "Family Name", "Level", "Comments"],
    "Ducts":               ["Mark", "Type", "System Type", "Level", "Size", "Length"],
    "Pipes":               ["Mark", "Type", "System Type", "Level", "Size", "Length"],
}

# ──────────────────────────────────────────────────────────────────────────
# v1.4 D0.5: Rule Builder tab -- NL -> Claude API -> editable form -> YAML
# ──────────────────────────────────────────────────────────────────────────
#
# B16 (Phase 3b M2-A, SPEC_3B_M2_RULE_BUILDER_NOW.md): the NL-extraction
# prompt, grounding, LLM-draft call, and the two save-time enforcement
# guards moved to bim_orchestrator.rule_builder_core so the M2 AutoAudit UI
# builder endpoints (service/routes_builder.py) share the EXACT same logic
# instead of a second implementation drifting apart. The old private names
# stay as thin aliases below (mechanical -- callers throughout this file are
# unchanged) and the existing test suite (tests/test_streamlit_argv.py)
# exercises them through this module, pinning behaviour didn't shift.
from bim_orchestrator.rule_builder_core import (
    CATEGORY_NOTES as _CATEGORY_NOTES,
    INTENT_ALIASES as _INTENT_ALIASES,
    RB_EXTRACT_SYSTEM as _RB_EXTRACT_SYSTEM,
)
from bim_orchestrator.rule_builder_core import (
    LLMNotConfiguredError as _LLMNotConfiguredError,
)
from bim_orchestrator.rule_builder_core import (
    RuleDraftError as _RuleDraftError,
)
from bim_orchestrator.rule_builder_core import (
    draft_rule as _draft_rule,
)
from bim_orchestrator.rule_builder_core import (
    enforce_reference_membership as _enforce_reference_membership,
)
from bim_orchestrator.rule_builder_core import (
    enforce_unique_autofix as _enforce_unique_autofix,
)
from bim_orchestrator.rule_builder_core import (
    grounding_block as _rb_grounding_block,
)
from bim_orchestrator.rule_builder_core import (
    validate_rule as _validate_rule,
)


def _call_claude_for_rule(description: str, *, catalog_grounded: bool = False) -> dict | None:
    """NL -> one Rule dict. Thin wrapper over rule_builder_core.draft_rule (B16):
    translates its exceptions into this tab's old "st.error + return None"
    contract. ``catalog_grounded`` is accepted for call-site compatibility but
    draft_rule always grounds with the catalog now (the only caller here
    already always passed True -- see the "merged v2 tab" note above).
    """
    try:
        rule, _warnings = _draft_rule(description)
    except _LLMNotConfiguredError as exc:
        if not exc.silent:
            st.error(str(exc))
        return None
    except _RuleDraftError as exc:
        st.error(str(exc))
        return None
    return rule



def _get_catalog_categories() -> list[str]:
    """Return sorted display names from OSTCatalog; fall back to _PARAM_HINTS keys."""
    try:
        jty = _load_json_to_yaml_module()
        catalog = jty.OSTCatalog.load()
        return sorted({e.display for e in catalog._entries})
    except Exception:
        return sorted(_PARAM_HINTS.keys())


def _slugify(text: str) -> str:
    """Lowercase, spaces → underscores, strip non-alphanum."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    return text.strip("_") or "rule"


# ── Parameter Catalog wiring (folded into the main Rule Builder tab) ──────────
# The rule's category (a display label) resolves to an OST via OSTCatalog, then the
# param catalog supplies the valid built-in params + their storage/binding/dimension
# so the form offers a param dropdown, refuses read-only write targets, and pre-fills
# the write target + unit. (Was an experimental v2 tab; merged into the full-UI tab.)


def _load_param_catalog_cached():
    """Load the parameter catalog (None if unavailable).

    ``load_param_catalog`` already caches by resolved path, so no extra Streamlit
    cache is needed (and a ``@st.cache_resource`` here would break the import-time
    test mock, which can't proxy a cached resource).
    """
    try:
        from bim_orchestrator.policies.param_catalog import load_param_catalog
        return load_param_catalog()
    except Exception:                                       # noqa: BLE001
        return None


def _ost_for_display(display: str) -> str | None:
    """Resolve a category display label → Revit OST (the catalog join key)."""
    try:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        return OSTCatalog.load().resolve(display, backend="revit")
    except Exception:                                       # noqa: BLE001
        return None


def _load_shared_conventions_cached():
    """Load the shared-parameter conventions (None if unavailable).

    ``load_shared_param_conventions`` caches by resolved path; like the param
    catalog loader, no ``@st.cache_resource`` (it would break the import-time mock).
    """
    try:
        from bim_orchestrator.policies.shared_params import load_shared_param_conventions
        return load_shared_param_conventions()
    except Exception:                                       # noqa: BLE001
        return None


def _catalog_params_for(display: str) -> list:
    """ParamSpec list for a category display label ([] if not catalogued)."""
    pcat = _load_param_catalog_cached()
    ost = _ost_for_display(display)
    return pcat.params_for(ost) if (pcat and ost) else []


def _param_option_label(spec) -> str:
    """Dropdown label: name + binding + a write/read-only/rename badge."""
    if spec.rename_only:
        badge = "✎ rename"
    elif spec.writable:
        badge = "✎ ghi được"
    else:
        badge = "🔒 read-only"
    unit = f" · {spec.dimension}" if spec.dimension not in ("text", "none") else ""
    return f"{spec.name} · {spec.binding}{unit} · {badge}"


# Suggested Rule.unit default per catalog dimension (the form keeps the full list).
_DIM_UNIT_DEFAULT = {"length": "mm", "area": "m²", "volume": "m³"}

# _CATEGORY_NOTES / _INTENT_ALIASES moved to rule_builder_core.py (B16) — see the
# alias imports near the top of the Rule Builder section above.


def _alias_param(ost: str | None, intent: str, names: list[str]) -> str | None:
    """Map an intent phrase → a real catalog param via _INTENT_ALIASES (longest
    matching alias wins). Returns the mapped name only if it's in ``names``."""
    if not ost or ost not in _INTENT_ALIASES or not intent:
        return None
    low = intent.strip().lower()
    best = None
    for phrase, target in _INTENT_ALIASES[ost].items():
        if phrase in low and target in names and (best is None or len(phrase) > len(best[0])):
            best = (phrase, target)
    return best[1] if best else None


def _suggest_param_index(draft_param: str, names: list[str], ost: str | None = None) -> int:
    """Index into ``names + [CUSTOM]`` for the param dropdown's default selection.

    Auto-suggest so the free-text box stays hidden unless the user opts out:
    exact (case-insensitive) match wins; else a known intent alias ("clear width"
    → "Actual Run Width", "ceiling height" → "Unbounded Height"); else the best
    token-overlap candidate; else the first param when the intent is empty; else
    the CUSTOM sentinel (last index).
    """
    custom_idx = len(names)
    if not names:
        return custom_idx
    p = (draft_param or "").strip().lower()
    if not p:
        return 0  # empty intent → suggest the first catalogued param
    for i, n in enumerate(names):
        if n.lower() == p:
            return i
    # known intent → param alias (the non-obvious QA mappings)
    aliased = _alias_param(ost, p, names)
    if aliased is not None:
        return names.index(aliased)
    ptoks = set(re.findall(r"[a-z0-9]+", p))
    best_i, best_score = custom_idx, 0
    for i, n in enumerate(names):
        score = len(ptoks & set(re.findall(r"[a-z0-9]+", n.lower())))
        if score > best_score:
            best_i, best_score = i, score
    return best_i if best_score > 0 else custom_idx


def _migrate_legacy_rule(draft: dict) -> dict:
    """Up-convert legacy requirements to their modern equivalent FOR THE EDITOR.

    Lossless: ``numeric_min`` → ``numeric_compare`` (>=); ``positive_number`` →
    ``numeric_compare`` (> 0); ``numeric_min_conditional`` → ``numeric_compare``
    (>=) keeping when_param/when_pattern (→ scope_filter); ``fire_rating_ge`` →
    ``relation_compare`` (fire_rating, >=). The ENGINE still evaluates the legacy
    keys (old YAMLs keep working) — this only modernises what the FORM shows, so
    re-saving writes the modern form. Disk is untouched until the user saves.
    Returns a copy; non-legacy drafts pass through unchanged.
    """
    if not isinstance(draft, dict):
        return draft
    req = draft.get("requirement")
    if req not in ("numeric_min", "positive_number", "numeric_min_conditional", "fire_rating_ge"):
        return draft
    d = dict(draft)
    if req in ("numeric_min", "numeric_min_conditional"):
        d["requirement"] = "numeric_compare"
        d["operator"] = draft.get("operator") or ">="
    elif req == "positive_number":
        d["requirement"] = "numeric_compare"
        d["operator"] = draft.get("operator") or ">"
        if not d.get("threshold"):
            d["threshold"] = 0.0
    elif req == "fire_rating_ge":
        d["requirement"] = "relation_compare"
        d["operator"] = draft.get("operator") or ">="
        d["compare_kind"] = draft.get("compare_kind") or "fire_rating"
        d.setdefault("other_param", "host.Fire Rating")
    return d


def _available_lookup_tables() -> list[str]:
    """Names of the lookup tables in config/lookup.<name>.yaml (for the picker)."""
    try:
        return sorted(p.stem[len("lookup."):] for p in CONFIG_DIR.glob("lookup.*.yaml"))
    except Exception:                                       # noqa: BLE001
        return []


def _parse_lookup_keys(raw: str) -> list[dict]:
    """Inline-editor keys: one ``param : dimension`` per line.

    ``"host.Fire Rating : fire_rating"`` → ``{"param": "host.Fire Rating",
    "dimension": "fire_rating"}``. Dimension defaults to ``string``; only
    ``fire_rating`` / ``string`` are accepted. Blank lines + empty params skipped.
    """
    out: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        param, sep, dim = line.partition(":")
        param = param.strip()
        dim = dim.strip().lower() if sep else "string"
        if param:
            out.append({"param": param, "dimension": dim if dim in ("fire_rating", "string") else "string"})
    return out


def _parse_lookup_rows(raw: str) -> list[dict]:
    """Inline-editor rows: one ``w1 | w2 | ... -> required`` per line.

    ``"1 HR | Corridor -> 20 min"`` → ``{"when": ["1 HR", "Corridor"], "require":
    "20 min"}``. ``*`` is a wildcard; keep it. Lines without ``->`` are skipped.
    """
    out: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or "->" not in line:
            continue
        lhs, _, require = line.partition("->")
        whens = [w.strip() for w in lhs.split("|")]
        require = require.strip()
        if whens and require and all(w != "" for w in whens):
            out.append({"when": whens, "require": require})
    return out


def _write_lookup_file(name: str, keys: list[dict], rows: list[dict],
                       description: str | None = None):
    """Write ``config/lookup.<name>.yaml`` for a Rule Builder lookup table."""
    import yaml as _yaml
    data: dict = {"name": name}
    if description:
        data["description"] = description
    data["keys"] = keys
    data["rows"] = rows
    path = CONFIG_DIR / f"lookup.{name}.yaml"
    path.write_text(_yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _render_rule_form(
    draft: dict, v: int, *, catalog_mode: bool = False, key_prefix: str = "rb"
) -> dict:
    """Render editable Rule form widgets; return current field values.

    `v` (generation version) is used as a key prefix so that when a new
    AI draft arrives, all widgets re-initialise from the fresh defaults.

    `catalog_mode` (Rule Builder v2): wire the parameter catalog — offer a param
    DROPDOWN per category, refuse read-only params as write targets, and pre-fill
    the write target (from `binding`) + unit (from `dimension`). `key_prefix`
    namespaces the widget keys so the v2 tab can render alongside the original
    without st key collisions (default "rb" keeps the original tab identical).
    """
    result: dict = {}
    # Modernise a loaded legacy requirement so the form shows the current one
    # (numeric_min → numeric_compare, etc.); disk is rewritten only on save.
    draft = _migrate_legacy_rule(draft)
    categories = _get_catalog_categories()
    cat_spec = None  # the selected param's ParamSpec (catalog_mode only)

    # ── NHẬN DẠNG ─────────────────────────────────────────────────────────
    st.markdown("**🏷️ Nhận dạng**")
    c1, c2 = st.columns([2, 3])
    with c1:
        result["id"] = st.text_input(
            "ID rule",
            value=draft.get("id", ""),
            key=f"{key_prefix}_{v}_id",
            help="Dạng lowercase, chấm phân cách: category.parameter.check",
        )
    with c2:
        result["description"] = st.text_input(
            "Mô tả ngắn (tiếng Anh — dùng trong báo cáo)",
            value=draft.get("description", ""),
            key=f"{key_prefix}_{v}_desc",
        )

    # ── GÌ CẦN KIỂM TRA ───────────────────────────────────────────────────
    st.markdown("**🔍 Gì cần kiểm tra**")

    # Category
    cur_cat = draft.get("category", categories[0] if categories else "Rooms")
    cat_idx = categories.index(cur_cat) if cur_cat in categories else 0
    result["category"] = st.selectbox(
        "Loại phần tử (Category)",
        categories,
        index=cat_idx,
        key=f"{key_prefix}_{v}_cat",
    )
    _cat_note = _CATEGORY_NOTES.get(result["category"])
    if _cat_note:
        st.caption(_cat_note)

    # Parameter — catalog dropdown (v2) or free text (current).
    specs = _catalog_params_for(result["category"]) if catalog_mode else []
    if catalog_mode and specs:
        _CUSTOM = "✏️ Khác (tự nhập / shared param)…"
        _names = [s.name for s in specs]
        _opts = _names + [_CUSTOM]
        _cur = draft.get("parameter", "")
        # Auto-suggest the closest catalog param (exact → intent alias → token
        # overlap → first), so the free-text box only appears on ✏️ Khác.
        _idx = _suggest_param_index(_cur, _names, _ost_for_display(result["category"]))
        _sel = st.selectbox(
            "Thông số Revit (Parameter)",
            _opts,
            index=_idx,
            format_func=lambda n: n if n == _CUSTOM else _param_option_label(
                next(s for s in specs if s.name == n)
            ),
            key=f"{key_prefix}_{v}_paramsel",
            help="Danh sách built-in param của category (theo param_catalog). "
                 "Chọn '✏️ Khác' cho shared/project param.",
        )
        if _sel == _CUSTOM:
            result["parameter"] = st.text_input(
                "Tên parameter (tự nhập)", value=_cur, key=f"{key_prefix}_{v}_param",
                help="Param không có trong catalog (shared/project, hoặc category loadable).",
            )
            st.caption("⚠️ Param ngoài catalog — không kiểm tra được read-only / binding.")
        else:
            result["parameter"] = _sel
            cat_spec = next(s for s in specs if s.name == _sel)
            if _cur and _cur.strip().lower() != _sel.lower():
                st.caption(f"🔗 Claude đọc “{_cur}” → catalog ánh xạ sang **{_sel}** "
                           "(đổi lại nếu chưa đúng).")
            _facts = (f"**{cat_spec.binding}** · {cat_spec.storage} · "
                      f"dimension `{cat_spec.dimension}`")
            if not cat_spec.is_write_target:
                st.warning(
                    f"🔒 `{cat_spec.name}` là **read-only** ({_facts}) — chỉ dùng để "
                    "**KIỂM TRA**, không thể làm đích **tự sửa** (Path B sẽ lỗi)."
                )
            else:
                _how = "đổi tên" if cat_spec.rename_only else "ghi"
                st.caption(f"✎ `{cat_spec.name}` — {_facts} · có thể {_how} (write target hợp lệ).")
    else:
        hints = _PARAM_HINTS.get(result["category"], [])
        hint_text = "Gợi ý: " + ", ".join(hints[:6]) if hints else "Tên parameter chính xác trong Revit"
        result["parameter"] = st.text_input(
            "Thông số Revit (Parameter)",
            value=draft.get("parameter", ""),
            key=f"{key_prefix}_{v}_param",
            help=hint_text,
        )
        if catalog_mode:
            st.caption("ℹ️ Category này chưa có trong param_catalog (loadable / chưa probe) "
                       "→ nhập param tự do.")

    # Requirement
    req_keys = list(REQUIREMENT_LABELS.keys())
    cur_req = draft.get("requirement", "present_and_nonempty")
    # v1.4-K22: the pattern family (matches_regex / not_matches_regex /
    # matches_regex_if_present) folds into ONE "Khớp pattern" entry — a loaded
    # variant selects that entry; two checkboxes below restore the exact key.
    _dropdown_req = "matches_regex" if cur_req in _PATTERN_FAMILY else cur_req
    # Preserve a TRUE legacy requirement (numeric_*/fire_rating_ge) when an old
    # rule is loaded, so the editor doesn't silently rewrite it.
    if _dropdown_req not in req_keys:
        req_keys = req_keys + [_dropdown_req]
    req_labels = [REQUIREMENT_LABELS_ALL.get(k, k) for k in req_keys]
    req_idx = req_keys.index(_dropdown_req) if _dropdown_req in req_keys else 0
    sel_req_label = st.selectbox(
        "Yêu cầu",
        req_labels,
        index=req_idx,
        key=f"{key_prefix}_{v}_req",
    )
    result["requirement"] = req_keys[req_labels.index(sel_req_label)]
    if result["requirement"] == "unique_in_set":
        st.caption("ℹ️ `unique_in_set` luôn là **Path-B auto** — khi lưu, engine sẽ "
                   "**đánh số lại** giá trị trùng (next-available, gate qua proposal). "
                   "Bỏ qua lựa chọn 'Cách xử lý' bên dưới.")

    # Conditional: numeric comparison (numeric_compare = operator + threshold;
    # legacy numeric_* keep the >= threshold form).
    _OP = COMPARISON_OPERATORS
    if result["requirement"] in ("numeric_compare", "numeric_min", "numeric_min_conditional", "positive_number"):
        c0, c1, c2 = st.columns([1, 2, 1])
        with c0:
            if result["requirement"] == "numeric_compare":
                cur_op = draft.get("operator") or ">="
                result["operator"] = st.selectbox(
                    "Toán tử", _OP,
                    index=_OP.index(cur_op) if cur_op in _OP else 0,
                    key=f"{key_prefix}_{v}_op",
                )
        with c1:
            result["threshold"] = st.number_input(
                "Ngưỡng", value=float(draft.get("threshold") or 0.0),
                step=0.1, key=f"{key_prefix}_{v}_threshold",
            )
        with c2:
            # v2: suggest the unit from the catalog dimension (length→mm, area→m², …).
            _unit_default = "m"
            if catalog_mode and cat_spec is not None:
                _unit_default = _DIM_UNIT_DEFAULT.get(cat_spec.dimension, "m")
            cur_unit = draft.get("unit") or _unit_default
            unit_idx = UNIT_OPTIONS.index(cur_unit) if cur_unit in UNIT_OPTIONS else 0
            result["unit"] = st.selectbox("Đơn vị", UNIT_OPTIONS, index=unit_idx, key=f"{key_prefix}_{v}_unit")
            if catalog_mode and cat_spec is not None and cat_spec.dimension in _DIM_UNIT_DEFAULT:
                st.caption(f"📐 catalog: `{cat_spec.name}` là **{cat_spec.dimension}**.")
        # Medium: a numeric_compare with >= / > and threshold 0 passes for EVERY
        # non-negative value → an accidental always-pass rule. Warn (don't block:
        # "== 0" / "!= 0" are legitimate uses of 0).
        if (
            result["requirement"] == "numeric_compare"
            and result.get("operator") in (">=", ">")
            and float(result.get("threshold") or 0.0) == 0.0
        ):
            st.warning(
                f"⚠️ Ngưỡng **0** với toán tử **{result.get('operator')}** khiến rule "
                "**luôn đạt** với mọi giá trị ≥ 0 — đặt ngưỡng thực tế (vd 815) hoặc đổi toán tử."
            )
    # Conditional: cross-element comparison (relation_compare; legacy fire_rating_ge).
    # Two modes: DIRECT (compare against a related param) or TABLE LOOKUP (the
    # required value is a code-table function of the host, e.g. IBC §716). The table
    # is authoritative data cited by name — like a reference set, not authored here.
    elif result["requirement"] in ("relation_compare", "fire_rating_ge"):
        _tables = _available_lookup_tables()
        _draft_lk = draft.get("lookup")
        _mode = st.radio(
            "Cách so sánh",
            ["Trực tiếp (param phần tử liên kết)", "📊 Tra bảng code (lookup)"],
            index=1 if (_draft_lk and (_tables or _draft_lk)) else 0,
            horizontal=True, key=f"{key_prefix}_{v}_relmode",
            help="Tra bảng: giá trị yêu cầu suy ra từ một BẢNG code (vd IBC §716 — "
                 "rating cửa theo use × rating tường). Bảng định nghĩa riêng trong "
                 "config/lookup.*.yaml.",
        )
        use_lookup = _mode.startswith("📊")
        if use_lookup:
            _NEW = "➕ Tạo bảng mới…"
            _opts = (_tables or []) + [_NEW]
            # default: the draft's table, else the first table, else the editor.
            _li = (_opts.index(_draft_lk) if _draft_lk in _opts
                   else (0 if _tables else len(_opts) - 1))
            cL, cR = st.columns([2, 1])
            with cL:
                lk = st.selectbox("Bảng tra (lookup table)", _opts, index=_li,
                                  key=f"{key_prefix}_{v}_lk")
            with cR:
                cur_op = draft.get("operator") or ">="
                result["operator"] = st.selectbox(
                    "Toán tử", _OP, index=_OP.index(cur_op) if cur_op in _OP else 0,
                    key=f"{key_prefix}_{v}_lkop",
                )
            result["other_param"] = None
            if lk == _NEW:
                # ── Inline table editor (advanced) — transcribe a code table ──
                result["lookup"] = None  # rule incomplete until the table is saved + picked
                result["compare_kind"] = "fire_rating"
                st.caption("✏️ **Tạo bảng tra mới** (chép từ bảng code, vd IBC Table "
                           "716.1). Lưu xong → chọn lại bảng ở dropdown để gắn vào rule.")
                _nm = st.text_input("Tên bảng (slug, không dấu cách)", value="",
                                    key=f"{key_prefix}_{v}_lknew", placeholder="ibc716")
                _kraw = st.text_area(
                    "Keys — mỗi dòng `param : dimension` (fire_rating | string)",
                    value="host.Fire Rating : fire_rating\nhost.Fire Function : string",
                    key=f"{key_prefix}_{v}_lkkeys", height=80,
                    help="param đọc từ phần tử (vd host.Fire Rating). fire_rating khớp "
                         "theo phút; string khớp không phân biệt hoa/thường.",
                )
                _rraw = st.text_area(
                    "Dòng — `giá trị1 | giá trị2 | … -> required`  ( * = mọi giá trị )",
                    value=("1 HR | Corridor -> 20 min\n1 HR | * -> 60 min\n"
                           "2 HR | * -> 90 min\n3 HR | * -> 3 HR"),
                    key=f"{key_prefix}_{v}_lkrows", height=120,
                )
                _desc = st.text_input("Mô tả (tùy chọn)", value="",
                                      key=f"{key_prefix}_{v}_lkdesc")
                _keys = _parse_lookup_keys(_kraw)
                _rows = _parse_lookup_rows(_rraw)
                _slug = _slugify(_nm.strip()) if _nm.strip() else ""
                _errs: list[str] = []
                if not _slug:
                    _errs.append("Tên bảng trống")
                if not _keys:
                    _errs.append("Cần ≥ 1 key")
                if not _rows:
                    _errs.append("Cần ≥ 1 dòng hợp lệ (`… -> required`)")
                _bad = [i + 1 for i, r in enumerate(_rows) if len(r["when"]) != len(_keys)]
                if _keys and _bad:
                    _errs.append(f"Dòng {_bad}: số giá trị ≠ số key ({len(_keys)})")
                if _errs:
                    for _e in _errs:
                        st.warning(f"⚠️ {_e}")
                else:
                    st.table([
                        {**{k["param"].replace("host.", ""): w for k, w in zip(_keys, r["when"])},
                         "→ required": r["require"]}
                        for r in _rows[:8]
                    ])
                    if st.button("💾 Lưu bảng", key=f"{key_prefix}_{v}_lksave"):
                        _write_lookup_file(_slug, _keys, _rows, _desc.strip() or None)
                        from bim_orchestrator.policies.lookup_table import clear_cache as _lc
                        _lc()  # so the picker sees the new/edited table immediately
                        st.success(f"✅ Đã tạo `config/lookup.{_slug}.yaml` — chọn lại ở dropdown để gắn.")
                        st.rerun()
            else:
                result["lookup"] = lk
                # Preview + auto compare_kind from the table's primary key dimension.
                try:
                    from bim_orchestrator.policies.lookup_table import load_lookup
                    _t = load_lookup(lk, CONFIG_DIR)
                    _prim = next((k.dimension for k in _t.keys if k.dimension == "fire_rating"),
                                 _t.keys[0].dimension if _t.keys else "string")
                    result["compare_kind"] = _prim if _prim in ("fire_rating", "string") else "numeric"
                    st.caption(f"🔑 keys: **{' × '.join(k.param for k in _t.keys)}** · "
                               f"{len(_t.rows)} dòng · compare_kind=`{result['compare_kind']}` (tự suy)")
                    st.table([
                        {**{k.param.replace('host.', ''): w for k, w in zip(_t.keys, r.when)},
                         "→ required": r.require}
                        for r in _t.rows[:6]
                    ])
                    if _t.description:
                        st.caption(f"📚 {_t.description}")
                except Exception as exc:                   # noqa: BLE001
                    result["compare_kind"] = draft.get("compare_kind") or "fire_rating"
                    st.warning(f"Không đọc được bảng `{lk}`: {exc}")
        else:
            result["lookup"] = None
            c0, c1, c2 = st.columns(3)
            with c0:
                result["other_param"] = st.text_input(
                    "Param phần tử liên kết", value=draft.get("other_param") or "host.Fire Rating",
                    key=f"{key_prefix}_{v}_other",
                    help="vd: host.Fire Rating (so với tường chứa cửa)",
                )
            with c1:
                cur_op = draft.get("operator") or ">="
                result["operator"] = st.selectbox(
                    "Toán tử", _OP, index=_OP.index(cur_op) if cur_op in _OP else 0,
                    key=f"{key_prefix}_{v}_op2",
                )
            with c2:
                ck_keys = list(COMPARE_KIND_LABELS.keys())
                ck_vals = list(COMPARE_KIND_LABELS.values())
                cur_ck = draft.get("compare_kind") or (
                    "fire_rating" if result["requirement"] == "fire_rating_ge" else "numeric"
                )
                sel_ck = st.selectbox(
                    "Kiểu so sánh", ck_vals,
                    index=ck_keys.index(cur_ck) if cur_ck in ck_keys else 0,
                    key=f"{key_prefix}_{v}_ck",
                )
                result["compare_kind"] = ck_keys[ck_vals.index(sel_ck)]
        result["threshold"] = None
        result["unit"] = None
    else:
        result["threshold"] = None
        result["unit"] = None

    # Conditional: regex pattern + the folded family flags (v1.4-K22).
    if result["requirement"] == "matches_regex":
        result["pattern"] = st.text_input(
            "Pattern (regex)",
            value=draft.get("pattern") or "",
            key=f"{key_prefix}_{v}_pattern",
            help=r"Ví dụ: ^[A-Z]-\d{3}$ sẽ khớp A-001, B-042, …",
        )
        pc1, pc2 = st.columns(2)
        with pc1:
            _neg = st.checkbox(
                "Phủ định — KHÔNG được khớp", value=(cur_req == "not_matches_regex"),
                key=f"{key_prefix}_{v}_pneg",
                help="vd: Name KHÔNG được chứa 'Copy of' / 'Default'.",
            )
        with pc2:
            _ifp = st.checkbox(
                "Chỉ kiểm khi có giá trị", value=(cur_req == "matches_regex_if_present"),
                key=f"{key_prefix}_{v}_pifp",
                help="Trống thì BỎ QUA (để rule 'Có giá trị' lo phần thiếu).",
            )
        # Map the two flags → the exact engine requirement. Negate wins (the
        # engine has no "if present AND not match" key; not_matches already passes
        # empty values, so skip-if-empty is moot under negate).
        if _neg:
            result["requirement"] = "not_matches_regex"
            if _ifp:
                st.caption("ℹ️ 'Phủ định' đã bỏ qua giá trị trống → "
                           "'chỉ khi có giá trị' không cần thiết.")
        elif _ifp:
            result["requirement"] = "matches_regex_if_present"
    else:
        result["pattern"] = None

    # v1.4-K10: UNIVERSAL scope filter (applies to ANY requirement, not just the
    # old numeric_min_conditional). "Applicable components" à la Solibri.
    _sf = draft.get("scope_filter") or {}
    with st.expander("⚙️ Lọc phạm vi áp dụng (tùy chọn)"):
        st.caption(
            "Rule chỉ áp dụng cho phần tử mà một thông số KHÁC khớp điều kiện "
            "(vd: chỉ Door có IsExternal = true; chỉ phòng Occupancy = Residential). "
            "Để trống = áp dụng toàn bộ."
        )
        c1, c2 = st.columns(2)
        with c1:
            sf_param = st.text_input(
                "Chỉ áp dụng khi thông số…",
                value=_sf.get("param") or draft.get("when_param") or "",
                key=f"{key_prefix}_{v}_sf_param", placeholder="vd: IsExternal",
            )
        with c2:
            sf_pattern = st.text_input(
                "…khớp pattern", value=_sf.get("pattern") or draft.get("when_pattern") or "",
                key=f"{key_prefix}_{v}_sf_pattern", placeholder="vd: (?i)^(true|yes)$",
            )
        result["scope_filter"] = (
            {"param": sf_param.strip(), "pattern": sf_pattern.strip()}
            if sf_param.strip() and sf_pattern.strip() else None
        )

    # ── MỨC ĐỘ + HÀNH ĐỘNG ────────────────────────────────────────────────
    st.markdown("**⚡ Mức độ + Hành động**")
    # v1.4-K12: canonical_format's fix is inherent (normalize) — no Action choice.
    is_canon = result["requirement"] == "canonical_format"
    c1 = st.container() if is_canon else None
    if not is_canon:
        c1, c2 = st.columns(2)
    with c1:
        # v1.4-K10: severity = a plain LEVEL (importance), decoupled from the
        # check kind (which is auto-derived from the requirement for reports).
        lvl_keys = list(SEVERITY_LEVEL_LABELS.keys())
        lvl_vals = list(SEVERITY_LEVEL_LABELS.values())
        cur_lvl = draft.get("severity_level") or "severity_medium"
        sel_lvl = st.selectbox(
            "Độ nghiêm trọng", lvl_vals,
            index=lvl_keys.index(cur_lvl) if cur_lvl in lvl_keys else 1,
            key=f"{key_prefix}_{v}_lvl",
        )
        result["severity_level"] = lvl_keys[lvl_vals.index(sel_lvl)]
        result["severity_tag"] = draft.get("severity_tag") or "rule_violation"
    # v1.4-K15: ONE "how to handle" selector. Path A (create issue) is just the
    # first option of the fix-strategy list — no separate Path A/B radio. A 🔧
    # strategy ALWAYS means "propose via ACC issue → approve → write Revit" (the
    # approve-gated Path B), so the user never ticks Path A vs B separately.
    da = draft.get("autofill") or {}
    dr = draft.get("remediation") or {}
    HANDLE_OPTS = {
        "📋 Chỉ tạo ACC Issue (giao người xử lý)":      "issue",
        "🔧 Tự sửa — Chuẩn hoá giá trị (normalize)":    "normalize",
        "🔧 Tự sửa — Ghép từ thông số khác (compose)":  "compose_template",
        "🔧 Tự sửa — Kế thừa từ host (inherit)":         "inherit_from_host",
        "🔧 Tự sửa — Giá trị cố định":                   "set_fixed",
    }
    if da.get("strategy") == "compose_template":
        cur_handle = "compose_template"
    elif da.get("strategy") in ("inherit_from_host", "inherit_then_normalize"):
        # inherit_then_normalize renders under the normalize handle (a host-fallback
        # checkbox); plain inherit_from_host has its own handle (K20).
        cur_handle = ("normalize" if da.get("strategy") == "inherit_then_normalize"
                      else "inherit_from_host")
    elif dr.get("new_value_strategy") == "fixed":
        cur_handle = "set_fixed"
    elif da.get("strategy") == "normalize" or dr.get("action") in ("set_parameter", "rename_element"):
        cur_handle = "normalize"
    else:
        cur_handle = "issue"

    if is_canon:
        handle = "normalize"   # canonical_format → the fix IS the normalizer
        st.caption(
            "Hành động: **tự chuẩn hoá** rồi đề xuất qua ACC proposal issue → duyệt "
            "→ ghi Revit (gate theo Độ nghiêm trọng). Không parse được → Path A. "
            "Không cần pattern."
        )
    else:
        with c2:
            hk = list(HANDLE_OPTS.keys())
            hv = list(HANDLE_OPTS.values())
            sel_handle = st.selectbox(
                "Cách xử lý khi vi phạm", hk,
                index=hv.index(cur_handle) if cur_handle in hv else 0,
                key=f"{key_prefix}_{v}_handle",
            )
            handle = HANDLE_OPTS[sel_handle]
            st.caption(
                "🔧 = đề xuất qua **ACC proposal issue → duyệt → ghi Revit** "
                "(approve-gated). 📋 = chỉ tạo issue cho người xử lý."
            )

    action = "issue" if handle == "issue" else "fix"
    strat = handle if action == "fix" else None

    if action == "fix":
        f1, f2 = st.columns(2)
        with f1:
            # Write target: an instance/type param, or rename the Family / Type.
            # v1.4-K17: "Đổi tên Family" renames the FAMILY (Family Name); "Đổi tên
            # Type" renames the family Type (Type Name). A rename writes the
            # element's Name property, not a parameter.
            # v1.4-K19: "auto" (default) lets the engine resolve the write target
            # at run time from the parameter (Family Name → rename family, Type
            # Name → rename type, Type-carried param → type, else instance). The
            # explicit options stay as an override for the ambiguous case.
            TGT_OPTS = {
                "auto":     "🪄 Tự phát hiện (Auto)",
                "instance": "Thông số (instance)",
                "type":     "Thông số (type)",
                "family":   "Đổi tên Family",
                "rename_type": "Đổi tên Type",
            }
            if dr.get("action") == "rename_element":
                cur_tgt = "rename_type" if (dr.get("target") == "type") else "family"
            else:
                cur_tgt = dr.get("target") or "auto"
            # v2: pre-fill the write target from the catalog binding (the draft
            # still wins if it pinned one). rename-only params (Family/Type Name)
            # map to the rename options; type/instance params to that binding.
            if catalog_mode and cat_spec is not None and not dr.get("target") \
                    and dr.get("action") != "rename_element":
                if cat_spec.rename_only:
                    cur_tgt = "family" if cat_spec.name == "Family Name" else "rename_type"
                else:
                    cur_tgt = cat_spec.binding  # "instance" | "type"
            tk = list(TGT_OPTS.keys())
            tgt = st.selectbox(
                "Ghi vào", tk,
                index=tk.index(cur_tgt) if cur_tgt in tk else 0,
                format_func=lambda t: TGT_OPTS[t], key=f"{key_prefix}_{v}_tgt",
            )
            if catalog_mode and cat_spec is not None:
                if not cat_spec.is_write_target:
                    st.error(
                        f"🔒 `{cat_spec.name}` read-only — **không ghi được**. Đổi sang "
                        "param khác hoặc dùng 📋 (chỉ tạo Issue)."
                    )
                else:
                    st.caption(f"🪄 catalog đề xuất **{cur_tgt}** (binding `{cat_spec.binding}`).")
            if tgt == "auto":
                st.caption(
                    "🪄 Engine **tự chọn nơi ghi** lúc chạy: `Family Name`→đổi tên "
                    "Family, `Type Name`→đổi tên Type, thông số của **type** "
                    "(vd Fire Rating)→ghi type, còn lại→instance. Chọn thủ công nếu "
                    "param nằm ở **cả 2** phía."
                )
        nfmt = None
        nmap: dict[str, str] | None = None
        nsrc = None
        nref = None
        nref_entries: list[dict] | None = None
        inherit_host_cb = False
        hpar_n = ""
        kind = None
        with f2:
            if strat == "normalize":
                # v1.4-K13/K15: kind is a DIMENSION or a name transform, not free
                # text; the format token picks the output unit / shape.
                kinds = list(NORMALIZE_KIND_LABELS.keys())
                cur_kind = da.get("normalize_kind") or "auto"
                if cur_kind not in kinds:        # preserve an unknown legacy kind
                    kinds = kinds + [cur_kind]
                kind = st.selectbox(
                    "normalize_kind (đơn vị / loại)", kinds,
                    index=kinds.index(cur_kind),
                    format_func=lambda k: NORMALIZE_KIND_LABELS.get(k, k),
                    key=f"{key_prefix}_{v}_nk",
                )
                if kind == "auto":
                    st.caption(
                        "🪄 Engine **tự thử** mọi cách chuẩn hoá (đơn vị + gộp "
                        "separator) và chọn kết quả **khớp Pattern**. Không cần khai "
                        "đơn vị/format — chỉ cần **Pattern** ở trên. (template/map "
                        "thì chọn thủ công.)"
                    )
                if kind == "map":
                    _existing = da.get("normalize_map") or {}
                    _lines = "\n".join(f"{k} = {val}" for k, val in _existing.items())
                    nmap_raw = st.text_area(
                        "Bảng ánh xạ — mỗi dòng `biến thể = giá trị chuẩn`",
                        value=_lines or "NR = Not Rated\n0 = Not Rated",
                        key=f"{key_prefix}_{v}_nmap", height=90,
                        help="Không phân biệt hoa/thường. Giá trị ngoài bảng → Path A.",
                    )
                    nmap = _parse_map_lines(nmap_raw)
                elif kind == "reference":
                    # v1.4-K21: pick an existing reference set OR define one inline.
                    import yaml as _y
                    _files = sorted(CONFIG_DIR.glob("reference.*.yaml"))
                    _names = [p.stem[len("reference."):] for p in _files]
                    _opts = ["➕ Tạo bảng mới…"] + _names
                    _cur = da.get("normalize_reference")
                    _idx = _opts.index(_cur) if _cur in _names else 0
                    _pick = st.selectbox(
                        "Bảng giá trị chuẩn (reference set)", _opts, index=_idx,
                        key=f"{key_prefix}_{v}_refpick",
                    )
                    if _pick == "➕ Tạo bảng mới…":
                        nref = st.text_input(
                            "Tên bảng (slug, không dấu cách)",
                            value="", key=f"{key_prefix}_{v}_refname",
                            placeholder="approved_materials",
                        )
                        _ent_raw = st.text_area(
                            "Giá trị chuẩn — mỗi dòng `Giá trị chuẩn = biến thể1, biến thể2`",
                            value="Oak = white oak, wood-oak\nSteel-Brushed = brushed steel",
                            key=f"{key_prefix}_{v}_refent", height=120,
                            help="Biến thể (alias) tùy chọn, cách nhau dấu phẩy. Không "
                                 "phân biệt hoa/thường + dấu cách/gạch. Ngoài bảng → Path A.",
                        )
                        nref_entries = _parse_reference_lines(_ent_raw)
                        st.caption(f"🗃️ {len(nref_entries)} giá trị chuẩn sẽ được lưu vào "
                                   f"`config/reference.{(nref or '...').strip()}.yaml`.")
                    else:
                        nref = _pick
                        try:
                            _d = _y.safe_load(
                                (CONFIG_DIR / f"reference.{_pick}.yaml").read_text(encoding="utf-8")
                            ) or {}
                            _n = len(_d.get("entries", []))
                            st.caption(f"🗃️ Dùng lại `{_pick}` ({_n} giá trị chuẩn).")
                        except Exception:                       # noqa: BLE001
                            st.caption(f"🗃️ Dùng lại `{_pick}`.")
                elif kind == "template":
                    nsrc = st.text_input(
                        "Source regex (bắt token từ tên hiện tại)",
                        value=da.get("normalize_source") or "",
                        key=f"{key_prefix}_{v}_nsrc",
                        placeholder=r"(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)[ _-]*(?P<d1>[a-z0-9]+)",
                        help="Dùng nhóm có tên (?P<fn>...) để bắt token.",
                    )
                    nfmt = st.text_input(
                        "Target template",
                        value=da.get("normalize_format") or "",
                        key=f"{key_prefix}_{v}_nfmt_t",
                        placeholder="ADSK_Fur_{fn}_{d1}",
                        help="Dựng tên chuẩn từ token: {fn},{d1} (hoặc {g1},{g2}). "
                             "Tên thiếu token → None → Path A.",
                    )
                elif kind == "family_name":
                    st.caption("Gộp dấu cách / gạch ngang → `_`. Không cần khai gì thêm.")
                else:  # quantity dimension (duration / length / area / fire_rating)
                    nfmt = st.text_input(
                        "normalize_format (output)",
                        value=da.get("normalize_format")
                        or NORMALIZE_DEFAULT_FMT.get(kind, "{h}-hour"),
                        key=f"{key_prefix}_{v}_nfmt",
                        help='Token chọn đơn vị xuất, literal text giữ nguyên. '
                             'duration: {h}=giờ,{m}=phút ("{m} Min"→"180 Min"). '
                             'length: {mm}/{cm}/{m}. area: {m2}.',
                    )
                # v1.4-K22: COMPOUND fix — when this param is empty, inherit from
                # the host THEN normalise (one rule = "present (inherit) AND in
                # canonical format"). Only for kinds normalize_value can apply
                # (auto/reference have no parser/host story here).
                if kind not in ("auto", "reference"):
                    inherit_host_cb = st.checkbox(
                        "➕ Kế thừa từ host khi trống (rồi chuẩn hoá)",
                        value=(da.get("strategy") == "inherit_then_normalize"),
                        key=f"{key_prefix}_{v}_ihn",
                        help="vd cửa trống Fire Rating → lấy của tường host → chuẩn "
                             "hoá ra format trên. Empty + host trống → Path A.",
                    )
                    if inherit_host_cb:
                        hpar_n = st.text_input(
                            "Tham số host để kế thừa (để trống = cùng tên)",
                            value=da.get("host_param") or "",
                            key=f"{key_prefix}_{v}_hparn",
                            placeholder=result.get("parameter") or "Fire Rating",
                        )
            elif strat == "compose_template":
                tmpl = st.text_input(
                    "Template", value=da.get("template") or "",
                    key=f"{key_prefix}_{v}_tmpl", placeholder="{_containing_space}-{Reference Level}-{System Name}-{seq}",
                )
            elif strat == "inherit_from_host":
                hpar = st.text_input(
                    "Tham số host để kế thừa (để trống = cùng tên)",
                    value=da.get("host_param") or "",
                    key=f"{key_prefix}_{v}_hpar",
                    placeholder=result.get("parameter") or "Fire Rating",
                    help="Khi element trống, lấy giá trị tham số này từ phần tử "
                         "host (vd cửa lấy Fire Rating của tường host). Để trống → "
                         "kế thừa tham số CÙNG TÊN. Host không có giá trị → Path A.",
                )
            elif strat == "set_fixed":
                fixed_val = st.text_input(
                    "Giá trị cố định", value=str(dr.get("new_value") or ""), key=f"{key_prefix}_{v}_fv",
                )

        if tgt == "family":
            remediation = {"action": "rename_element", "target": "family"}
        elif tgt == "rename_type":
            remediation = {"action": "rename_element", "target": "type"}
        else:
            # v1.4-K19: "auto" rides as target=auto + action=set_parameter; the
            # DesignAgent re-resolves the action (set_parameter vs rename) per
            # element, so a Family/Type-Name rule still renames correctly.
            remediation = {"action": "set_parameter", "target": tgt}

        if strat == "normalize":
            af: dict = {"strategy": "normalize", "normalize_kind": kind}
            if kind == "auto":
                pass   # engine self-selects at runtime; only the pattern is needed
            elif kind == "map":
                af["normalize_map"] = nmap or {}
            elif kind == "reference":
                # v1.4-K21: cite an inline-defined set. C1 fix — do NOT write the
                # file here: _render_rule_form runs on EVERY rerun (typing, tabbing
                # away), so writing here persisted config/reference.<name>.yaml
                # BEFORE the user clicked Save and could silently overwrite a real
                # table already in use with placeholder entries. Stash the pending
                # entries instead; the Save handler writes them (with an overwrite
                # guard) only when the user commits.
                _name = (nref or "").strip()
                if _name and nref_entries is not None:
                    result["_pending_reference"] = {"name": _name, "entries": nref_entries}
                af["normalize_reference"] = _name
                # reference requires the canonical_format contract (compliant iff
                # value is already a canonical member) — force it regardless of the
                # requirement dropdown so the rule is coherent.
                result["requirement"] = "canonical_format"
            elif kind == "template":
                af["normalize_source"] = nsrc or ""
                af["normalize_format"] = nfmt or ""
            elif kind != "family_name":
                af["normalize_format"] = nfmt or NORMALIZE_DEFAULT_FMT.get(kind, "{h}-hour")
            # v1.4-K22: the host-fallback checkbox upgrades plain normalize into the
            # COMPOUND inherit_then_normalize (empty → inherit host → normalise).
            # It checks the canonical_format contract, so force that requirement.
            if inherit_host_cb and kind not in ("auto", "reference"):
                af["strategy"] = "inherit_then_normalize"
                if (hpar_n or "").strip():
                    af["host_param"] = hpar_n.strip()
                result["requirement"] = "canonical_format"
                st.caption(
                    "🔗➡️🔧 Trống → **kế thừa host** "
                    f"(`host.{(hpar_n or '').strip() or result.get('parameter') or '—'}`) "
                    "→ **chuẩn hoá**. (Yêu cầu = Chuẩn hoá định dạng.)"
                )
            result["autofill"] = af
            result["remediation"] = remediation
            # v1.4-K11/K13/K15/K16: live preview, generalised to every kind. Catches
            # the "output ≠ pattern → silent None → Path A" trap in the UI.
            import re as _re_prev
            from bim_orchestrator.policies.normalize import (
                auto_candidates as _ac, normalize_value as _nv,
            )
            if kind == "auto":
                _pat = result.get("pattern")
                if _pat:
                    _demos = []
                    for _s in ("180 MIN", "2400 mm", "ADSK-Fur-Chair"):
                        _pick = next((c for c in _ac(_s) if _re_prev.search(_pat, c)), None)
                        if _pick:
                            _demos.append(f"`{_s}`→`{_pick}`")
                    st.caption(
                        "🪄 Auto thử & chọn theo Pattern. Ví dụ: " + " · ".join(_demos)
                        if _demos else
                        "🪄 Auto: chưa mẫu nào khớp Pattern — kiểm tra lại Pattern."
                    )
                else:
                    st.caption("🪄 Auto cần **Pattern** để chọn — khai Pattern "
                               "(Yêu cầu = Khớp định dạng).")
            elif kind == "reference":
                # Live preview: snap a couple of the declared aliases to canonical.
                try:
                    from bim_orchestrator.policies.reference import ReferenceSet
                    if nref_entries:
                        _rs = ReferenceSet.model_validate(
                            {"name": nref or "preview", "entries": nref_entries}
                        )
                        _demo = []
                        for _e in nref_entries[:2]:
                            _probe = (_e.get("aliases") or [_e["canonical"].lower()])[0]
                            _demo.append(f"`{_probe}`→`{_rs.match(_probe)}`")
                        st.caption("🔎 reference: " + " · ".join(_demo)
                                   + " · ngoài bảng → Path A")
                except Exception:                               # noqa: BLE001
                    pass
            else:
                _sample = NORMALIZE_SAMPLE.get(kind)
                if _sample is None and kind == "map" and nmap:
                    _sample = next(iter(nmap))
                if _sample is not None:
                    _out = _nv(_sample, kind, nfmt, nmap, nsrc)
                    if _out is None:
                        st.caption(f"🔎 normalize('{_sample}') → ⚠️ **None** "
                                   "(không parse được → Path A, không auto-fix)")
                    elif result.get("requirement") == "matches_regex" and result.get("pattern"):
                        _ok = _re_prev.fullmatch(result["pattern"], _out) is not None
                        st.caption(
                            f"🔎 normalize('{_sample}') → `{_out}` — "
                            + ("✅ khớp pattern (sẽ auto-fix)" if _ok else
                               "⚠️ **KHÔNG khớp pattern** → None → Path A. Sửa cho khớp.")
                        )
                    else:
                        st.caption(f"🔎 normalize('{_sample}') → `{_out}`")
        elif strat == "compose_template":
            result["autofill"] = {"strategy": "compose_template", "template": tmpl}
            result["remediation"] = remediation
        elif strat == "inherit_from_host":
            # v1.4-K20: inherit a parameter from the host element when empty. The
            # host_param defaults to the rule's own parameter (same-named).
            af = {"strategy": "inherit_from_host"}
            if (hpar or "").strip():
                af["host_param"] = hpar.strip()
            result["autofill"] = af
            result["remediation"] = remediation
            st.caption(
                "🔗 Khi trống, **kế thừa từ host** "
                f"(`host.{(hpar or '').strip() or result.get('parameter') or '—'}`). "
                "Host không có giá trị → Path A."
            )
        else:  # set_fixed
            result["autofill"] = {"strategy": "none"}
            result["remediation"] = {
                **remediation, "new_value_strategy": "fixed", "new_value": fixed_val,
            }
        result["fixability"] = "auto"
        st.caption(
            "⚠️ Tự sửa bị **gate theo Độ nghiêm trọng** (Med/High → duyệt qua ACC "
            "proposal issue trước khi ghi). Chỉ chạy ở chế độ **Full run — Revit**."
        )
    else:
        result["autofill"] = {"strategy": "none"}
        result["remediation"] = {"action": "create_acc_issue"}
        result["fixability"] = "manual"

    return result


def _append_rule_history(description: str, draft: dict) -> None:
    """Append one entry to the Rule Builder JSONL history log."""
    import json as _json
    RULE_BUILDER_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "description": description,
        "draft": draft,
    }
    with RULE_BUILDER_HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")


def _load_rule_history(n: int = 10) -> list[dict]:
    """Return the last *n* Rule Builder history entries, newest first."""
    import json as _json
    if not RULE_BUILDER_HISTORY_PATH.exists():
        return []
    lines = RULE_BUILDER_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(_json.loads(line))
        except Exception:
            continue
        if len(entries) >= n:
            break
    return entries


def _rule_action_summary(r: dict) -> str:
    """One-line "what happens on violation" for a saved rule dict (for the list)."""
    rem = r.get("remediation") or {}
    af = r.get("autofill") or {}
    action = rem.get("action")
    tgt = rem.get("target", "?")
    # v1.4-K19: "auto" resolves the target at run time — render it readably.
    tgt_label = "tự phát hiện" if tgt == "auto" else tgt
    if action == "rename_element":
        what = "Family" if tgt == "family" else "Type"
        return f"🔧 đổi tên {what} ({af.get('normalize_kind', '?')})"
    if action == "set_parameter":
        strat = af.get("strategy")
        if strat == "normalize":
            return f"🔧 normalize · {af.get('normalize_kind', '?')} → {tgt_label}"
        if strat == "compose_template":
            return f"🔧 compose → {tgt_label}"
        if rem.get("new_value_strategy") == "fixed":
            return f"🔧 giá trị cố định → {tgt_label}"
        return f"🔧 set_parameter → {tgt_label}"
    return "📋 ACC Issue (Path A)"


def _render_created_rules_section() -> None:
    """Optional inbox of rules already saved to config/ — view + load-to-edit +
    (guarded) delete. Lets the user CHECK what they've built without leaving the
    Rule Builder. No nested expanders (Streamlit forbids them) — a file picker."""
    import yaml as _yaml

    files = sorted(CONFIG_DIR.glob("rules.*.yaml"))
    with st.expander(f"📚 Rule đã tạo ({len(files)} file trong config/)", expanded=False):
        if not files:
            st.caption("Chưa có file rule nào. Tạo rule ở trên rồi **💾 Lưu**.")
            return
        sel = st.selectbox(
            "Chọn file để xem", [f.name for f in files], key="rb_view_file",
        )
        fp = CONFIG_DIR / str(sel)
        try:
            data = _yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception as exc:                       # noqa: BLE001
            st.warning(f"⚠️ Không đọc được: {exc}")
            return
        rules = data.get("rules") or []
        geos = data.get("geometry_rules") or []
        st.caption(f"`{sel}` — {len(rules)} param rule · {len(geos)} geometry rule")
        if rules:
            st.table([
                {
                    "id": r.get("id"),
                    "category": r.get("category"),
                    "parameter": r.get("parameter"),
                    "yêu cầu": REQUIREMENT_LABELS_ALL.get(r.get("requirement"), r.get("requirement")),
                    "hành động": _rule_action_summary(r),
                }
                for r in rules
            ])
        c1, c2, c3 = st.columns([2, 2, 3])
        with c1:
            if rules and st.button("✏️ Nạp rule đầu vào form", key=f"rb_load_{sel}"):
                st.session_state["rb_draft"] = rules[0]
                st.session_state["rb_gen_v"] = st.session_state.get("rb_gen_v", 0) + 1
                st.rerun()
        with c2:
            show_yaml = st.toggle("👁 Xem YAML gốc", key=f"rb_yaml_{sel}")
        with c3:
            confirm = st.checkbox("Xác nhận xoá", key=f"rb_delc_{sel}")
            if st.button("🗑 Xoá file", key=f"rb_del_{sel}", disabled=not confirm):
                try:
                    fp.unlink()
                except OSError as exc:                  # noqa: BLE001
                    st.error(f"Xoá thất bại: {exc}")
                else:
                    # H4: do NOT assign st.session_state["rb_view_file"] here — it is
                    # the key of the selectbox already instantiated this run, so
                    # setting it raises StreamlitAPIException (crash on the flagship
                    # tab). Just rerun; the selectbox rebuilds from the remaining
                    # files and drops the stale selection cleanly.
                    st.rerun()
        if show_yaml:
            st.code(fp.read_text(encoding="utf-8"), language="yaml")


def _guard_imported_ruleset(data: dict) -> tuple[dict, list[str]]:
    """A-01: run the save-time guard chain over a ruleset authored ELSEWHERE.

    Returns ``(guarded_ruleset, errors)``. A non-empty ``errors`` means the
    caller must NOT write — every message names the offending rule id.

    The IDS-import tab used to dump its converted ruleset straight into
    ``config/`` behind nothing but Pydantic's shape check: no
    ``enforce_unique_autofix``, no ``enforce_reference_membership``, no
    ``validate_rule``. That tab explicitly invites files authored in other
    tools ("import from Solibri, ..."), which makes it the least trusted
    input in the product — and it was the only authoring surface with no
    save-time guards. An external IDS naming a read-only built-in as a
    ``set_parameter`` target landed on disk under a green "✅ Đã lưu". The
    runtime net still refused the write (it degrades to Path A), so no model
    was ever harmed; the defect is that the tool was WRONG ABOUT WHAT IT HAD
    JUST SAVED, which is the part that matters for a product whose whole
    claim is auditability.

    Same guards, same order, as ``PUT /rules/{name}`` and
    ``_save_rule_to_yaml``. A third authoring surface must not mean a third
    policy — re-typing the chain per call site is exactly how this one
    drifted, so it lives here once.
    """
    guarded = dict(data)
    categories = data.get("target_category")
    rules: list[dict] = []
    errors: list[str] = []
    for idx, raw in enumerate(data.get("rules") or []):
        rule = _enforce_reference_membership(_enforce_unique_autofix(raw))
        rules.append(rule)
        rule_id = rule.get("id") or f"rules[{idx}]"
        for issue in _validate_rule(
            rule, is_geometry=False, ruleset_categories=categories
        ).errors:
            errors.append(f"`{rule_id}` · {issue.field}: {issue.message}")
    guarded["rules"] = rules
    return guarded, errors


def _save_rule_to_yaml(rule: dict, scenario_name: str) -> tuple[bool, str]:
    """Validate + save a single-rule RuleSet to config/rules.<scenario>.yaml.

    Returns ``(True, relative_path)`` on success, ``(False, error_msg)`` on failure.
    """
    jty = _load_json_to_yaml_module()
    rule = _enforce_unique_autofix(rule)  # QA F2: unique_in_set ⇒ auto, always
    rule = _enforce_reference_membership(rule)  # QA G7: reference-set value-validity

    full_rule: dict = {
        "id": (rule.get("id") or f"{scenario_name}.rule").strip(),
        "category": rule.get("category", ""),
        "parameter": rule.get("parameter", ""),
        "requirement": rule.get("requirement", "present_and_nonempty"),
        "severity_tag": rule.get("severity_tag") or "rule_violation",
        "description": rule.get("description", ""),
        "fixability": rule.get("fixability", "manual"),
        # v1.4-K10 / Proposal A: honour the editor's chosen Path-B remediation +
        # autofill instead of hard-coding Path A. Default to Path A when absent.
        "autofill": rule.get("autofill") or {"strategy": "none"},
        "remediation": rule.get("remediation") or {"action": "create_acc_issue"},
        "extraction_meta": {
            "confidence": 1.0,
            "source_text": rule.get("description", ""),
            "source_location": "Rule Builder (UI)",
            "execution_status": "executable",
        },
    }
    if rule.get("threshold") is not None:
        full_rule["threshold"] = float(rule["threshold"])
    # v1.4-K10 pass-through: only set when present so exclude_none keeps YAML lean.
    for k in ("unit", "pattern", "when_param", "when_pattern", "operator",
              "compare_kind", "other_param", "severity_level", "lookup"):
        if rule.get(k):
            full_rule[k] = rule[k]
    if rule.get("scope_filter"):
        full_rule["scope_filter"] = rule["scope_filter"]

    envelope: dict = {
        "scenario": scenario_name,
        "target_category": rule.get("category", ""),
        "rules": [full_rule],
    }

    try:
        executable, _review, invalid, warnings = jty._split_by_status(envelope)
        if invalid:
            err = invalid[0].get("error", "Unknown schema error")
            return False, f"❌ Lỗi schema: {err}"
        catalog = jty.OSTCatalog.load()
        ruleset = jty._build_ruleset(envelope, executable, catalog)
        yaml_text = jty._ruleset_to_yaml(ruleset)
        out_path = REPO_ROOT / "config" / f"rules.{scenario_name}.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_text, encoding="utf-8")
        return True, str(out_path.relative_to(REPO_ROOT))
    except Exception as exc:
        return False, f"❌ {exc}"


def _yaml_has_geometry_rules(rules_path: str) -> bool:
    """Return True when the YAML at *rules_path* has a non-empty geometry_rules list."""
    if not rules_path:
        return False
    try:
        import yaml as _yaml
        data = _yaml.safe_load(Path(rules_path).read_text(encoding="utf-8")) or {}
        return bool(data.get("geometry_rules"))
    except Exception:
        return False


def _save_geometry_rule_to_yaml(grule: dict, scenario_name: str) -> tuple[bool, str]:
    """Build a RuleSet with geometry_rules only and write to config/rules.<name>.yaml."""
    import yaml as _yaml
    from bim_orchestrator.policies.rules_schema import GeometryRule, RuleSet

    try:
        geo = GeometryRule.model_validate(grule)
        ruleset = RuleSet(
            scenario=scenario_name,
            target_category=grule.get("category", ""),
            rules=[],
            geometry_rules=[geo],
        )
        data = ruleset.model_dump(exclude_none=True)
        yaml_text = _yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        out_path = CONFIG_DIR / f"rules.{scenario_name}.yaml"
        out_path.write_text(yaml_text, encoding="utf-8")
        return True, str(out_path.relative_to(REPO_ROOT))
    except Exception as exc:
        return False, f"❌ {exc}"


def _render_geometry_rule_form() -> None:
    """Structured capture form for geometry-based rules (v2 roadmap, not yet executable)."""
    st.info(
        "📐 **Geometry Check** — clearances, spatial containment, linked-file queries.\n\n"
        "Rule được lưu với `execution_status: not_model_checkable`. "
        "Sẽ chạy trong **v2 geometric evaluator** (Tier 3 roadmap).",
        icon=None,
    )

    categories = _get_catalog_categories()

    # ── Nhận dạng ─────────────────────────────────────────────────────────
    st.markdown("**🏷️ Nhận dạng**")
    c1, c2 = st.columns([2, 3])
    with c1:
        geo_id = st.text_input(
            "ID rule",
            key="geo_id",
            placeholder="ducts.parking.floor_clearance",
            help="Dạng lowercase, chấm phân cách: category.context.check",
        )
    with c2:
        geo_desc = st.text_area(
            "Mô tả",
            key="geo_desc",
            height=68,
            placeholder="Duct clearance to nearest floor slab below >= 2400mm in Parking MEP Space",
        )

    # ── Loại kiểm tra ─────────────────────────────────────────────────────
    st.markdown("**⚙️ Loại kiểm tra**")
    _CHECK_TYPE_LABELS = {
        "clearance_min":       "Khoảng cách tối thiểu (clearance_min)",
        "clearance_max":       "Khoảng cách tối đa (clearance_max)",
        "spatial_containment": "Element phải nằm trong không gian (spatial_containment)",
        "min_spacing":         "Khoảng cách giữa các phần tử cùng loại (min_spacing)",
    }
    c3, c4, c5 = st.columns([3, 2, 2])
    with c3:
        check_type_label = st.selectbox(
            "Loại", list(_CHECK_TYPE_LABELS.values()), key="geo_check_type",
        )
    check_type = next(k for k, v in _CHECK_TYPE_LABELS.items() if v == check_type_label)
    needs_threshold = check_type in ("clearance_min", "clearance_max", "min_spacing")
    needs_direction = check_type in ("clearance_min", "clearance_max")
    with c4:
        if needs_threshold:
            geo_threshold = st.number_input(
                "Ngưỡng (mm)", min_value=0.0, value=2400.0, step=50.0, key="geo_threshold",
            )
        else:
            st.markdown("&nbsp;")
            geo_threshold = None
    with c5:
        if needs_direction:
            # clearance_max cần khoảng cách ĐO ĐƯỢC để so "actual > threshold";
            # chỉ raycast dọc (below/above) trả clearanceActualMm — bbox
            # (horizontal) chỉ báo "có cặp chạm", engine sẽ fail-closed
            # (geometry_query._run_max_rule). Đừng cho author một rule mà
            # engine từ chối chạy.
            _dir_options = (
                ["below", "above"]
                if check_type == "clearance_max"
                else ["below", "above", "horizontal"]
            )
            geo_direction = st.selectbox(
                "Hướng", _dir_options, key="geo_direction",
            )
        else:
            st.markdown("&nbsp;")
            geo_direction = None

    # ── Category cần kiểm tra ─────────────────────────────────────────────
    st.markdown("**🏗️ Category cần kiểm tra**")
    geo_category = st.selectbox(
        "Category", categories, key="geo_category",
        help="Revit category của elements cần check (ví dụ: Ducts, Walls, Pipes)",
    )

    # ── Phần tử tham chiếu ────────────────────────────────────────────────
    geo_ref_cat: str | None = None
    geo_ref_source = "same_model"
    geo_link_hint = ""
    if check_type in ("clearance_min", "clearance_max", "min_spacing"):
        st.markdown("**📐 Phần tử tham chiếu**")
        _REF_SOURCE_LABELS = {
            "same_model":    "Cùng model (same_model)",
            "linked_arch":   "Link kiến trúc (linked_arch)",
            "linked_struct": "Link kết cấu (linked_struct)",
            "linked_mep":    "Link MEP (linked_mep)",
        }
        c6, c7 = st.columns(2)
        with c6:
            geo_ref_cat = st.selectbox(
                "Category tham chiếu", categories, key="geo_ref_cat",
                help="Ví dụ: Floors (sàn), Ceilings (trần), Structural Columns",
            )
        with c7:
            ref_source_label = st.selectbox(
                "Nguồn file", list(_REF_SOURCE_LABELS.values()), key="geo_ref_source",
            )
            geo_ref_source = next(k for k, v in _REF_SOURCE_LABELS.items() if v == ref_source_label)
        # Link disambiguation: MEP models are named by discipline (HVAC /
        # Plumbing / Electrical), not "MEP", so the linked_mep keyword resolves
        # nothing on its own when several MEP links are loaded. Let the author
        # name the exact link by a substring of its filename.
        if geo_ref_source != "same_model":
            geo_link_hint = st.text_input(
                "Tên link cụ thể (tuỳ chọn)", key="geo_link_hint",
                placeholder="HVAC",
                help=(
                    "Chuỗi con khớp tên file link (không phân biệt hoa/thường), "
                    "ưu tiên hơn keyword theo discipline. Cần khi model có nhiều "
                    "link cùng loại — ví dụ 'HVAC' để chọn '...Sample HVAC.rvt' "
                    "thay vì '...Plumbing.rvt'. Bỏ trống để tự dò theo discipline."
                ),
            )

    # ── Spatial filter (optional) ──────────────────────────────────────────
    with st.expander("🔍 Spatial filter — Giới hạn vùng kiểm tra (tuỳ chọn)", expanded=False):
        sf_cat = st.selectbox(
            "Container category", ["(không giới hạn)"] + categories, key="geo_sf_cat",
        )
        sf_name = st.text_input(
            "Tên container chứa chuỗi", key="geo_sf_name",
            placeholder="Parking",
            help="Chỉ kiểm tra elements nằm trong container có tên chứa chuỗi này",
        )
    has_sf = (sf_cat != "(không giới hạn)") or bool(sf_name.strip())

    # ── Validation ────────────────────────────────────────────────────────
    geo_issues: list[str] = []
    if not geo_id.strip():
        geo_issues.append("ID không được để trống")
    if not geo_desc.strip():
        geo_issues.append("Mô tả không được để trống")
    if needs_threshold and (geo_threshold is None or geo_threshold <= 0):
        geo_issues.append("Ngưỡng phải > 0")
    for iss in geo_issues:
        st.warning(f"⚠️ {iss}")

    # ── Step 3: Save ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 3️⃣ Lưu thành YAML")
    default_slug = _slugify(geo_id.strip()) if geo_id.strip() else "geo_rule"
    c_name, c_save = st.columns([3, 2])
    with c_name:
        geo_scenario = st.text_input(
            "Tên scenario", value=default_slug, key="geo_scenario",
            help="Sẽ lưu vào config/rules.<tên>.yaml",
        )
    geo_slug = _slugify(str(geo_scenario).strip()) if geo_scenario else "geo_rule"
    st.caption(f"📄 Sẽ lưu vào: `config/rules.{geo_slug}.yaml`")
    with c_save:
        st.write("")
        save_geo = st.button(
            "💾 Lưu rule (geometry)",
            type="primary",
            disabled=bool(geo_issues) or not str(geo_scenario).strip(),
            use_container_width=True,
        )

    if save_geo and not geo_issues:
        grule: dict = {
            "id": geo_id.strip(),
            "category": geo_category,
            "check_type": check_type,
            "description": geo_desc.strip(),
            "severity_tag": "geometric_violation",
            "execution_status": "not_model_checkable",
        }
        if geo_threshold is not None:
            grule["threshold_mm"] = float(geo_threshold)
        if geo_direction:
            grule["clearance_direction"] = geo_direction
        if geo_ref_cat:
            grule["reference_category"] = geo_ref_cat
        if geo_ref_source != "same_model":
            grule["reference_source"] = geo_ref_source
            if geo_link_hint.strip():
                grule["reference_link_hint"] = geo_link_hint.strip()
        if has_sf:
            sf: dict = {}
            if sf_cat != "(không giới hạn)":
                sf["category"] = sf_cat
            if sf_name.strip():
                sf["name_contains"] = sf_name.strip()
            if sf:
                grule["spatial_filter"] = sf
        ok, msg = _save_geometry_rule_to_yaml(grule, geo_slug)
        if ok:
            st.success(f"✅ Đã tạo `{msg}`")
            st.info(
                "📌 Rule này **không chạy** trong QC hiện tại — "
                "sẽ được hỗ trợ trong **v2 geometric evaluator** (Tier 3 roadmap)."
            )
        else:
            st.error(msg)


def _render_ids_import_section() -> None:
    """IDS import expander inside Rule Builder — alternative to NL text input."""
    with st.expander("📥 Import từ IDS (buildingSMART)", expanded=False):
        uploaded = st.file_uploader(
            "Upload file .ids",
            type=["ids", "xml"],
            key="rb_ids_upload",
            help="Chấp nhận IDS 1.0 từ bất kỳ công cụ nào (Solibri, bim-orchestrator, …)",
        )
        if uploaded is None:
            st.caption("Kéo thả file .ids vào đây để import toàn bộ ruleset, bỏ qua bước nhập text.")
            return

        try:
            from bim_orchestrator.policies.ids_converter import ids_xml_to_ruleset
            xml_text = uploaded.read().decode("utf-8")
            ruleset, ids_warns = ids_xml_to_ruleset(xml_text)
        except Exception as exc:
            st.error(f"Không thể đọc file IDS: {exc}")
            return

        n = len(ruleset.rules)
        cat = (
            ruleset.target_category
            if isinstance(ruleset.target_category, str)
            else ", ".join(ruleset.target_category)
        )
        st.info(
            f"Đọc được **{n} rule{'s' if n != 1 else ''}** "
            f"— scenario: `{ruleset.scenario}` | category: `{cat}`"
        )
        if ids_warns:
            with st.expander(f"⚠️ {len(ids_warns)} cảnh báo chuyển đổi", expanded=False):
                for w in ids_warns:
                    st.caption(w)

        ids_scenario = st.text_input(
            "Tên scenario",
            value=_slugify(ruleset.scenario),
            key="rb_ids_scenario",
            help="Sẽ lưu vào config/rules.<tên>.yaml",
        )
        ids_slug = _slugify((ids_scenario or "").strip()) or "imported"
        st.caption(f"📄 Sẽ lưu vào: `config/rules.{ids_slug}.yaml`")

        if st.button(
            f"💾 Lưu {n} rules từ IDS",
            type="primary",
            disabled=not (ids_scenario or "").strip(),
            key="rb_ids_save",
        ):
            import yaml as _yaml
            guarded, errors = _guard_imported_ruleset(
                ruleset.model_dump(exclude_none=True)
            )
            if errors:
                # A-01: refuse the write entirely. Saving the good rules and
                # dropping the rest would be the same lie in a smaller font —
                # the operator fixes the source IDS (or the converter) and
                # re-imports, which is why every error is listed.
                st.error(
                    f"❌ Không lưu — {len(errors)} lỗi không qua được kiểm tra "
                    "lưu (cùng bộ guard mà Rule Builder và API dùng):"
                )
                for _e in errors[:20]:
                    st.caption(f"• {_e}")
                if len(errors) > 20:
                    st.caption(f"… và {len(errors) - 20} lỗi nữa.")
            else:
                yaml_text = _yaml.dump(guarded, allow_unicode=True, sort_keys=False,
                                       default_flow_style=False)
                out_path = CONFIG_DIR / f"rules.{ids_slug}.yaml"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(yaml_text, encoding="utf-8")
                st.success(f"✅ Đã lưu {n} rules vào `config/rules.{ids_slug}.yaml`")
                st.info("👉 Mở tab **Setup**, chọn file vừa tạo rồi chạy **▶️ Run**.")


def _build_ids_xml_for_rule(rule: dict, scenario: str) -> str | None:
    """Build IDS XML for the currently edited single-rule dict.

    Returns None silently on any error (caller should disable the button).
    """
    try:
        from bim_orchestrator.agents.qc import Rule as _Rule, RuleSet as _RuleSet
        from bim_orchestrator.policies.ids_converter import ruleset_to_ids_xml

        optional: dict = {}
        for key in ("threshold", "unit", "pattern", "when_param", "when_pattern"):
            v = rule.get(key)
            if v is not None and v != "":
                optional[key] = v
        if optional.get("threshold") is not None:
            optional["threshold"] = float(optional["threshold"])

        r = _Rule.model_validate({
            "id": rule.get("id") or "rule.exported",
            "parameter": rule.get("parameter") or "",
            "requirement": rule.get("requirement") or "present_and_nonempty",
            "severity_tag": rule.get("severity_tag") or "missing_required_param",
            "description": rule.get("description") or "",
            "fixability": rule.get("fixability") or "manual",
            "autofill": {"strategy": "none"},
            "remediation": {"action": "create_acc_issue"},
            **optional,
        })
        rs = _RuleSet(
            scenario=scenario,
            target_category=rule.get("category") or "Rooms",
            rules=[r],
        )
        xml, _ = ruleset_to_ids_xml(rs)
        return xml
    except Exception:
        return None


def render_rule_builder_tab() -> None:
    """v1.4: NL → Claude API (grounded by param_catalog) → editable Rule form → YAML.

    Catalog-wired (the experimental v2 tab was folded in here): for a catalogued
    category the parameter is a built-in DROPDOWN (read-only refused as a write
    target, write-target + unit pre-filled), and extraction is grounded with the
    real categories + params + intent→param aliases.
    """
    st.subheader("📋 Rule Builder")
    st.caption(
        "Mô tả yêu cầu kiểm tra bằng ngôn ngữ tự nhiên — AI tự tạo rule draft "
        "(grounded theo **param_catalog**: thông số là dropdown built-in theo "
        "category). Kiểm tra, chỉnh sửa, rồi lưu thành YAML để chạy QC."
    )

    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))

    # ── Step 0: Check type ────────────────────────────────────────────────
    st.markdown("#### 0️⃣ Loại kiểm tra")
    _CHECK_TYPE_OPTIONS = {
        "parameter": "📊 Parameter Check — data completeness, naming, thresholds",
        "geometry":  "📐 Geometry Check — clearances, spatial, linked files",
    }
    check_type_label = st.radio(
        "Loại kiểm tra",
        options=list(_CHECK_TYPE_OPTIONS.values()),
        horizontal=True,
        key="rb_check_type",
        label_visibility="collapsed",
        help=(
            "**Parameter Check**: kiểm tra giá trị Revit parameter — "
            "completeness, naming, numeric threshold, uniqueness. "
            "Hỗ trợ đầy đủ bởi QCAgent hiện tại.\n\n"
            "**Geometry Check**: kiểm tra khoảng cách, containment, linked file. "
            "Lưu lại cho v2 geometric evaluator — **không chạy trong lần này**."
        ),
    )
    rb_check_type = "geometry" if "Geometry" in check_type_label else "parameter"

    if rb_check_type == "geometry":
        st.divider()
        _render_geometry_rule_form()
        return

    st.divider()

    # ── Step 1: NL input or IDS import ────────────────────────────────────
    st.markdown("#### 1️⃣ Mô tả yêu cầu kiểm tra")
    _render_ids_import_section()
    nl_input = st.text_area(
        "Hoặc nhập bằng tiếng Việt / tiếng Anh",
        key="rb_nl_input",
        height=85,
        placeholder=(
            "Ví dụ: Kiểm tra tất cả cột/dầm trong dự án đã có Mark value chưa. "
            "Yêu cầu không trùng nhau."
        ),
    )

    c_btn, c_warn = st.columns([2, 5])
    with c_btn:
        generate_clicked = st.button(
            "🤖 Tạo rule",
            type="primary",
            disabled=not has_api_key,
            use_container_width=True,
        )
    with c_warn:
        if not has_api_key:
            st.warning("Chưa có ANTHROPIC_API_KEY trong .env")

    if generate_clicked and nl_input.strip():
        with st.spinner("Claude đang đọc yêu cầu (grounded theo param_catalog)…"):
            draft = _call_claude_for_rule(nl_input.strip(), catalog_grounded=True)
        if draft is not None:
            st.session_state["rb_draft"] = draft
            # Increment version so form widgets re-initialise from the new draft
            st.session_state["rb_gen_v"] = st.session_state.get("rb_gen_v", 0) + 1
            _append_rule_history(nl_input.strip(), draft)
            st.rerun()

    # Always-visible: review the rules already saved (even before any draft).
    _render_created_rules_section()

    if "rb_draft" not in st.session_state:
        return

    # ── Step 2: Editable form ─────────────────────────────────────────────
    st.divider()
    st.markdown("#### 2️⃣ Kiểm tra và chỉnh sửa")

    gen_v = st.session_state.get("rb_gen_v", 0)
    edited = _render_rule_form(st.session_state["rb_draft"], gen_v, catalog_mode=True)

    # Live validation — delegates to rule_builder_core.validate_rule (B16) so the
    # Streamlit form and POST /api/builder/validate share ONE source of truth.
    # Message text/order preserved verbatim (golden S1 + existing test suite).
    _validation = _validate_rule(edited, is_geometry=False)
    issues: list[str] = [e.message for e in _validation.errors]

    if issues:
        for iss in issues:
            st.warning(f"⚠️ {iss}")

    # ── Step 3: Save ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 3️⃣ Lưu thành YAML")

    # Use the rule id as the default scenario name, versioned so it refreshes
    # whenever a new rule is generated (same key-versioning trick as the form fields).
    default_scenario = _slugify(str(edited.get("id", "new_rule")))
    c1, c2, c3 = st.columns([3, 1.5, 1.5])
    with c1:
        scenario_name = st.text_input(
            "Tên scenario (dùng làm tên file)",
            value=default_scenario,
            key=f"rb_{gen_v}_scenario_name",
            help="Sẽ lưu vào config/rules.<tên>.yaml",
        )
    scenario_slug = _slugify(str(scenario_name).strip()) if scenario_name else "new_rule"
    with c2:
        st.write("")  # vertical spacer
        save_clicked = st.button(
            "💾 Lưu rule",
            type="primary",
            disabled=bool(issues) or not str(scenario_name).strip(),
            use_container_width=True,
        )
    with c3:
        st.write("")  # vertical spacer
        _ids_xml = _build_ids_xml_for_rule(edited, scenario_slug)
        st.download_button(
            "⬇ Export IDS",
            data=_ids_xml or "",
            file_name=f"rules.{scenario_slug}.ids",
            mime="application/xml",
            disabled=bool(issues) or not _ids_xml,
            use_container_width=True,
        )

    st.caption(f"📄 Sẽ lưu vào: `config/rules.{scenario_slug}.yaml`")

    # C1: an inline-defined reference table is written ONLY on Save (not during
    # render), and never silently over an existing table with different content.
    _pending_ref = edited.get("_pending_reference")
    _ref_overwrite_ok = True
    if _pending_ref and _reference_needs_overwrite(_pending_ref["name"], _pending_ref["entries"]):
        _ref_overwrite_ok = st.checkbox(
            f"⚠️ Bảng tham chiếu `reference.{_pending_ref['name']}.yaml` đã tồn tại "
            "với nội dung KHÁC — tích để **ghi đè** (mọi rule đang cite bảng này sẽ "
            "dùng nội dung mới).",
            key=f"rb_{gen_v}_refoverwrite",
            value=False,
        )

    if save_clicked and not issues and scenario_name:
        if _pending_ref and not _ref_overwrite_ok:
            st.error(
                f"⛔ Chưa lưu: bảng `reference.{_pending_ref['name']}.yaml` đã tồn tại "
                "và khác nội dung. Tích ô ghi đè ở trên rồi bấm Lưu lại, hoặc đổi tên bảng."
            )
            return
        if _pending_ref:
            _write_reference_file(_pending_ref["name"], _pending_ref["entries"])
        edited.pop("_pending_reference", None)
        success, msg = _save_rule_to_yaml(edited, _slugify(scenario_name.strip()))
        if success:
            st.success(f"✅ Đã tạo `{msg}`")
            # v1.4-K17: auto-select the just-saved rule in Setup so it's the
            # active ruleset for the next run — no hunting in the multiselect.
            # rules_paths holds ABSOLUTE paths (Setup converts back to rel).
            _abs = str(REPO_ROOT / msg)
            st.session_state["rules_paths"] = [_abs]
            st.session_state["rules_path"] = _abs
            st.info(
                "👉 Rule này đã được **chọn sẵn ở tab Setup** — qua **▶️ Run** để kiểm tra."
            )
        else:
            st.error(msg)

    # ── Recent drafts ──────────────────────────────────────────────────────
    history = _load_rule_history(10)
    if history:
        st.divider()
        with st.expander("📂 Lịch sử tạo rule (10 gần nhất)", expanded=False):
            for i, entry in enumerate(history):
                ts = entry.get("ts", "?")
                desc = entry.get("description", "")
                preview = desc[:80] + ("…" if len(desc) > 80 else "")
                col_info, col_btn = st.columns([6, 1])
                with col_info:
                    st.markdown(f"**{ts}** — {preview}")
                with col_btn:
                    if st.button("Restore", key=f"rb_hist_restore_{i}"):
                        st.session_state["rb_draft"] = entry["draft"]
                        st.session_state["rb_gen_v"] = st.session_state.get("rb_gen_v", 0) + 1
                        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# v1.4 D0.4b: Extraction Review tab — JSON-in, JSON-cleaned + YAML out
# ──────────────────────────────────────────────────────────────────────────


def _load_json_to_yaml_module():
    """Import the extraction-skills/scripts/json_to_yaml.py module by path.

    The script lives outside the streamlit_app package so we can't use a
    normal import. ``importlib.util`` lets us load it once and reuse its
    validation + conversion helpers (avoids duplicating logic).
    """
    import importlib.util
    script_path = REPO_ROOT / "extraction-skills" / "scripts" / "json_to_yaml.py"
    spec = importlib.util.spec_from_file_location("_jty", str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load json_to_yaml.py at {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_extraction_payload(payload_text: str) -> dict:
    """Run the full validation pipeline on raw JSON text. Returns a dict:

    {
      "json_error": str | None,
      "rulesets": [
        {"scenario": ..., "executable": int, "review": int, "invalid": int,
         "warnings": [str, ...], "executable_rules": [Rule, ...]},
        ...
      ],
      "total_warnings": int,
      "total_invalid": int,
    }

    All validation runs in-memory — no files written. Used for the live
    "Re-validate" button so the user sees immediate feedback while editing.
    """
    jty = _load_json_to_yaml_module()
    out: dict = {
        "json_error": None,
        "rulesets": [],
        "total_warnings": 0,
        "total_invalid": 0,
        "total_executable": 0,
        "total_review": 0,
    }
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        out["json_error"] = f"Line {exc.lineno}, col {exc.colno}: {exc.msg}"
        return out
    rulesets_raw = jty._envelope_to_rulesets(payload)
    for rs_raw in rulesets_raw:
        scenario = rs_raw.get("scenario") or "extracted_unnamed"
        executable, review, invalid, warnings = jty._split_by_status(rs_raw)
        out["rulesets"].append({
            "scenario": scenario,
            "executable": len(executable),
            "review": len(review),
            "invalid": len(invalid),
            "warnings": warnings,
            "invalid_details": invalid,
            "review_details": review,
        })
        out["total_warnings"] += len(warnings)
        out["total_invalid"] += len(invalid)
        out["total_executable"] += len(executable)
        out["total_review"] += len(review)
    return out


def _save_extraction_payload(payload_text: str) -> dict:
    """Write the cleaned JSON + generated YAML(s) to the repo. Returns:

    {"cleaned_paths": [...], "yaml_paths": [...], "errors": [str, ...]}

    The cleaned JSON goes under ``extraction-skills/cleaned/`` so the
    original Claude output isn't overwritten. The YAML lands in
    ``config/rules.<scenario>.yaml`` ready for ``bim-orchestrator --check``.
    """
    jty = _load_json_to_yaml_module()
    out: dict = {"cleaned_paths": [], "yaml_paths": [], "errors": []}
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        out["errors"].append(f"JSON parse error: {exc}")
        return out

    cleaned_dir = REPO_ROOT / "extraction-skills" / "cleaned"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rulesets_raw = jty._envelope_to_rulesets(payload)
    catalog = jty.OSTCatalog.load()

    for rs_raw in rulesets_raw:
        scenario = rs_raw.get("scenario") or f"extracted_{ts}"
        # Cleaned JSON — exactly what the user has in the editor for THIS
        # ruleset (so multi-ruleset envelopes split into separate files).
        cleaned_path = cleaned_dir / f"{scenario}.cleaned.json"
        cleaned_path.write_text(json.dumps(rs_raw, indent=2, ensure_ascii=False), encoding="utf-8")
        out["cleaned_paths"].append(str(cleaned_path.relative_to(REPO_ROOT)))

        # YAML — only executable rules, same as CLI.
        executable, _review, invalid, _warnings = jty._split_by_status(rs_raw)
        if invalid:
            for inv in invalid:
                out["errors"].append(
                    f"[{scenario}] schema-invalid rule {inv['raw'].get('id', '?')}: {inv['error']}"
                )
            continue
        ruleset = jty._build_ruleset(rs_raw, executable, catalog)
        yaml_path = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(jty._ruleset_to_yaml(ruleset), encoding="utf-8")
        out["yaml_paths"].append(str(yaml_path.relative_to(REPO_ROOT)))
    return out


def render_extraction_review_tab() -> None:
    """JSON-in editor with live validation + dual save (cleaned JSON + YAML)."""
    st.subheader("Extraction Review")
    st.caption(
        "Paste the JSON Claude Desktop produced from your BEP / standard. "
        "Fix any heuristic warnings, then save → writes both the cleaned "
        "JSON (for audit / re-edit) and the runtime YAML (`config/rules."
        "<scenario>.yaml`)."
    )

    # ── Input area ──────────────────────────────────────────────────────
    col_upload, col_paste_hint = st.columns([1, 2])
    with col_upload:
        uploaded = st.file_uploader(
            "Upload JSON", type=["json"], key="extraction_upload",
            help="Or paste below.",
        )
    with col_paste_hint:
        st.caption(
            "Default workflow: Claude Desktop → copy JSON → paste into the "
            "editor below → Re-validate → fix warnings → Save."
        )

    # Seed the editor from upload exactly once per file name.
    if uploaded is not None:
        if st.session_state.get("_last_uploaded_name") != uploaded.name:
            st.session_state["extraction_json"] = uploaded.read().decode("utf-8")
            st.session_state["_last_uploaded_name"] = uploaded.name
            st.session_state["_extraction_validation"] = None  # force re-run

    payload_text = st.session_state.get("extraction_json", "")

    # ── Live validation (cached in session_state, refreshed on button) ──
    validation = st.session_state.get("_extraction_validation")
    if validation is None and payload_text.strip():
        try:
            validation = _validate_extraction_payload(payload_text)
            st.session_state["_extraction_validation"] = validation
        except Exception as exc:
            st.error(f"Validator crashed: {exc}")
            validation = None

    # ── Summary at top ──────────────────────────────────────────────────
    if validation:
        cols = st.columns(4)
        cols[0].metric("Executable", validation["total_executable"])
        cols[1].metric("In review", validation["total_review"])
        cols[2].metric(
            "Warnings", validation["total_warnings"],
            delta=None if validation["total_warnings"] == 0 else "fix before save",
            delta_color="inverse",
        )
        cols[3].metric(
            "Invalid", validation["total_invalid"],
            delta=None if validation["total_invalid"] == 0 else "schema reject",
            delta_color="inverse",
        )

        if validation["json_error"]:
            st.error(f"JSON parse error: {validation['json_error']}")
        else:
            for rs in validation["rulesets"]:
                with st.expander(
                    f"Scenario: `{rs['scenario']}` — "
                    f"{rs['executable']} executable, "
                    f"{rs['review']} review, "
                    f"{rs['invalid']} invalid, "
                    f"{len(rs['warnings'])} warnings",
                    expanded=bool(rs["warnings"] or rs["invalid"]),
                ):
                    if rs["warnings"]:
                        st.markdown("**Heuristic warnings** (advisory unless --strict):")
                        for w in rs["warnings"]:
                            st.warning(w)
                    if rs["invalid_details"]:
                        st.markdown("**Schema-invalid rules** (will not be saved):")
                        for inv in rs["invalid_details"]:
                            rid = inv["raw"].get("id", "no-id")
                            st.error(f"`{rid}`\n\n```\n{inv['error']}\n```")
                    if rs["review_details"]:
                        st.markdown(
                            "**Review items** (execution_status != executable — "
                            "won't make it into YAML):"
                        )
                        for item in rs["review_details"]:
                            rid = item["raw"].get("id", "no-id")
                            reason = item.get("reason", "?")
                            details = item.get("details", {})
                            st.info(
                                f"`{rid}` — {reason}: "
                                f"{details.get('status_reason', '(no reason given)')}"
                            )

    # ── JSON editor ────────────────────────────────────────────────────
    st.markdown("### JSON editor")
    new_text = st.text_area(
        "JSON content",
        value=payload_text,
        height=400,
        key="extraction_editor",
        help=(
            "Edit inline. Click 'Re-validate' to refresh warnings. "
            "JSON is parsed strictly — trailing commas etc. will trip the parser."
        ),
        label_visibility="collapsed",
    )

    btn_cols = st.columns([1, 1, 4])
    with btn_cols[0]:
        if st.button("🔄 Re-validate", use_container_width=True):
            st.session_state["extraction_json"] = new_text
            st.session_state["_extraction_validation"] = None
            st.rerun()
    with btn_cols[1]:
        if st.button(
            "💾 Save & generate YAML",
            type="primary", use_container_width=True,
            disabled=not new_text.strip() or (
                validation is not None
                and (validation["total_invalid"] > 0 or validation["json_error"])
            ),
        ):
            with st.spinner("Writing cleaned JSON + YAML…"):
                result = _save_extraction_payload(new_text)
            if result["errors"]:
                st.error("Save completed with errors:")
                for err in result["errors"]:
                    st.code(err)
            if result["cleaned_paths"]:
                st.success("Cleaned JSON written:")
                for p in result["cleaned_paths"]:
                    st.code(p, language="text")
            if result["yaml_paths"]:
                st.success("YAML written — ready for `bim-orchestrator --check`:")
                for p in result["yaml_paths"]:
                    st.code(p, language="text")

    # ── Bottom helper text ─────────────────────────────────────────────
    st.divider()
    st.caption(
        "Save is blocked when there are schema-invalid rules or JSON parse "
        "errors. Heuristic warnings (unit / scope / fragmentation) are "
        "advisory — you can save anyway, but the CLI's `--strict` mode will "
        "later block them."
    )


# Wire all six tabs — Rule Builder is tab[0] (first).
def _proposal_lifecycle(rec: dict) -> tuple[str, str]:
    """(badge, trạng thái issue) for a proposal record — derived, back-compat.

    Old records lack ``issue_status``; fall back to the ``applied`` flag. The
    watcher closes the ACC issue on apply, so applied ⟹ closed (unless the close
    itself failed, which the watcher records as ``applied_pending_close``).
    """
    if rec.get("applied"):
        istat = rec.get("issue_status") or "closed"
        if istat == "closed":
            return "✅ Đã áp dụng", "🔒 Issue đã đóng (closed)"
        return "✅ Đã áp dụng", "⚠️ Đã ghi Revit — issue CHƯA đóng được (thử lại)"
    return "🕓 Chờ duyệt", "🔓 Issue đang mở — đặt status **In progress** trên ACC để duyệt"


def _proposal_rules(rec: dict) -> str:
    """Rule id(s) behind a proposal, parsed from each fix's finding_id (rule::eid)."""
    rules = sorted({
        (f.get("finding_id") or "").split("::")[0]
        for f in rec.get("fixes", [])
    } - {""})
    return ", ".join(rules) or "—"


def _fmt_current_cell(f: dict) -> str:
    """Render the 'current value' cell for a proposal fix (v1.4-K22).

    A value INHERITED from the host (the element's own value was empty) reads
    "(trống) ⤺ host: <value>" so the reviewer sees the source before approving;
    otherwise just the original value (or '—' when empty).
    """
    inh = f.get("inherited_from")
    if inh not in (None, ""):
        return f"(trống) ⤺ host: {inh}"
    old = f.get("old_value")
    return "—" if old in (None, "") else str(old)


def _render_proposal(path: "Path", rec: dict, *, applied: bool) -> None:
    """One proposal card. Applied ones show the value NOW in Revit; pending ones
    show current → proposed plus an Ignore button."""
    from bim_orchestrator.ui_phase2 import evidence_cell, source_cell  # Phase 2 (guarded)

    badge, istat_label = _proposal_lifecycle(rec)
    disp = rec.get("display_id") or rec.get("issue_id")
    fixes = rec.get("fixes", [])
    with st.expander(f"#{disp} — {len(fixes)} fixes — {badge}", expanded=not applied):
        st.caption(
            f"issue_id: `{rec.get('issue_id')}` · project: `{rec.get('project_id')}` "
            f"· rule: `{_proposal_rules(rec)}`"
        )
        st.markdown(f"**Trạng thái:** {istat_label}")
        if applied:
            # Fix is committed → the NEW value IS the current value in Revit.
            when = rec.get("applied_at", "")
            st.table([
                {
                    "element": f.get("element_id"),
                    "parameter": f.get("parameter"),
                    "✅ giá trị hiện tại (đã ghi Revit)": f.get("new_value"),
                    "đã sửa từ": _fmt_current_cell(f),
                    **source_cell(f),
                    **evidence_cell(f),
                }
                for f in fixes
            ])
            if when:
                st.caption(f"Áp dụng lúc: `{when}`")
        else:
            st.table([
                {
                    "element": f.get("element_id"),
                    "parameter": f.get("parameter"),
                    "hiện tại": _fmt_current_cell(f),
                    "→ đề xuất": f.get("new_value"),
                    **source_cell(f),
                    **evidence_cell(f),
                }
                for f in fixes
            ])
            if st.button("🚫 Bỏ qua đề xuất này (ignore)",
                         key=f"ignore_{rec.get('issue_id')}"):
                if _ignore_proposal(path):
                    st.session_state["_approval_msg"] = f"🚫 Đã bỏ qua đề xuất #{disp}."
                else:
                    st.session_state["_approval_msg"] = (
                        f"⚠️ Không bỏ qua được đề xuất #{disp} — lỗi di chuyển file "
                        "(kiểm tra quyền ghi thư mục approvals/)."
                    )
                st.rerun()


def _ignore_proposal(path: "Path") -> bool:
    """Archive a pending proposal out of the inbox (reversible — moved, not deleted).

    Moved into ``approvals/_ignored/``; the non-recursive ``*.json`` glob (both
    here and in the watcher) then skips it, so it stops showing AND won't apply.

    Medium: returns True on success, False if the move failed. The caller used to
    swallow an OSError here and still report "ignored", so a proposal that failed
    to archive kept showing (and would still apply) while the UI claimed success.
    """
    dest = path.parent / "_ignored"
    try:
        dest.mkdir(exist_ok=True)
        path.rename(dest / path.name)
    except OSError:
        return False
    return True


def render_approvals_tab() -> None:
    """v1.4-K5/K14 Inbox: approve-gated Revit-fix proposals, lifecycle-tracked.

    The propose side (DesignAgent) writes one record per proposal issue under
    runs/approvals/. A human approves by setting the ACC issue status to
    'In progress'; then 'Apply approved now' runs --apply-approvals-once (the
    ApprovalWatcher) which writes the fixes (one revit_batch = one undo) and
    closes the issue. The inbox separates **Chờ duyệt** (actionable, on top) from
    **Lịch sử** (applied + closed, at the bottom) so the lifecycle reads at a glance.
    """
    import json as _json

    st.subheader("📥 Approvals Inbox")
    st.caption(
        "Đề xuất sửa Revit cần duyệt (approve-gated Path B). Mở issue trên ACC, "
        "đổi status → **In progress** để duyệt, rồi bấm *Apply approved now* "
        "(hoặc chạy CLI `--watch-approvals`). Cần Revit đang mở + Forma cấu hình."
    )
    msg = st.session_state.pop("_approval_msg", None)
    if msg:
        st.success(msg)

    approvals_dir = RUNS_DIR / "approvals"
    records: list[tuple[Path, dict]] = []
    if approvals_dir.exists():
        for p in sorted(approvals_dir.glob("*.json")):
            try:
                records.append((p, _json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue

    pending = [(p, r) for p, r in records if not r.get("applied")]
    applied = [(p, r) for p, r in records if r.get("applied")]
    c1, c2 = st.columns(2)
    c1.metric("🕓 Chờ duyệt", len(pending))
    c2.metric("✅ Đã áp dụng", len(applied))

    if not records:
        st.info(
            "Chưa có đề xuất. Chạy **Full run — Revit** với rule auto-fix bị gate "
            "(autonomy=approve) để sinh proposal issue."
        )
        return

    # ── Chờ duyệt (actionable) — on top ──────────────────────────────────────
    st.markdown("### 🕓 Chờ duyệt")
    if pending:
        for p, r in pending:
            _render_proposal(p, r, applied=False)
        st.divider()
        if st.button("⚙️ Apply approved now (--apply-approvals-once)", type="primary"):
            argv = [sys.executable, "-m", "bim_orchestrator.orchestrator",
                    "--apply-approvals-once"]
            # M9: goes through the same guard as the Run tab -- this also
            # writes into Revit, so it must not race a Run-tab launch.
            result = _launch_guarded(argv, _build_env(), st.empty())
            if result is None:
                return  # blocked: another run is still alive (message already shown)
            rc, _log = result
            if rc == 0:
                st.session_state["_approval_msg"] = (
                    "✅ Hoàn tất — các element đã được ghi vào Revit và issue đã đóng. "
                    "Xem mục **Lịch sử** bên dưới."
                )
                st.rerun()
            else:
                st.error(f"Exit code {rc} — xem log ở trên.")
    else:
        st.caption("Không có đề xuất nào đang chờ duyệt. 🎉")

    # ── Lịch sử (applied + closed) — at the bottom ───────────────────────────
    if applied:
        st.divider()
        st.markdown("### 📜 Lịch sử — đã áp dụng")
        for p, r in applied:
            _render_proposal(p, r, applied=True)


tabs = st.tabs([
    "📋 Rule Builder", "⚙️ Setup", "▶️ Run", "📊 Results",
    "📥 Approvals", "📈 Trend", "📜 Run History",
])
with tabs[0]:
    render_rule_builder_tab()
with tabs[1]:
    render_setup_tab()
with tabs[2]:
    render_run_tab()
with tabs[3]:
    render_results_tab()
with tabs[4]:
    render_approvals_tab()
with tabs[5]:
    render_trend_tab()
with tabs[6]:
    render_history_tab()
