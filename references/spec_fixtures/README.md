# Spec-PDF fixtures for the PDF→rules extractor

Free / public-domain regulation & BEP PDFs used as **demo inputs** for
`POST /api/extraction/pdf` (AutoAudit UI → Rules → Extract from PDF) and the
`rules_extractor` sibling package. The PDFs themselves are **gitignored**
(large third-party binaries) — run `fetch.sh` to download them.

```bash
cd references/spec_fixtures && bash fetch.sh
```

Links verified live.

| File | Source | License | Yields (examples) |
|---|---|---|---|
| `ada_2010_standards.pdf` | [ada.gov 2010 Standards](https://www.ada.gov/assets/pdfs/2010-design-standards.pdf) | US federal → **public domain** | door clear width ≥ 32 in (§404.2.3), walking-surface width ≥ 36 in (§403.5.1), reach-range tables (§308) |
| `nyc_bc2022_ch10_egress.pdf` | [NYC BC 2022 Ch.10 Egress](https://www.nyc.gov/assets/buildings/codes-pdf/cons_codes_2022/2022BC_Chapter10_EgressWBwm.pdf) | NYC official code (free) | door width (BC 1010.1.1), riser/tread (BC 1011.5.2), corridor fire-rating (Table 1020.1) — IBC-derived proxy (IBC itself has no free PDF) |
| `va_bim_manual.pdf` | [VA BIM Manual](https://www.cfm.va.gov/til/bim/BIM-Manual.pdf) | US federal → **public domain** | the BEP/owner-requirement type: LOD per phase, file/model naming convention, object parameter requirements |

Other researched-and-verified sources (not fetched by default — add if needed):
BIMForum LOD Spec 2024 (CC BY-NC-ND — mind redistribution), QCVN 06:2022/BXD
(Vietnamese fire safety — tests multilingual extraction), GSA BIM Guides,
USACE M3 (xlsx), NBIMS §5.2, UK BIM Framework, Singapore BCA Essential Guides.
IBC / California / NFPA 101 / ISO 19650 have **no free-legal PDF** (view-online
or paywall only) — do not use scraped scans as fixtures for a product that ships.

## Note on running the demo

The extractor sends section text to an LLM. Two providers:

* **Cloud (default, recommended)** — Claude Sonnet. Needs a funded
  `ANTHROPIC_API_KEY` in the service's environment (the service loads `.env` on
  start, v1.7-3bP0.1). No credit → the endpoint returns a **502** with the real
  upstream message (not a misleading "no rules"). ~$1.7–1.8 per document after
  prompt caching (v1.7-3bP0.2).
* **Local / air-gapped** (v1.7-3bP0.2) — set `BIM_LLM_PROVIDER=ollama`
  (+ `ollama serve && ollama pull qwen3:14b`). Nothing leaves the host. Note:
  local models are lower-recall on regulation prose — they skip more rules than
  cloud Sonnet; use for data-residency pilots, not to save a couple dollars.

Building-code *prose* (ADA/NYC) also legitimately yields fewer directly
executable rules than a structured BEP: many clauses carry an occupancy/egress
applicability condition the extractor marks `not_model_checkable`
(→ review report, not YAML). The VA BIM Manual (owner requirements) is the
closest to the product's real input shape.
