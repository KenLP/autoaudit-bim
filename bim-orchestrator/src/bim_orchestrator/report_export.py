"""v1 report module, Phase 2 — docx / pdf export of the verification report.

The verification report's **canonical** form is Markdown (``verification_report.md``)
— it's diffable, greppable, and renders everywhere. docx / pdf are *exports* of
that single source, produced on demand so there's never a second source of truth.

This module is a thin, honest converter:

* If ``pandoc`` is on PATH, it converts the Markdown to the requested format
  (pandoc handles md→docx natively; md→pdf needs a LaTeX/HTML engine pandoc can
  find). This is the runtime path on any machine with pandoc.
* If pandoc is absent, it does NOT fail — it returns guidance pointing to the
  Claude Code document skills (``anthropic-skills:docx`` / ``anthropic-skills:pdf``),
  which convert the same Markdown. The Markdown stays the deliverable.

No new heavy dependency, no in-house Markdown parser (which would drift from the
renderer). Pure-ish: ``export_report`` does filesystem + subprocess I/O only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

SUPPORTED_FORMATS = ("docx", "pdf")

_GUIDANCE = (
    "pandoc not found on PATH. The verification report's canonical form is "
    "Markdown ({md}). To produce {fmt}:\n"
    "  1. install pandoc (https://pandoc.org) and re-run this command, or\n"
    "  2. convert {md} with the Claude Code document skills "
    "(anthropic-skills:docx / anthropic-skills:pdf)."
)


# v1.5-R6 (3.3): pandoc's default PDF engine (usually pdflatex, when present
# at all) mishandles the report's non-ASCII glyphs (→, ·, — used throughout
# the Markdown). xelatex has native UTF-8/font support — pin it explicitly
# rather than let pandoc guess. ``export_report``'s ``pdf_engine`` param lets
# a caller override (or disable via None) for a machine with a different
# working engine.
DEFAULT_PDF_ENGINE = "xelatex"
# Pandoc/subprocess safety net — a hung LaTeX pass (missing package prompt,
# network font fetch, etc.) must not hang the CLI forever.
_PANDOC_TIMEOUT_SECONDS = 120


def _engine_looks_missing(detail: str, engine: str) -> bool:
    """Heuristic: does this pandoc stderr look like "the pdf-engine isn't
    installed" rather than some other conversion failure? Conservative (only
    a substring match on the engine name) — a false positive just means we
    ALSO try the no-engine fallback, which is harmless."""
    return engine.lower() in detail.lower()


def export_report(
    md_path: Path,
    fmt: str,
    *,
    out_path: Path | None = None,
    pandoc: str | None = None,
    pdf_engine: str | None = DEFAULT_PDF_ENGINE,
) -> tuple[Path | None, str]:
    """Convert ``md_path`` (a Markdown report) to ``fmt`` (``docx``|``pdf``).

    Returns ``(output_path, message)`` on success or ``(None, message)`` when the
    conversion couldn't run (missing file, unsupported format, pandoc absent, or a
    pandoc error). Never raises for the "no pandoc" case — that's expected and
    handled with guidance, since Markdown is the canonical output.

    ``pandoc`` overrides the executable lookup (for tests). ``pdf_engine``
    (PDF only; ignored for docx — 3.3) pins ``--pdf-engine=<engine>``; pass
    ``None`` to let pandoc pick its own default. When the pinned engine isn't
    installed, this retries ONCE without the flag (honest fallback) before
    giving up with guidance naming exactly what's missing.
    """
    if fmt not in SUPPORTED_FORMATS:
        return None, f"unsupported format '{fmt}' (supported: {', '.join(SUPPORTED_FORMATS)})"
    if not md_path.exists():
        return None, f"report not found: {md_path}"
    out = out_path or md_path.with_suffix(f".{fmt}")

    exe = pandoc or shutil.which("pandoc")
    if exe is None:
        return None, _GUIDANCE.format(md=md_path, fmt=fmt)

    base_cmd = [exe, str(md_path), "-o", str(out)]
    engine = pdf_engine if fmt == "pdf" else None
    cmd = [*base_cmd, f"--pdf-engine={engine}"] if engine else base_cmd

    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, check=True, capture_output=True, text=True,
            timeout=_PANDOC_TIMEOUT_SECONDS,
        )

    try:
        _run(cmd)
    except FileNotFoundError:
        return None, _GUIDANCE.format(md=md_path, fmt=fmt)
    except subprocess.TimeoutExpired:
        log.warning("report_export.pandoc_timeout", fmt=fmt, seconds=_PANDOC_TIMEOUT_SECONDS)
        return None, (
            f"pandoc timed out after {_PANDOC_TIMEOUT_SECONDS}s converting to {fmt}"
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        if engine and _engine_looks_missing(detail, engine):
            log.warning("report_export.pdf_engine_missing", engine=engine, error=detail)
            try:
                _run(base_cmd)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc2:
                detail2 = getattr(exc2, "stderr", None) or str(exc2)
                return None, (
                    f"pandoc failed converting to pdf: `{engine}` engine not found "
                    f"({detail}); fallback without a pinned engine also failed "
                    f"({detail2}). Install {engine} (https://www.tug.org/xetex/) or "
                    f"any pandoc-supported PDF engine, or convert {md_path} with the "
                    f"Claude Code document skills (anthropic-skills:pdf)."
                )
            log.info("report_export.pdf_engine_fallback", missing_engine=engine, out=str(out))
            return out, f"wrote {out} (fallback: `{engine}` not found, used pandoc's default engine)"
        log.warning("report_export.pandoc_failed", fmt=fmt, error=detail)
        return None, f"pandoc failed converting to {fmt}: {detail}"

    log.info("report_export.done", fmt=fmt, out=str(out))
    return out, f"wrote {out}"


__all__ = ["SUPPORTED_FORMATS", "export_report"]
