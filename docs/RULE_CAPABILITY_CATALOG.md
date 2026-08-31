# Rule Capability Catalog — what the Rule Builder understands

**AutoAudit Rule Builder** turns plain-language compliance requirements into
deterministic, machine-checkable YAML rules. You can type a requirement
directly into the builder — in **English or Vietnamese** — or extract a whole
rule set from a **BEP / specification / building-code PDF**. Either way, the
output is the same: a YAML rule the audit engine runs with **no LLM in the
loop at check time**.

> **Governance in one paragraph.** An LLM helps *draft* the rule from your
> sentence — but it never holds the pen. Deterministic post-processing
> re-asserts the intent your sentence stated (quoted formats, inheritance
> clauses, approved-set memberships), the rule is validated against the live
> Revit parameter catalog before it saves, and at runtime the check is pure
> rules-engine evaluation. Scheduled and unattended audits are **propose-only
> by construction** — every model write they produce goes through the approval
> loop: dry-run preview → a reviewable proposal issue in ACC → a human flips it
> to "In progress" → one audited batch write (one undo step), integrity-checked
> against a fingerprint of what was approved. Interactive runs use that same
> loop by default; a single policy file (`autonomy.yaml`) is the one place that
> widens it, and out of the box it admits only deterministic template fills and
> whatever severity you choose to mark auto. A value a model proposed is never
> eligible, whatever the policy says.

**How to read this catalog.** Each entry gives: the intent, how to phrase it,
1–2 worked examples (your sentence → what the compiled rule does), and the fix
behaviour — **report-only** (an ACC issue is raised, grouped by rule) or
**auto-fix** (a deterministic correction, proposed for approval — see the
governance note above for the one narrow exception an interactive run allows).

**Supported categories** (extendable by catalog, not by code): Rooms · Walls ·
Doors · Windows · Floors · Ceilings · Structural Columns · Structural Framing ·
Generic Models · Furniture · Ducts · Pipes · Cable Trays. Parameters are
grounded against the live model's parameter catalog per Revit version, so the
builder proposes real, writable parameters — and refuses read-only ones as
write targets.

---

## Quick index

| # | Use case | Typical phrasing | Fix behaviour |
|---|---|---|---|
| A1 | Required data is present | "Every door must have a Mark" | report-only (or auto when composable/inheritable) |
| A2 | Values are unique | "Door Marks must not repeat" | auto (renumber, approve-gated) |
| A3 | Identifiers composed from other data | "When Mark is blank, build it from System Name" | auto |
| B1 | Canonical unit format | "Fire Rating must read 'X HR'" | auto |
| B2 | Enumerated value mapping | "'NR' or '0' must read 'Not Rated'" | auto |
| B3 | Pattern (regex) rules | "Mark must match ABC-###" | report-only, or auto when the pattern is a unit/separator reshape |
| B4 | Naming conventions | "Wall types follow A_Wall_{Function}_{Thickness}_{FireRating}" | auto (recoverable names), report-only (unrecoverable) |
| C1 | Membership in an approved set | "Assembly Code must be a valid Uniformat code" | auto (known aliases), report-only (off-list) |
| C2 | Value required by a code table | "Door rating per IBC 716 for the host wall" | report-only + explicit exemptions |
| D1 | Numeric thresholds with units | "Ceiling height at least 2400 mm" | report-only |
| E1 | Consistency with a related element | "Door rating ≥ host wall rating" | report-only, or auto when combined with inherit |
| F1 | Inherit from host when empty | "If unset, take the host wall's Fire Rating" | auto |
| F2 | Present AND canonical, inherit when empty | "Must read 'X HR'; inherit from host when empty" | auto |
| G1 | Conditional scope | "Only external doors…", "accessible-route doors…" | gates any rule above |

---

## A. Data completeness

### A1 — Required data is present

**Ask for:** a parameter that must be filled on every element of a category.

- *"Every door must have a Mark."* → flags doors whose `Mark` is empty or
  missing (blank strings count as missing — an instance value of `""` does not
  silently fall back to the type). Report-only: one ACC issue per rule listing
  all offending elements.
- *"Phòng nào cũng phải có Department."* (Vietnamese input works the same) →
  same check on Rooms/`Department`.

When the missing value is *computable* — composable from other parameters (A3)
or inheritable from a host (F1) — the same finding is routed to an
**auto-fix** proposal instead of a bare issue. When it is not computable, the
engine never invents a value: it raises the issue.

### A2 — Values are unique within the model

**Ask for:** no duplicates in an identifier.

- *"Door Marks must be unique."* → detects every member of every duplicate
  cluster (two doors on `D-01` **and** two on `D-05` → all four flagged). The
  auto-fix renumbers to the next available value, per element — always as an
  approve-gated proposal, never a silent write.

### A3 — Identifiers composed from other data

**Ask for:** a blank identifier assembled deterministically from other
parameters of the same element (or its containing space).

- *"Every duct must have a Mark; when blank, compose it from the duct's
  System Name."* → empty `Mark` becomes e.g. `Supply-01`, `Supply-02` — a
  template fill plus a per-rule sequence number. Deterministic, so it
  qualifies for auto-fix. This is the one family of fill an interactive run may
  apply directly — the value is composed from data already in the model, not
  judged — and even that is demoted to a proposal in a scheduled audit.

