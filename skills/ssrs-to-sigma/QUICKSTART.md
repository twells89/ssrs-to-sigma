# Quickstart

End-to-end on the bundled fixture (no Sigma org or SSRS server needed —
proves the offline half of the pipeline):

```bash
cd skills/ssrs-to-sigma

# Phase 1 — parse RDL
python3 scripts/parse_rdl.py fixtures/SalesByRegion.rdl -o /tmp/bundle.json

# Phase 0a — gap scan
python3 scripts/scan_gaps.py --bundle /tmp/bundle.json -o /tmp/gap.md

# Phase 2 — convert to Sigma specs (placeholder ids/conn)
python3 scripts/convert.py --bundle /tmp/bundle.json \
  --connection-id CONN --folder-id FOLDER \
  --dm-name "Sales Reporting" --wb-name "Sales by Region" --out-prefix /tmp/sigma

cat /tmp/sigma_dm_spec.json /tmp/sigma_workbook_spec.json /tmp/conversion_report.md
```

You should see a `pivot-table` (the matrix Tablix), a `bar-chart`, three
controls, and a flag list noting the T-SQL/parameter work.

## Against a real estate

1. **Phase 0** — customer runs `scripts/export-ssrs.ps1` inside the firewall →
   `ssrs-export-*.zip`.
2. Set credentials:
   ```bash
   export SIGMA_BASE_URL="https://api.sigmacomputing.com"
   export SIGMA_CLIENT_ID="..." SIGMA_CLIENT_SECRET="..."   # or ~/.sigma-migration/env
   eval "$(scripts/get-token.sh)"
   ```
3. Walk Phases 1 → 6 in `SKILL.md`. The hard gates are Phase 3 (`type: error`
   scan on DM readback) and Phase 5/6 (`verify_parity.py` GREEN).

## Regression check after editing the parser

```bash
python3 scripts/parse_rdl.py fixtures/SalesByRegion.rdl -o /tmp/b.json
diff <(python3 -m json.tool /tmp/b.json) <(python3 -m json.tool fixtures/expected_bundle.json)
```
