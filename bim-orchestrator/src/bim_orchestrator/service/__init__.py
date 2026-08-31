"""AuditHub service (Phase 3, P3-2) — FastAPI wrapper around the orchestrator.

ORCHESTRATE-ONLY (D6): no business logic lives here. The service triggers
``orchestrator.audit`` / reads run folders / mirrors the Streamlit "Apply
now" button — a thin, localhost-only (127.0.0.1:8601, no auth at P3) API for
the Revit panel, automation, and the pilot.

Requires the ``service`` extras group: ``uv sync --extra dev --extra service``.
"""
