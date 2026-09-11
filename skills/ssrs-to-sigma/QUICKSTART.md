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
  --dm-name "Sales Reporting" --wb-name "Sales by Region" \
  --target workbook --out-prefix /tmp/sigma

cat /tmp/sigma_dm_spec.json /tmp/sigma_workbook_spec.json /tmp/conversion_report.md
```

You should see a `pivot-table` (the matrix Tablix), a `bar-chart`, three
controls, and a flag list noting the T-SQL/parameter work.

Omitting `--target` is identical to `--target workbook`. Use
`--target report` for fixed pages, or `--target auto` to partition each source
report by objective print/dashboard signals. Mixed auto bundles write both
`/tmp/sigma_workbook_spec.json` and `/tmp/sigma_report_spec.json` plus
`/tmp/sigma_target_resolution.json`.

The offline report `schemaVersion` default is `1`. For a live workflow, GET a
recent report spec as JSON, copy `document.schemaVersion`, and reconvert with
`--report-schema-version <current>` before verify/create:

```bash
curl -sf -H "Authorization: Bearer $SIGMA_API_TOKEN" \
  "$SIGMA_BASE_URL/v2/reports/<reference-report-id>/spec?format=json"
```

## Against a real estate

1. **Phase 0** — customer runs `scripts/export-ssrs.ps1` inside the firewall →
   `ssrs-export-*.zip`.
2. Set credentials:
   ```bash
   export SIGMA_BASE_URL="https://api.sigmacomputing.com"
   export SIGMA_CLIENT_ID="..." SIGMA_CLIENT_SECRET="..."   # or ~/.sigma-migration/env
   eval "$(scripts/get-token.sh)"
   ```
3. Install/load companion `sigma-authoring` (`sigma-data-models` and
   `sigma-workbooks`); load `sigma-reports` for report output.
4. Walk Phases 1 → 6 in `SKILL.md`: reuse-check first; DM POST/readback;
   layout last and readback preservation; numeric/visual parity; RLS/CLS
   detection and denied-user tests.

Workbooks as Code and Reports as Code are private beta. Reports additionally
require **Create, edit, and publish reports** permission and currently have no
DELETE endpoint. Verify is therefore the default:

```bash
# Authenticated server verification, no persistent mutation:
python3 scripts/publish.py --spec /tmp/sigma_workbook_spec.json \
  --out-dir /tmp/publish

# Only after explicit approval:
python3 scripts/publish.py --spec /tmp/sigma_report_spec.json \
  --out-dir /tmp/publish-report --create \
  --pdf-out /tmp/publish-report/report.pdf
```

The helper saves verify/create responses and JSON readback, and compares the
complete normalized document (including sources, columns, formulas, filters,
panels, and layout). A PASS is structural only; it is not live parity proof.

## Regression check after editing the parser or converter

The offline suite (stdlib `unittest`, no pytest) is the fast gate — it diffs
parser goldens, normalized physical metadata, auto routing, section/header/
footer retention, multi-section concatenation, empty-layout diagnostics,
canonical workbook shape, absolute report placement, and mocked publish flows:

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
the RDL 2016 `<ReportSections>` nesting (single- and multi-section);
`fixtures/PrintLayout2016.rdl` exercises page size, margins, panels, List/page
break/subreport signals, nested coordinates, and report auto-selection.