---

## B. Format & naming

### B1 — Canonical unit format

**Ask for:** a value recorded in exactly one written form. The builder
derives the format from **your literal** — if you write `'X HR'`, the rule
enforces `2 HR`, never a "more conventional" variant like `2-hour`. Supported
dimensions: duration/fire-rating (minutes/hours), length (mm/cm/m), area (m²).

- *"A door's Fire Rating must be recorded as 'X HR'."* → `90 min` →
  suggested fix `1.5 HR`; a door already at `2 HR` is **compliant and left
  alone** (a format rule must never flag values that already conform).
- *"Chiều dài ghi bằng mm, dạng 'X mm'."* → `1.2 m` → `1200 mm`.

### B2 — Enumerated value mapping

**Ask for:** a small fixed vocabulary, with known aliases folded in.

- *"Blank, 'NR' or '0' in Fire Rating must read 'Not Rated'."* → a
  deterministic map `{nr → Not Rated, 0 → Not Rated}`; anything outside the
  map is reported, never guessed.

### B3 — Pattern (regex) rules

**Ask for:** a value matching a pattern. Three flavours, all from natural
phrasing:

- *"Room numbers must be three digits."* → match required; non-matching
  values reported (no safe auto-fix for arbitrary patterns → report-only).
- *"Room names must not contain digits."* → negated pattern.
- *"Fire Rating must match the pattern 'X HR'; values like '90 min' should be
  auto-converted."* → pattern **plus** auto-normalization: the engine tries
  its deterministic canonicalisers and keeps the one that satisfies your
  pattern (`90 min` → `1.5 HR`). The compiled pattern accepts everything the
  conversion can produce (decimals included) — a rule never rejects the very
  fix it asked for.
- A "skip if empty" variant exists for patterns that should only apply when
  the field is filled (pair it with A1 if presence is also required).

### B4 — Naming conventions

**Ask for:** family/type names following a firm convention.

- *"Family names use underscores, not spaces or hyphens."* → separator-only
  cleanup (`ADSK Fur Chair` → `ADSK_Fur_Chair`). Auto-fix.
- *"Wall type names must follow 'A_Wall_<Function>_<Thickness>_<FireRating>';
  names like 'Wall Ext 200mm 2HR Copy' must be rewritten into that form."* →
  a token-capture template: recoverable names are rebuilt
  (`Wall Ext 200mm 2HR Copy` → `A_Wall_Ext_200_2HR`), names already in
  canonical form re-match and stay untouched (the transform is idempotent),
  and names missing the tokens (`Basic Wall Copy 2`) are **reported, not
  invented** — the engine cannot conjure a Function or rating that isn't in
  the name.

Renaming resolves the right target automatically: `Family Name` → rename the
family; `Type Name` → rename the type; a type-carried parameter → write the
type once (not N instances).

---

## C. Value validity

### C1 — Membership in an approved set

**Ask for:** the value must come from an authoritative list — a
classification table, an approved materials palette, a valid type list.

- *"Assembly Code must be a valid Uniformat code."* → membership check
  against a versioned reference set: exact match → compliant; a known
  alias/casing variant → deterministic auto-fix to the canonical entry;
  anything else → ACC issue (**never fuzzy-guessed**).
- *"Vật liệu phải thuộc bảng vật liệu được duyệt."* → same mechanism against
  your firm's palette file.

Shared/openBIM parameter conventions (COBie fields, classification
parameters) are recognised: a vague *"must be valid"* on such a parameter is
steered to the right registered reference set deterministically at save time
— the builder won't accept an invented set name.

### C2 — Value required by a code table

**Ask for:** a requirement whose *threshold lives in a code table keyed by a
related element* — the classic building-code shape.

- *"Door opening protection per IBC 716: the door's rating must meet the
  value the table gives for its host wall's type and rating."* → the rule
  cites a lookup table (wall situation → required minutes). A wall row
  explicitly marked not-rated → the door is **exempt** (counted as compliant
  in the report's PASS set, with the exemption stated); a wall situation with
  no row → **manual review**, never a guessed threshold.

The table is data (`lookup.<name>.yaml`), not prose transcribed into the rule
— auditable and swappable per jurisdiction.

---

## D. Numeric thresholds

### D1 — Numeric compare with units

**Ask for:** any ≥ / > / ≤ / < / = comparison, in the unit you speak.

- *"Rooms must have an unbounded height of at least 2.4 m."* → threshold
  2.4 m; the engine converts from Revit's internal storage units — you never
  write feet.
- *"Accessible-route doors must have a clear width of at least 900 mm."* →
  numeric threshold **plus** a scope gate (see G1). Report-only: a wrong
  dimension is a design decision, not a data fix.

---

## E. Cross-element consistency

### E1 — Consistency with a related element

**Ask for:** one element's value compared against a *related* element's —
its host, or its containing room/space.

- *"A door in a rated wall must carry at least the host wall's fire
  rating."* → compares fire ratings semantically (`2 HR` vs `90 min` parses
  to minutes before comparing — no string accidents). Phrase it with
  *"must carry / inherit"* and the rule also gets the inherit auto-fix (F1):
  non-compliant doors are proposed the host's value.

