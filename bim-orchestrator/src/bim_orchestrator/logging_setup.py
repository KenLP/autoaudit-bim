"""Logging setup — plain text for TTY, JSON for pipes/files.

Override via env:
    BIM_LOG_FORMAT  = plain | json   (default: plain on TTY, json otherwise)
    BIM_LOG_LEVEL   = debug | info | warning | error  (default: info)

CLI overrides:
    --verbose → BIM_LOG_LEVEL=debug
    --quiet   → BIM_LOG_LEVEL=warning
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar, Token
from typing import Any, Callable, Literal

import structlog

from bim_orchestrator.run_recorder import trace_processor

LogFormat = Literal["plain", "json"]
LogLevel = Literal["debug", "info", "warning", "error"]

# ── P3-2: service event tap ─────────────────────────────────────────────────
# The AuditHub service streams run progress over SSE by tapping the structlog
# pipeline (same contextvar pattern as run_recorder.trace_processor — a tap is
# active only inside the service's audit task; everywhere else this is a
# no-op). Lives HERE (stdlib + structlog only) so the core never imports the
# service package.

_SERVICE_TAP: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "bim_service_event_tap", default=None
)


def set_service_tap(tap: Callable[[dict[str, Any]], None]) -> Token:
    return _SERVICE_TAP.set(tap)


def reset_service_tap(token: Token) -> None:
    _SERVICE_TAP.reset(token)


def service_tap_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    tap = _SERVICE_TAP.get()
    if tap is not None:
        try:
            tap(dict(event_dict))
        except Exception:  # a broken subscriber must never break logging
            pass
    return event_dict


def detect_format() -> LogFormat:
    env_choice = os.environ.get("BIM_LOG_FORMAT", "").lower()
    if env_choice in ("plain", "json"):
        return env_choice  # type: ignore[return-value]
    # Default: plain on interactive TTY, json otherwise
    return "plain" if sys.stderr.isatty() else "json"


def detect_level() -> int:
    env_choice = os.environ.get("BIM_LOG_LEVEL", "info").lower()
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(env_choice, logging.INFO)


def configure_logging(
    *,
    fmt: LogFormat | None = None,
    level: int | None = None,
) -> None:
    fmt = fmt or detect_format()
    level = level if level is not None else detect_level()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            pad_event=28,
        )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # v1 task L: captures events into the active TraceCollector via
            # contextvar. No-op when no collector is active, so this is safe
            # for tests / scripts that don't set up a run folder.
            trace_processor,
            # P3-2: forwards events to the AuditHub SSE stream when a service
            # tap is active (no-op otherwise — same contextvar pattern).
            service_tap_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
