#!/usr/bin/env bash
# Fetch the spec-PDF extraction fixtures (gitignored). See README.md.
set -euo pipefail
cd "$(dirname "$0")"

fetch() { # url  outfile
  echo "== $2 =="
  curl -fSL -A "Mozilla/5.0" -o "$2" "$1" \
    -w "   HTTP %{http_code} | %{size_download} bytes | %{content_type}\n"
}

fetch "https://www.ada.gov/assets/pdfs/2010-design-standards.pdf" ada_2010_standards.pdf
fetch "https://www.nyc.gov/assets/buildings/codes-pdf/cons_codes_2022/2022BC_Chapter10_EgressWBwm.pdf" nyc_bc2022_ch10_egress.pdf
fetch "https://www.cfm.va.gov/til/bim/BIM-Manual.pdf" va_bim_manual.pdf

echo "Done. Verify each starts with %PDF-:"
for f in *.pdf; do printf "  %s: %s\n" "$f" "$(head -c 5 "$f")"; done
