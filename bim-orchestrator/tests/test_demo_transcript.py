"""Snapshot test of the FULL `--demo` transcript — the assembled-product tier.

Why this tier exists (2026-08-16, adopted from the DeepSeek Harness review —
their testing.md names the class "green unit tests, broken product"): this
repo has been bitten three times by bugs that live in the WIRING, where the
unit tests on both ends stay green:

  * L-01 — `--fail-on-partial-coverage` declared by argparse (tested) and
    honoured by `_exit_code_for` (tested), but `_dispatch` never forwarded
    it. The flag was inert.
  * #37 — a `→` glyph crashed the post-run summary on cp1252 stdout, and the
    crash marked a CONVERGED run as failed. No test drove `main()`.
  * v1.7-R9 — a render call was removed from the wire while its function
    (and the function's tests) stayed green.

One subprocess run of `bim-orchestrator --demo` crosses every one of those
wires: argparse → dispatch → agents → engine → design → recorder →
verification report → console summary → exit code. The transcript IS the
assertion: any change to what a user sees turns this red with a line-level
diff, and an intentional change re-records the snapshot so the diff shows up
in the PR for review.

What this does NOT replace: unit/mutation tests (a snapshot can't tell a
correct number from a wrong one — only a CHANGED one) and Rail-B live probes
(the demo runs on mock clients by design).

Maintenance contract:
  * Red after an intentional surface change → re-record:
        BIM_UPDATE_SNAPSHOT=1 uv run pytest tests/test_demo_transcript.py
    then REVIEW the snapshot diff like code — that review is the point.
  * Keep the normalizer MINIMAL ("fix fixtures, not normalizers"): every
    added pattern is a place a regression can hide. Current patterns strip
    only genuine nondeterminism, each verified against two live runs whose
    normalized transcripts were byte-identical.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT = Path(__file__).parent / "snapshots" / "demo_transcript.txt"
_RUNS_DIR = _PROJECT_ROOT / "runs"

# Every pattern here was justified by diffing two real runs (2026-08-16):
# the ONLY differences were run id, timestamps, the date-stamped checkpoint
# path, and timing-derived numbers. Nothing else may be normalized without
# the same evidence.
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"run-[0-9a-f]{8}"), "run-<ID>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+\d{2}:\d{2})?"), "<TS>"),
    (re.compile(r"checkpoints([\\/]+)\d{8}"), r"checkpoints\1<DATE>"),
    (re.compile(r"iteration_(\d+)_\d{8}T\d{6}Z"), r"iteration_\1_<TS>"),
    (re.compile(r"\d+\.\d+s\b"), "<N>s"),
    (re.compile(r'"elapsed_ms": [0-9.]+'), '"elapsed_ms": <MS>'),
    (re.compile(r'"elements_per_sec": [0-9.]+'), '"elements_per_sec": <RATE>'),
]

# Path SEPARATORS are platform noise: the snapshot was first recorded on
# Windows (`<ROOT>\\config\\...`) and CI runs Ubuntu (`<ROOT>/config/...`) —
# CI run 31984101406 (2026-08-17) failed with separator flips as the ONLY
# diff class, which is this pattern's two-run evidence. Scoped to
# `<ROOT>`-anchored path segments (every path the product prints is
# package-anchored — the empty-cwd assertion enforces that), so JSON escapes
# like `→` elsewhere in the transcript are never touched.
_ROOTED_PATH = re.compile(r"<ROOT>(?:\\\\|\\|/)[^\s\"']*")


def _canon_separators(m: "re.Match[str]") -> str:
    return m.group(0).replace("\\\\", "/").replace("\\", "/")


def _normalize(text: str) -> str:
    # The project root appears both raw and JSON-escaped (structlog dumps
    # paths inside JSON strings); replace the escaped form first so the raw
    # replacement can't split it.
    root = str(_PROJECT_ROOT)
    text = text.replace(root.replace("\\", "\\\\"), "<ROOT>")
    text = text.replace(root, "<ROOT>")
    text = _ROOTED_PATH.sub(_canon_separators, text)
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    # CRLF/LF differences are platform noise, not product behaviour.
    return text.replace("\r\n", "\n").strip() + "\n"


def _parse_run_folder(stdout: str) -> Path | None:
    """The run folder the demo created, parsed from ITS OWN output.

    `DEFAULT_RUNS_DIR` is anchored to the package, not the cwd, so the demo
    writes into the real `runs/` the AU demo scripts also use. This test
    must leave that directory exactly as it found it — the folder to delete
    is identified from the transcript, never by "newest folder" guessing
    (a concurrent real run would be the newest folder).
    """
    m = re.search(r"Run folder:\s+(\S+runs[\\/]run-[0-9a-f]{8})", stdout)
    return Path(m.group(1)) if m else None


@pytest.mark.slow
def test_demo_transcript_matches_snapshot(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # cwd = an EMPTY temp dir, deliberately: every output path in the
    # product is supposed to be package-anchored, so anything that appears
    # in the cwd after the run is a cwd-relative write someone introduced
    # by accident — asserted below.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # C-1 (review round 7, 2026-08-17): the demo's cross-run proposal dedup
    # reads whatever approvals dir it is given — the first recording of this
    # snapshot inherited THIS machine's gitignored records and pinned
    # `proposal_already_parked` lines carrying their fingerprints, so a fresh
    # clone (no records → proposals get CREATED) went red, and a re-record
    # there flip-flopped on the second run (records now exist). An EMPTY
    # per-test approvals dir makes the transcript the deterministic first-run
    # one on every machine, every time.
    approvals = tmp_path / "approvals"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from bim_orchestrator.cli import main; "
            "sys.argv = ['bim-orchestrator', '--demo', "
            f"'--approvals-dir', {str(approvals)!r}]; "
            "raise SystemExit(main())",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=300,
    )

    run_folder = _parse_run_folder(proc.stdout)
    try:
        assert proc.returncode == 0, (
            f"--demo exited {proc.returncode}\nSTDERR tail:\n{proc.stderr[-2000:]}"
        )

        # Verify the world, not the self-report: the artifact the transcript
        # advertises must actually exist.
        assert run_folder is not None, "transcript never printed a run folder"
        assert (run_folder / "verification_report.md").exists()

        # No cwd-relative writes: the temp cwd must still be empty.
        strays = [p.name for p in cwd.iterdir()]
        assert not strays, f"--demo wrote into the cwd: {strays}"

        got = _normalize(proc.stdout)
        # The per-test approvals dir path is machine-local — the demo prints
        # it when parking proposals. Same class as <ROOT>, normalized the
        # same way (a straight replace, not a regex a regression could hide
        # behind).
        approvals_str = str(approvals)
        got = got.replace(approvals_str.replace("\\", "\\\\"), "<APPROVALS>")
        got = got.replace(approvals_str, "<APPROVALS>")

        if os.environ.get("BIM_UPDATE_SNAPSHOT"):
            _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
            _SNAPSHOT.write_text(got, encoding="utf-8", newline="\n")
            pytest.skip(f"snapshot re-recorded: {_SNAPSHOT}")

        assert _SNAPSHOT.exists(), (
            "no snapshot on disk — record one with "
            "BIM_UPDATE_SNAPSHOT=1 uv run pytest tests/test_demo_transcript.py"
        )
        expected = _normalize(_SNAPSHOT.read_text(encoding="utf-8"))
        assert got == expected, (
            "the --demo transcript changed. If this is an INTENTIONAL "
            "surface change, re-record (BIM_UPDATE_SNAPSHOT=1) and review "
            "the snapshot diff in the PR; if not, a wire just broke — the "
            "assert diff above shows exactly where."
        )
    finally:
        # Leave runs/ as we found it. Guard against a parse pointing outside
        # runs/ before deleting anything.
        if (
            run_folder is not None
            and run_folder.exists()
            and run_folder.parent == _RUNS_DIR
        ):
            shutil.rmtree(run_folder, ignore_errors=True)
