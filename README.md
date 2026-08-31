# AutoAudit

[![CI](https://github.com/KenLP/autoaudit-bim/actions/workflows/ci.yml/badge.svg)](https://github.com/KenLP/autoaudit-bim/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Audit-grade BIM quality assurance.** Point it at a Revit or ACC model, and it
checks the model against rules you wrote in plain language, then proposes the
fix — as an ACC issue for a human, or as a parameter write that only lands after
someone approves it.

Every write is previewed before it happens, gated on an explicit approval, and
recorded. Nothing is changed behind your back.

```
your rule, in plain language
   → compiled to a YAML rule
   → checked against the model
   → dry-run preview → approval → write → audit trail
```

> **Coming from the Autodesk University session?**
> AutoAudit is the productised implementation of the BIM Orchestrator shown in
> that talk — same engine, same four-bucket outcomes, same trust pipeline. The
> narrative version, with the slides, is in
> [`docs/WHY_THIS_SOLUTION.md`](docs/WHY_THIS_SOLUTION.md).
> To see the loop run on your own machine, the demo below needs only Python,
> Git and `uv` — no Revit, no ACC account, no API key.

## Try it in 5 minutes

No Revit. No ACC account. No API key. Nothing to configure.

```bash
git clone https://github.com/KenLP/autoaudit-bim.git
cd autoaudit-bim/bim-orchestrator
uv sync --extra dev
uv run bim-orchestrator --demo --quiet
```

Requires [Python 3.12+](https://www.python.org/downloads/) and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Drop `--quiet` to
watch the structured log of every decision the engine makes.

You should see this:

```
=== Compliance outcomes ===
  Compliant:          45 / 52
  Non-Compliant:       5
  Manual Review:       0
  Missing Data:        2

--- Elements → ACC Issues ---
  Detected:            5 non-compliant + 2 missing-data elements
  ACC Issues created:  3  (1 issue per rule)
    ·   2 auto-fix proposal(s)  → review/approve in the Approvals tab
    ·   1 manual issue(s)       → Path A (someone fixes by hand)
  Revit auto-writes (no issue):  2 element(s)

Revit parameter writes:
  - element 705: Mark → 'D_105'
  - element 401: Department → 'General'
```

Read that as: of **52 checks**, 45 pass and **7 need attention** — 5 violations
plus 2 elements missing the data to decide. Of those 7, **2** had a value the
engine could compute and write on its own, **4** are parked in 2 approval-gated
proposals, and **1** became an issue for a human. Two auto-writes, four parked,
one raised: seven.

The model **and both backends** are simulated — a mock Revit and a mock ACC,
so no network call leaves your machine and no issue is filed anywhere. What is
not simulated is everything that decides: the rules engine, QC, the design
decisions, the approval gating and the report pipeline are the production code
path, and the run ends with a `verification_report.md` from the same renderer a
live audit uses — see
[a committed copy](docs/sample-output/verification_report.md) if you would
rather read one before installing anything.

Then open `config/rules.demo.yaml`, change a threshold, and run it again to see
the verdict change. That file is the whole point: **the rules are data, not
code.**

## What it actually does

A model gets checked, and every element lands in one of four buckets —
**compliant**, **non-compliant**, **needs human judgment**, or **missing the
data needed to decide**. That last bucket matters: a check that cannot see a
value says so, instead of quietly passing.

Each problem then takes one of two routes:

- **Path A — an ACC issue.** For anything needing human judgment, or any fix the
  engine cannot derive with certainty. It states what is wrong and why, and
  waits for a person.
- **Path B — a parameter write back into Revit.** Only for fixes with one
  deterministic answer. Even then the write is previewed, and unless the rule is
  trivially safe it is parked behind an approval: it becomes a proposal issue,
  a human moves it to *In progress*, and only then does the value land.

It never guesses a value into your model. Where no deterministic answer exists,
it raises an issue and says why.

Rules are written in a natural-language builder grounded in the real Revit
parameter catalog — it will not offer a parameter that does not exist on that
category, and it refuses read-only parameters as write targets.

## Where to go next

| You want to… | Read |
|---|---|
| Run it against a real Revit session or ACC project | [`bim-orchestrator/README.md`](bim-orchestrator/README.md) |
| See what kinds of rules it can express | [`docs/RULE_CAPABILITY_CATALOG.md`](docs/RULE_CAPABILITY_CATALOG.md) |
| Understand how it is built | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Understand *why* it is built this way | [`docs/WHY_THIS_SOLUTION.md`](docs/WHY_THIS_SOLUTION.md) |
| Install on a company machine, end to end | [`bim-orchestrator/docs/PILOT_INSTALL.md`](bim-orchestrator/docs/PILOT_INSTALL.md) |
| Deploy it without Node.js on the host | [`bim-orchestrator/docs/PRODUCTION_PACKAGING.md`](bim-orchestrator/docs/PRODUCTION_PACKAGING.md) |
| Run it unattended, nightly | [`bim-orchestrator/docs/SCHEDULED_AUDIT.md`](bim-orchestrator/docs/SCHEDULED_AUDIT.md) |
| Turn a code PDF into rules | [`bim-orchestrator/extraction-skills/README.md`](bim-orchestrator/extraction-skills/README.md) |

## Talking to real Revit and ACC

Two connections, neither of which needs Node.js on the host:

- **Revit** — over HTTP to a C# add-in,
  [`KenLP/RevitMCPServer`](https://github.com/KenLP/RevitMCPServer) (Revit
  2025–2027, MIT). Install its
  [latest release](https://github.com/KenLP/RevitMCPServer/releases/latest);
  **v0.8 or newer** is what this client expects, since it batches a set of
  parameter writes into a single undoable transaction. That release also ships
  an **AutoAudit ribbon tab and dockable panel** which loads this project's UI
  at `http://127.0.0.1:8601/ui/`, so the console runs inside Revit rather than
  in a separate browser window — see
  [`PILOT_INSTALL.md`](bim-orchestrator/docs/PILOT_INSTALL.md) §13.
- **ACC** — through
  [`acc-forma-mcp-server`](https://github.com/KenLP/acc-forma-mcp-server), which
  supplies the issues, the approval tokens and the tamper-evident audit log. Run
  it yourself as a single executable
  (`scripts/fetch-forma-mcp.ps1` downloads it), or point at the hosted service
  at <https://mcp.bimlynx.com> and skip hosting entirely.

Either side works on its own: `--run-revit --no-forma` audits a live model with
no ACC account, and the ACC path needs no Revit installed.

## Status

Pilot preview. The deterministic engine, the rule builder and the reporting are
complete and covered by a test suite that runs offline with no credentials —
`uv run pytest -q` in `bim-orchestrator/`. There is no installer and no code
signing yet, so treat a production rollout as a pilot.

Optional AI-assisted remediation exists as a separate private extension. Without
it — the default — the engine is fully deterministic, and the suite passes with
the extension absent. Drafting a rule from natural language calls the Anthropic
API and needs your own `ANTHROPIC_API_KEY`; nothing else here needs one.

## License

MIT — see [LICENSE](LICENSE).