Comparison kinds are chosen by the parameter's nature (numeric · fire-rating
· text identity), so a rating never gets compared as a plain string.

The related element here is the **host** (a door's wall, a window's wall).
Comparing against the *containing space* is not a relation check today — to tie
an element's identifier to the space it sits in, compose it instead (A3), which
does read the containing space.

---

## F. Inheritance & auto-population

### F1 — Inherit from host when empty

**Ask for:** empty values filled from the hosting element.

- *"Doors must have a Fire Rating; if not set, take the host wall's."* →
  empty door rating → propose the wall's value. Host has no value either →
  ACC issue (**the engine never invents data**). When several differently-rated
  hosts drive one shared type, the write collapses to the **maximum** rating,
  with the conflict listed in the proposal: this parameter records the rating
  the code *requires*, so taking the minimum would leave the
  higher-requirement doors declaring less than their own host demands — and a
  presence/format rule would then pass them forever. The maximum over-states
  for the lower-rated host, which is visible in the proposal and safe.

### F2 — Present AND canonical, inherit when empty (compound)

**Ask for:** the full lifecycle of one parameter in one sentence.

- *"A door's Fire Rating must be recorded as 'X HR'; when empty, inherit the
  fire rating of its host wall and normalise it to the same form."* → ONE
  rule covering three cases: `90 min` → fix to `1.5 HR`; empty with host
  `120 min` → inherit + normalise to `2 HR`; already `2 HR` → compliant,
  untouched. This exact sentence shape is deterministically guaranteed — the
  quoted format and the inherit clause survive extraction by construction,
  not by model goodwill.

---

## G. Scope & applicability

### G1 — Conditional scope

**Ask for:** any rule above, restricted to a subset.

- *"Only external doors need this check."* / *"chỉ áp dụng cho phòng
  Residential"* → the rule runs only on elements whose gating parameter
  matches (e.g. `Function ~ exterior`, `Department ~ residential`).
- *"Accessible-route doors must be at least 900 mm wide."* → the qualifier
  itself becomes the gate (`Function ~ access`) — a standard corridor door at
  700 mm is out of scope and correctly not flagged.

The builder is deliberately conservative here: an adjective that merely
*names the value being checked* ("a **fire** door's **Fire Rating**") is not
turned into a gate — a guessed gate that matches nothing would silently
disable the rule.

---

## What happens after a rule fires

- **Report-only findings (Path A)** group into **one ACC issue per rule**,
  listing every affected element — never one issue per element.
- **Auto-fixes (Path B)** gather into **one approve-gated proposal issue per
  rule** showing `element | current → proposed`. A human approves by flipping
  the issue status; the engine then re-verifies nothing changed since the
  proposal (fingerprint + live re-preview), applies everything as **one batch =
  one undo step**, and closes the loop.
- Both issue bodies obey ACC's own 1000-character description limit: a longer
  list is trimmed at a line boundary with a footer stating how many of how many
  rows are shown. Only the *displayed* text is ever shortened — the approved
  write-set, the audit record and the verification report always carry every
  element.
- Every run emits a **verification report**: which rules ran, the full PASS
  set (including exemptions), provenance hashes, and a native re-check recipe
  so you can verify the tool's claims *without trusting the tool*. One command
  turns those recipes into per-rule schedules inside your Revit model.
- Severity (Low / Medium / High) is yours to set per rule, independent of the
  check type.

## Honest limits (by design)

- **No invented data.** Missing tokens, absent host values, off-list codes,
  unmatched lookup rows → issue or manual review. Every auto-fix is a
  deterministic transform of data already in the model or in your reference
  files.
- **One rule per sentence.** Compound lifecycles like F2 are supported where
  the mechanics are deterministic; a paragraph of intertwined requirements
  should be split (the PDF extractor does this splitting for you).
- **Language authors data rules; geometry is structured input.** The
  natural-language path of this catalog authors data/parameter rules only.
  Geometric clearance checks are authored in the builder's dedicated
  Geometry mode — explicit check type, threshold, direction and reference
  category, not free prose — and run in the same deterministic engine.
  LOD validation and spatial QC run as separate deterministic audit axes,
  authored as profiles.
- **Nothing writes unattended unless your own policy says so.**
  Scheduled/nightly audits are propose-only by construction — there, no
  phrasing produces an unattended write. In an interactive run, `autonomy.yaml`
  is the single place that decides what may apply without a human; shipped
  defaults admit deterministic template fills (A3) and anything you marked low
  severity. Model-proposed values are never eligible, and life-safety
  parameters route to a human by policy.

---

*This catalog reflects the shipped rule engine and Rule Builder. Every
example above is exercised by an automated NL→rule→run evaluation suite that
extracts the rule from the natural-language sentence with the production
prompt, runs it against a fixture model, and asserts both the compiled rule
and the audit outcome (see the private QA harness reports for the current scenario
count and pass rate — a number printed here would only drift).*
