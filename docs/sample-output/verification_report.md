<!-- A real `--demo` run, committed so the output is readable without
     installing anything. Verbatim apart from absolute machine paths and the
     operator/host names in the provenance block.
     Reproduce with:  uv run bim-orchestrator --demo  -->

# Verification report — run-e92510d2

> ⚠ DEMO MODE — simulated model (mock Revit + ACC); reasoning & report pipeline are the real production path.

## 0. Provenance

- **Tool version:** 0.1.0
- **Run by:** demo-user
- **Machine:** demo-machine
- **Captured at:** 2026-08-27T06:49:24+00:00 (UTC)
- **Project id:** demo-villa-simulated
- **Run id:** run-e92510d2
- **Elements fetched:** 20
- **Model:** `Demo Villa`
- **Revit:** Autodesk Revit 2026
- **Workshared:** no
- **Rules file:** `config\rules.demo.yaml` — SHA-256 `096d02ba2a33332d3225bc8c94803a06b9ed282d6735b76500a5ea8fba5ddf8c`
  - Verify: PowerShell `Get-FileHash <file> -Algorithm SHA256`

- **Mode:** run-revit
- **Started:** 2026-08-27T06:49:24+00:00 (UTC)
- **Finished:** 2026-08-27T06:49:24+00:00 (UTC)
- **Duration:** 0.13s
- **Iterations:** 2
- **Rules:** `demo_villa`
- **Final status:** converged

## Contents

