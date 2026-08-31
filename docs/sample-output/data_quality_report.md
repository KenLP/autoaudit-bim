# Data Quality Report

Generated: 2026-08-27 13:49:24
Project: `demo-villa-simulated`
Run iteration: 2

These elements are missing parameter values needed to evaluate compliance. They are data quality issues, NOT design violations -- the designer needs to fill in the values before re-running the audit. Items are grouped by parameter so bulk fixes are easy.

> **Scope note.** QCAgent always evaluates *every* rule in the active YAML against *every* in-scope element, regardless of the operator's `--rule` filter. The `--rule` flag scopes only the subsequent DesignAgent step (ACC issue creation + Path B writes); this report reflects the full QC pass. If you expected fewer rows because you set `--rule foo`, that's working as designed -- the filter doesn't (and shouldn't) hide data-quality information.

## Summary

- Total items: 2

Most common missing parameters:
- `Fire Rating`: 2 element(s)

## Items by parameter

### Parameter: `Fire Rating`

| # | Element | Rule | Severity | Citation |
|---|---------|------|----------|----------|
| 1 | Door-Single-Flush - 36x84 (Blank FR) | `demo.doors.fire_rating` | severity_high |  |
| 2 | Door-Single-Flush - 36x84 (Blank FR) | `demo.doors.fire_rating` | severity_high |  |
