# Changelog

## v1.7 — first public release

AutoAudit reads a Revit or ACC model, checks it against rules you wrote in plain
language, and proposes the fix. Every write is previewed, gated on an explicit
approval, and recorded.

This is the first release published outside the private development repo. What
follows is what the engine can do today, grouped by capability rather than by
the release it happened to land in.

### Rules are data

A rule declares **Scope → Check → Severity → Action** in YAML. Six requirement
kinds cover the ground: value present, canonical format, numeric comparison,
regex match, uniqueness within a set, and comparison against a related element
(a door against the wall that hosts it, for example).

A scope filter narrows any rule to a subset of elements. Categories come from a
catalog (`config/ost_catalog.yaml`), so adding one is a config edit, not a code
change. Units live in the rule (`unit:`), and the engine converts Revit's
internal storage values before comparing.

Authoritative value lists are first-class: a rule can require membership in an
approved set (`config/reference.*.yaml`), or resolve a required value through a
code table keyed on a related element (`config/lookup.*.yaml`). A table row can
mark a case explicitly exempt, and an exempt element is reported as compliant by
exemption — not as a finding, and not as a silent pass.

### The natural-language Rule Builder

Describe a check in a sentence; the builder drafts the YAML, and you edit it in
a form before saving. It is grounded in the real Revit parameter catalog per
category, so it will not offer a parameter that does not exist there, and it
refuses read-only parameters as write targets. Rules import and export as
[IDS](https://www.buildingsmart.org/standards/bsi-standards/information-delivery-specification-ids/).

Drafting is the one step that calls a language model, using your own API key.
Everything downstream of the saved YAML is deterministic.

### Four outcomes, including "I could not tell"

Every (element, rule) pair lands in one of four buckets: **compliant**,
**non-compliant**, **needs human judgment**, or **missing the data needed to
decide**. The fourth bucket is the point — a check that cannot see a value says
so rather than passing quietly.

### Fixes it can compute, and fixes it will not guess

For problems with a deterministic answer, the engine computes the exact value:
normalise a unit or format, compose from a template, restructure a name, look it
up in an approved list, or inherit it from the host element. It shows the before
and after for each one.

Where no deterministic answer exists it raises an issue for a human and says
why. It never invents a value.

Two write paths: an **ACC issue** for a person to resolve, or a **Revit
parameter write**. Writes are grouped one proposal per rule, listing every
affected element, and a batch of writes commits as a single undo step.

### The approval loop

An approval-gated fix becomes a proposal issue in ACC. A human moves it to *In
progress*; a watcher then applies the parked writes and closes the issue.

Between proposing and applying, two things are checked. The write set is
fingerprinted, and a fingerprint mismatch blocks the apply — so an edited
proposal cannot be executed. And the live values are re-read: if a value already
matches the proposal the issue is closed without writing, and if it has drifted
to something else entirely the write is held back rather than overwriting work
someone did in the meantime.

### Reports that render, never re-derive

Each run writes a verification report built only from what the run recorded —
including the elements that **passed**, which is the defence against a silent
false negative. It never re-runs a check to render, because a second evaluation
is a second source of truth that can disagree with the first.

Each rule comes with a recipe for verifying the result natively in Revit or ACC,
and the engine can auto-create the matching Revit schedule. Reports export to
docx and pdf.

### Unattended operation

`--audit` runs a profile end to end without a human, suitable for a scheduled
task. Scheduled runs are propose-only by construction: they never write, only
raise proposals. Issues are deduplicated across runs, so a nightly audit does
not re-raise the same finding every night, and each run writes a delta against
the previous comparable run — what got resolved, what is new, what persists.

### Deployment

No Node.js on the host. Revit connectivity is HTTP-direct to the
[RevitMCP add-in](https://github.com/KenLP/RevitMCPServer) (MIT, Revit
2025–2027; v0.8+ for batched writes). ACC connectivity is a single executable
downloaded from a public release, or the hosted service at
<https://mcp.bimlynx.com>.

Installing is deliberately light: `sentence-transformers` — sole parent of
torch, and 476 MB of what used to be a 1.4 GB install — is now the optional
`rag` extra, because nothing in the engine, the reports or `--demo` ever loads
it. Add it with `uv sync --extra dev --extra rag` if you want the default RAG
embedder.

### Verifying what you install

`forma-mcp.exe` is unsigned, so the fetch script does the job a signature
would: it downloads to a temporary file, verifies the SHA-256 against the
digest published with the release, and only then installs it. A mismatch
discards the download and leaves any existing copy alone. The verified hash is
kept beside the binary and `--doctor` re-checks it, so a file that changes
after install is caught as well. Pin the hash yourself with `-ExpectedSha256`
for a locked-down machine — that is the only mode that does not trust the
release origin.

### Not in this release

No installer and no code signing — treat a production rollout as a pilot. Signing
the ACC server binary is the proper fix for the integrity story above.
AI-assisted remediation is an optional private extension; with it absent, which
is the default, the engine is fully deterministic and the test suite passes
offline with no credentials and no API key.
