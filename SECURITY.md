# Security

## Reporting a vulnerability

Use **GitHub private vulnerability reporting** on
[`KenLP/autoaudit-bim`](https://github.com/KenLP/autoaudit-bim): the **Security**
tab → **Report a vulnerability**. That keeps the report private until there is a
fix.

Please do **not** open a public issue for a security problem.

Include what you ran, what happened, and what you expected. A `--demo` repro is
ideal — it needs no Revit, no ACC and no API key.

Response is **best-effort**. This is a pilot preview maintained by one person —
there is no SLA, and no bounty.

## Supported versions

Only the **latest commit on `main`**. There is no version support matrix and no
backporting; this is a pilot preview, not a supported release line. If you are
on an older snapshot, the fix is to update.

## Security posture

This project sells an audit-grade claim, so here are the limits it ships with,
stated plainly rather than left to be discovered.

**The AuditHub service has no authentication.** It binds `127.0.0.1:8601` and
trusts every caller on the machine. Anyone who can reach that port can start an
audit and drive the approval pass. There is one browser-vector guard — an
unsafe-method request carrying a non-local `Origin` is rejected — but that is
CSRF hardening, not auth: `curl` on the same machine is unrestricted. **Never
expose port 8601 to a LAN or to the internet**, and do not put it behind a
reverse proxy expecting the proxy to authenticate for you.

**`forma-mcp.exe` is unsigned.** It is a build artifact of the sibling
`acc-forma-mcp-server`, fetched from a GitHub Release rather than committed.
`scripts/fetch-forma-mcp.ps1` verifies its SHA-256 against the digest the
release publishes, which catches a corrupted or intercepted download — it does
**not** prove the release was not tampered with at source, because the hash and
the file come from the same origin. To get a guarantee that survives a
compromised release, pin the hash out of band:
`scripts\fetch-forma-mcp.ps1 -ExpectedSha256 <hex>`. `-SkipHashCheck` disables
verification entirely and says so.

**Rule drafting calls out to the Anthropic API.** When you draft a rule from
natural language in the Rule Builder, the rule text you type is sent to
Anthropic using **your own** `ANTHROPIC_API_KEY`. Nothing else in the engine
does this — the checking, remediation, approval and reporting paths are
deterministic and run locally.

**`--demo` makes no network calls at all.** Both backends are mocked, no
credentials are read, and nothing leaves the machine.

## Known advisories we have not patched

**ChromaDB (2 critical, 2 high) — dismissed as unreachable, not fixed.** There
is no patched release; 1.5.9 is the latest. All four advisories describe server
mode: pre-authentication code injection, `SimpleRBACAuthorizationProvider`
scoping, and cross-tenant access by authenticated users. This project never
starts a Chroma server — `rag/store.py` uses `PersistentClient` and
`EphemeralClient` and nothing else, and there is no `HttpClient`, no auth
provider and no `CHROMA_SERVER` configuration anywhere in the tree. The
vulnerable code is not reachable, so the alerts are dismissed as *not used*
rather than left open forever.

That reasoning holds only while the usage stays embedded. **If you point this at
a Chroma server, the advisories apply to you and none of them are fixed.**

ChromaDB is only loaded on the RAG path. `--demo`, the rules engine, the
checks, the reports and the approval loop never touch it.

## Not in this release

- **Code signing.** No signed installer and no signed binaries, for this
  project or for `forma-mcp.exe`. Treat any production rollout as a pilot.
- **Authentication or multi-user access control** on the AuditHub service.
- **Transport security** between components — everything is loopback HTTP.