1. [Executive summary](#1-executive-summary)
2. [How to trust this report](#2-how-to-trust-this-report)
3. [Per-rule verification](#3-per-rule-verification)
   - [`demo.doors.fire_rating`](#rule-demo-doors-fire-rating)
   - [`demo.doors.width_min`](#rule-demo-doors-width-min)
   - [`demo.doors.mark_naming`](#rule-demo-doors-mark-naming)
   - [`demo.rooms.department_required`](#rule-demo-rooms-department-required)
   - [`demo.rooms.number_unique`](#rule-demo-rooms-number-unique)
4. [Per-element appendix](#4-per-element-appendix-query--qc--design--result)
5. [What we did NOT touch, and why](#5-what-we-did-not-touch-and-why)
6. [Audit trail](#6-audit-trail)

<a id="1-executive-summary"></a>
## 1. Executive summary

**Headline: 7 of 52 checks need attention** (5 non-compliant, 0 need human review, 2 missing data).
_converged = the final iteration re-checked every previously-fixed element and found no NEW findings — the loop stopped because it ran out of work, not because it gave up._

- **Coverage:** Elements fetched: **20** · evaluated: **20** (cap `--max-elements`=300) · checks: **52** · evaluability: **96%** (= (total−missing)/total)
  - Skipped as out-of-scope (rule category / scope filter): **48** (element, rule) pair(s) — never counted toward `total`, listed for transparency only.
- **Query coverage:** requested: **2** (Doors, Rooms) · resolved: **2** (Doors, Rooms)
- Rules: **5** · Compliant: **45** (86.5%) · Non-compliant: **5** · Needs human: **0** · Missing data: **2**
- ACC Issues created: **1** · Revit auto-writes: **2** · Approve-gated proposals: **2**
  - Issues by rule: [`demo.doors.width_min`](#rule-demo-doors-width-min) (#1001)
  - Proposals by rule: [`demo.doors.fire_rating`](#rule-demo-doors-fire-rating), [`demo.rooms.number_unique`](#rule-demo-rooms-number-unique)

| Rule | Category | Severity | Parameter | Compliant | Non-compliant | Needs human | Missing |
|------|----------|----------|-----------|-----------|---------------|-------------|---------|
| [`demo.doors.fire_rating`](#rule-demo-doors-fire-rating) | Doors | HIGH | `Fire Rating` | 8 | 2 | 0 | 2 |
| [`demo.doors.width_min`](#rule-demo-doors-width-min) | Doors | HIGH | `Width` | 11 | 1 | 0 | 0 |
| [`demo.rooms.number_unique`](#rule-demo-rooms-number-unique) | Rooms | MEDIUM | `Number` | 6 | 2 | 0 | 0 |
| [`demo.doors.mark_naming`](#rule-demo-doors-mark-naming) | Doors | LOW | `Mark` | 12 | 0 | 0 | 0 |
| [`demo.rooms.department_required`](#rule-demo-rooms-department-required) | Rooms | LOW | `Department` | 8 | 0 | 0 | 0 |

<a id="2-how-to-trust-this-report"></a>
## 2. How to trust this report

This report **renders what the run recorded**; it does not re-run any check. Verify each claim yourself, in tools you already trust — in increasing rigour (a *trust ladder*):

1. **Select by ID** (Revit) / model viewer (ACC) — the atomic, interpretation-free anchor; jump straight to the exact elements.
2. **Schedule** — recreate the finding set as a native schedule (recipe per rule below); it lists the PASS set too, so you can check for false negatives.
3. **View filter + colour override** — make the flagged set visually obvious in a view (where the check maps to a native filter).
4. **ACC Issue + audit chain** — cross-check each raised issue and, via Forma `meta_verify_audit_chain`, its tamper-evident audit entries.

> Lead with the **schedule + filter anchored on the ElementId list** — native, nothing new to learn. Where a check can't be one native filter, the recipe says so and falls back to Select-by-ID + an operands schedule.

<a id="3-per-rule-verification"></a>
## 3. Per-rule verification

**Legend** (fix lifecycle — every outcome cell below uses one of these):

- **QC** — the deterministic rules-engine pass that produced this record's verdict (compliant / non-compliant / needs human / missing data).
- **Revit write (auto-applied)** — a deterministic fix committed straight to the model; batched into one undo entry when the addin supports it, written element-by-element otherwise.
- **awaiting approval (proposal issue)** — a computed fix parked as an ACC issue; applied only after a human sets it to *In progress*.
- **human-only** — an LLM-suggested value for a life-safety parameter; raised as a manual ACC issue and NEVER auto-applied or approve-gated.
- **parked** — a fix that was computed but held back (e.g. by the issue budget).
- **pending** — a fix was previewed but not yet committed (this run ended, or the write is gated).

<a id="rule-demo-doors-fire-rating"></a>
### Rule `demo.doors.fire_rating`

> Door Fire Rating must be present (inherit from the host Wall if blank) and formatted as "X HR".

- **In plain language:** `Fire Rating` must already be in its canonical form.
- **Outcome:** 8 compliant · 2 non-compliant · 0 need human · 2 missing

**How to verify this yourself (native):**

Schedule **Doors** with columns `Mark`, `Family and Type`, `Fire Rating`, grouped by `Fire Rating` so every distinct spelling sits together. The rule passes only when `Fire Rating` is ALREADY in canonical form; the per-element table shows each value next to the canonical it should be. "Is it canonical?" can't be a view-filter rule, so confirm the flagged set with **Select by ID** (list below).

- **Schedule recipe:** Category = `Doors`; fields = `Mark`, `Family and Type`, `Fire Rating`.
  - Group by Fire Rating
- **View filter:** _not expressible as one native filter rule_ — "Already canonical" is not expressible as a native filter rule. Use the Select-by-ID list + the "Needs attention" table below.
- **Select by ID:** Revit **Manage → Inquiry → Select by ID** (paste): `701, 702, 703, 704`
  - Full scope (audit false negatives): Revit **Manage → Inquiry → Select by ID** (paste): `701, 702, 703, 704, 705, 706, 707, 708, 709, 710, 711, 712`

**PASS set (8)** — listed so you can audit for false negatives:

| Element | ElementId | `Fire Rating` | Verdict |
|---|---|---|---|
| Door-Single-Flush - 36x84 (2 HR OK) | 705 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 706 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 Narrow | 707 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 708 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 709 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 710 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 711 | 2 HR | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 712 | 2 HR | ✓ pass |

**Needs attention (4):**

| Element | ElementId | `Fire Rating` | Canonical | Status → outcome |
|---|---|---|---|---|
| Door-Single-Flush - 36x84 (Blank FR) | 701 | (empty) | 2 HR | Missing data → proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (Blank FR) | 702 | (empty) | 2 HR | Missing data → proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (120 MIN) | 703 | 120 min | 2 HR | Non-compliant → proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (120 MIN) | 704 | 120 min | 2 HR | Non-compliant → proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |

<a id="rule-demo-doors-width-min"></a>
### Rule `demo.doors.width_min`

> Door nominal leaf width must be at least 900 mm (firm standard).

- **In plain language:** `Width` must be >= 900.0 mm.
- **Outcome:** 11 compliant · 1 non-compliant · 0 need human · 0 missing

**How to verify this yourself (native):**

Schedule **Doors** with columns `Mark`, `Family and Type`, `Width`. The rule passes when `Width` is greater than or equal to **900.0**, so a View Filter `Width` **is less than 900.0** + a red override highlights exactly the FAIL set. Sort the schedule by `Width` to eyeball the boundary. The rule's threshold is in **mm**; Revit shows the parameter in the project's display units — convert before comparing if they differ.

- **Schedule recipe:** Category = `Doors`; fields = `Mark`, `Family and Type`, `Width`.
  - Filters (auto-created schedule only — the manual recipe above stays unfiltered): `Width` less 900.0 mm (= 2.9528 internal/storage units — auto-schedule uses raw values)
- **View filter + colour:** rule `Width` **is less than 900.0 mm (convert to your project's display units)** → override **Red — solid fill**.
- **Select by ID:** Revit **Manage → Inquiry → Select by ID** (paste): `707`
  - Full scope (audit false negatives): Revit **Manage → Inquiry → Select by ID** (paste): `701, 702, 703, 704, 705, 706, 707, 708, 709, 710, 711, 712`

**PASS set (11)** — listed so you can audit for false negatives:

| Element | ElementId | `Width` | Threshold | Verdict |
|---|---|---|---|---|
| Door-Single-Flush - 36x84 (Blank FR) | 701 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (Blank FR) | 702 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (120 MIN) | 703 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (120 MIN) | 704 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 705 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 706 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 708 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 709 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 710 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 711 | 914.4 mm | 900.0 | ✓ pass |
| Door-Single-Flush - 36x84 (2 HR OK) | 712 | 914.4 mm | 900.0 | ✓ pass |

**Needs attention (1):**

| Element | ElementId | `Width` | Threshold | Status → outcome |
|---|---|---|---|---|
| Door-Single-Flush - 36x84 Narrow | 707 | 609.6 mm | 900.0 | Non-compliant → ACC Issue #1001 (open) |

<a id="rule-demo-doors-mark-naming"></a>
### Rule `demo.doors.mark_naming`

> Door Mark must follow the "D_NNN" naming convention.

- **In plain language:** `Mark` must match the pattern `^D_\d{3}$`.
- **Outcome:** 12 compliant · 0 non-compliant · 0 need human · 0 missing · **1 auto-fixed**

**How to verify this yourself (native):**

Schedule **Doors** with columns `Mark`, `Family and Type`, sorted by `Mark`. The value must match the pattern `^D_\d{3}$`. Revit view filters can't evaluate a regex, so verify by eye against the sorted column, then confirm the exact flagged set with **Select by ID** (list below). The per-element table also shows each value next to the pattern.

- **Schedule recipe:** Category = `Doors`; fields = `Mark`, `Family and Type`.
  - Sort by Mark
- **View filter:** _not expressible as one native filter rule_ — Revit view filters cannot evaluate a regular expression. Use the Select-by-ID list + the "Needs attention" table below.
- **Select by ID:** _(no element ids)_
  - Full scope (audit false negatives): Revit **Manage → Inquiry → Select by ID** (paste): `701, 702, 703, 704, 705, 706, 707, 708, 709, 710, 711, 712`

**PASS set (12)** — listed so you can audit for false negatives:

| Element | ElementId | `Mark` | Pattern | Verdict |
|---|---|---|---|---|
| 36x84 (Blank FR) | 701 | D_101 | ^D_\d{3}$ | ✓ pass |
| 36x84 (Blank FR) | 702 | D_102 | ^D_\d{3}$ | ✓ pass |
| 36x84 (120 MIN) | 703 | D_103 | ^D_\d{3}$ | ✓ pass |
| 36x84 (120 MIN) | 704 | D_104 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 705 | D_105 | ^D_\d{3}$ | ✓ pass (auto-fixed this run: D 105 → D_105) |
| 36x84 (2 HR OK) | 706 | D_106 | ^D_\d{3}$ | ✓ pass |
| 36x84 Narrow | 707 | D_107 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 708 | D_108 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 709 | D_109 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 710 | D_110 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 711 | D_111 | ^D_\d{3}$ | ✓ pass |
| 36x84 (2 HR OK) | 712 | D_112 | ^D_\d{3}$ | ✓ pass |

**Needs attention (0):**

_0 outstanding — 1 auto-fixed this run (D 105 → D_105) (see below)._

**Auto-fixed (1):**

| Element | ElementId | Old → New | Via |
|---------|-----------|-----------|-----|
| 36x84 (2 HR OK) | 705 | D 105 → D_105 | revit_batch |

<a id="rule-demo-rooms-department-required"></a>
### Rule `demo.rooms.department_required`

> Every room must carry a non-empty Department value.

- **In plain language:** `Department` must be present and non-empty.
- **Outcome:** 8 compliant · 0 non-compliant · 0 need human · 0 missing · **1 auto-fixed**

**How to verify this yourself (native):**

Create a schedule of **Rooms** with columns `Number`, `Name`, `Department`. Any **blank `Department`** cell is a FAIL; a filled cell is a PASS. To see them in a view, add a View Filter `Department` **has no value** and override its colour — every flagged element lights up. Both are 100% native; nothing here trusts the tool's log.

- **Schedule recipe:** Category = `Rooms`; fields = `Number`, `Name`, `Department`.
  - Filters (auto-created schedule only — the manual recipe above stays unfiltered): `Department` has_no_value
- **View filter + colour:** rule `Department` **has no value** → override **Red — solid fill**.
- **Select by ID:** _(no element ids)_
  - Full scope (audit false negatives): Revit **Manage → Inquiry → Select by ID** (paste): `401, 402, 403, 404, 405, 406, 407, 408`

**PASS set (8)** — listed so you can audit for false negatives:

| Element | ElementId | `Department` | Verdict |
|---|---|---|---|
| Guest Bedroom | 401 | General | ✓ pass (auto-fixed this run: (empty) → General) |
| Living Room | 402 | Residential | ✓ pass |
| Dining Room | 403 | Residential | ✓ pass |
| Kitchen | 404 | Residential | ✓ pass |
| Storage Closet | 405 | Services | ✓ pass |
| Corridor | 406 | Circulation | ✓ pass |
| Guest Bedroom 2 | 407 | Residential | ✓ pass |
| Powder Room | 408 | Wet | ✓ pass |

**Needs attention (0):**

_0 outstanding — 1 auto-fixed this run ((empty) → General) (see below)._

**Auto-fixed (1):**

| Element | ElementId | Old → New | Via |
|---------|-----------|-----------|-----|
| Guest Bedroom | 401 | (empty) → General | revit_batch |

<a id="rule-demo-rooms-number-unique"></a>
### Rule `demo.rooms.number_unique`

> Room numbers must be unique across the project.

- **In plain language:** `Number` must be unique across all in-scope elements.
- **Outcome:** 6 compliant · 2 non-compliant · 0 need human · 0 missing

**How to verify this yourself (native):**

Schedule **Rooms** with columns `Number`, `Name`; sort or group by `Number` with *Itemize every instance* on, so duplicate `Number` values land on adjacent rows — that's the native way to spot collisions. A view filter can't express "appears more than once", so confirm each duplicate group with **Select by ID** (list below).

- **Schedule recipe:** Category = `Rooms`; fields = `Number`, `Name`.
  - Sort/Group by Number, enable Itemize every instance
- **View filter:** _not expressible as one native filter rule_ — Uniqueness ("appears more than once") is not a per-element filter rule. Use the Select-by-ID list + the "Needs attention" table below.
- **Select by ID:** Revit **Manage → Inquiry → Select by ID** (paste): `402, 403`
  - Full scope (audit false negatives): Revit **Manage → Inquiry → Select by ID** (paste): `401, 402, 403, 404, 405, 406, 407, 408`

**PASS set (6)** — listed so you can audit for false negatives:

| Element | ElementId | `Number` | Verdict |
|---|---|---|---|
| Guest Bedroom | 401 | 201 | ✓ pass |
| Kitchen | 404 | 102 | ✓ pass |
| Storage Closet | 405 | 103 | ✓ pass |
| Corridor | 406 | 104 | ✓ pass |
| Guest Bedroom 2 | 407 | 105 | ✓ pass |
| Powder Room | 408 | 106 | ✓ pass |

**Needs attention (2):**

| Element | ElementId | `Number` | Status → outcome |
|---|---|---|---|
| Living Room | 402 | 101 | Non-compliant → proposed → 101A (awaiting approval, issue `issue-mock-0002`) |
| Dining Room | 403 | 101 | Non-compliant → proposed → 101B (awaiting approval, issue `issue-mock-0002`) |

<a id="4-per-element-appendix-query--qc--design--result"></a>
## 4. Per-element appendix (Query → QC → Design → Result)

Each row is one `(element, rule)` evaluation as the run recorded it — the value pulled, the verdict, and what was done. The ElementId is the anchor for Select-by-ID.

_43 compliant row(s) with no action omitted — full list in `report_trace.json`._

| Element | ElementId | Rule | Value | Operand | QC | Design/Result |
|---------|-----------|------|-------|---------|----|--------------| 
| Door-Single-Flush - 36x84 (Blank FR) | 701 | `demo.doors.fire_rating` | (empty) | - | Missing data (HIGH) | proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (Blank FR) | 702 | `demo.doors.fire_rating` | (empty) | - | Missing data (HIGH) | proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (120 MIN) | 703 | `demo.doors.fire_rating` | 120 min | - | Non-compliant (HIGH) | proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| Door-Single-Flush - 36x84 (120 MIN) | 704 | `demo.doors.fire_rating` | 120 min | - | Non-compliant (HIGH) | proposed → 2 HR (awaiting approval, issue `issue-mock-0001`) |
| 36x84 (2 HR OK) | 705 | `demo.doors.mark_naming` | D_105 | - | Compliant (after auto-fix) | Revit write: D 105 → D_105 |
| Door-Single-Flush - 36x84 Narrow | 707 | `demo.doors.width_min` | 609.6 mm | - | Non-compliant (HIGH) | ACC Issue #1001 (open) |
| Guest Bedroom | 401 | `demo.rooms.department_required` | General | - | Compliant (after auto-fix) | Revit write: (empty) → General |
| Living Room | 402 | `demo.rooms.number_unique` | 101 | - | Non-compliant (MEDIUM) | proposed → 101A (awaiting approval, issue `issue-mock-0002`) |
| Dining Room | 403 | `demo.rooms.number_unique` | 101 | - | Non-compliant (MEDIUM) | proposed → 101B (awaiting approval, issue `issue-mock-0002`) |

<a id="5-what-we-did-not-touch-and-why"></a>
## 5. What we did NOT touch, and why

A success-only report destroys trust. These are the things the run deliberately left for a human — by design, not by omission:

- **Missing data (2):** the parameter was blank, so compliance could not be computed — a data-quality gap, not a verdict. See [data_quality_report.md](data_quality_report.md).
- **Needs human review (0):** the rule is deterministic but the result needs judgement (e.g. a host rating not in the code table). See [review_queue.md](review_queue.md).
- **Awaiting approval (4):** computed Revit fixes that are safety-gated — they were proposed as ACC issues and will only be written after a human sets the issue to *In progress* (the Approvals flow).

**Fix interactions observed (0):**

_none observed_

<a id="6-audit-trail"></a>
## 6. Audit trail

- Reasoning trace (raw events): [trace.md](trace.md)
- Structured (element, rule) trace: [report_trace.json](report_trace.json)
- Findings (machine-readable): [findings.json](findings.json)
- Outcomes (all 4 buckets): [outcomes.json](outcomes.json)
- Run metadata: [metadata.json](metadata.json)
- ACC audit chain: for any issue above, run Forma `meta_verify_audit_chain` to confirm its tamper-evident dry-run → approval → execute entries.

---
_Generated by bim-orchestrator. This report RENDERS the run's recorded artifacts; it does not re-run checks. Verify every claim natively using the per-rule recipes above._

Integrity: SHA-256 in sidecar (`verification_report.sha256`).
  Verify: PowerShell `Get-FileHash <file> -Algorithm SHA256`