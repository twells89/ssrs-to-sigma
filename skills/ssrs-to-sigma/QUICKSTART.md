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

## Regression check after editing the parser or converter

The offline suite (stdlib `unittest`, no pytest) is the fast gate — it diffs
both parser goldens, checks section/header/footer retention, multi-section
concatenation, the empty-layout diagnostics, and the workbooks-as-code
workbook shape:

```bash
python3 -m unittest discover -s tests      # or: python3 tests/test_ssrs.py
```

Prefer a manual byte-diff of a single fixture? Both goldens diff the same way:

```bash
python3 scripts/parse_rdl.py fixtures/SalesByRegion.rdl -o /tmp/b.json
diff <(python3 -m json.tool /tmp/b.json) <(python3 -m json.tool fixtures/expected_bundle.json)

python3 scripts/parse_rdl.py fixtures/ReportSections2016.rdl -o /tmp/rs.json
diff <(python3 -m json.tool /tmp/rs.json) <(python3 -m json.tool fixtures/expected_reportsections_bundle.json)
```

`fixtures/SalesByRegion.rdl` is the flat 2008/2010 layout;
`fixtures/ReportSections2016.rdl` and `fixtures/MultiSection2016.rdl` exercise
the RDL 2016 `<ReportSections>` nesting (single- and multi-section).
