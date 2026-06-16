# ssrs-to-sigma

Claude Code plugin for migrating **Microsoft SSRS (SQL Server Reporting
Services)** — and Power BI Report Server — to **Sigma**, in the same format and
phase structure as the
[sigma-migration-skills](https://github.com/twells89/sigma-migration-skills)
converters (Tableau, Power BI, Qlik, ThoughtSpot, QuickSight, Cognos,
MicroStrategy). Built standalone so it can graduate into that marketplace's
`plugins/`.

## Status: built from research — structurally validated, not yet live-validated

The pipeline runs clean offline and produces well-formed Sigma data-model +
workbook specs from real RDL:

- `parse_rdl.py` parses the bundled fixture (`fixtures/SalesByRegion.rdl`) into a
  normalized `bundle.json`.
- `convert.py` turns that into a Sigma DM spec (Custom-SQL element) and a
  workbook spec — the matrix Tablix becomes a `pivot-table`, the column chart a
  `bar-chart`, the report parameters become controls — following the canonical
  shapes documented in the `sigma-data-models` / `sigma-workbooks` skills.

What it has **not** done yet: a live POST to a Sigma org, or a data-parity run
against rendered SSRS output. Phases 3–6 are scaffolded and documented as live
gates to run on a real engagement. **Don't claim parity until
`verify_parity.py` is GREEN against numbers taken from SSRS itself.** See
`skills/ssrs-to-sigma/refs/design-notes.md`.

## The defining constraint: the report server is behind a firewall

SSRS almost always sits inside the customer network, and **SSRS has no API-key
concept** — auth is Windows credentials. So Phase 0 is a small, auditable,
read-only **PowerShell exporter the customer runs themselves**
(`export-ssrs.ps1`), producing a zip of RDL you convert entirely offline. Two
acquisition modes (REST for SSRS 2017+, ReportServer-catalog-DB for older), plus
a no-server SSDT `.rptproj` path.

## What's in the box

| Skill | What it does |
|---|---|
| [`skills/ssrs-to-sigma`](skills/ssrs-to-sigma/SKILL.md) | The converter: customer export → `parse_rdl.py` (RDL XML → `bundle.json`) → `convert.py` (→ Sigma DM + workbook specs) → POST + readback gate → `verify_parity.py` (hard parity gate). Plus `scan_gaps.py` (coverage shortlist) and `ssrs_expr.py` (SSRS VB → Sigma formula translation). |
| [`skills/ssrs-assessment`](skills/ssrs-assessment/SKILL.md) | Read-only estate inventory + readout: report counts, visualization histogram, dataset/parameter mix, and per-report AUTO / HINT / MANUAL / UNHANDLED tags scored against the converter's *actual* coverage (it imports the converter's own classifier, so it can't drift). |

The hard-won knowledge lives in `skills/ssrs-to-sigma/refs/`: `rdl-format.md`
(the RDL element tree and what the parser extracts), `expression-mapping.md`
(SSRS VB → Sigma formula rules + what's flagged), `ssrs-rest-api.md` (firewall
export, the no-API-key reality, HTTP 503 troubleshooting), `viz-type-mapping.md`
(coverage table), `design-notes.md` (architecture + hard problems + roadmap).

## Quick start

```bash
# Offline demo on the bundled fixture (no SSRS / Sigma needed):
cd skills/ssrs-to-sigma
python3 scripts/parse_rdl.py fixtures/SalesByRegion.rdl -o /tmp/bundle.json
python3 scripts/scan_gaps.py --bundle /tmp/bundle.json -o /tmp/gap.md
python3 scripts/convert.py --bundle /tmp/bundle.json \
  --connection-id CONN --folder-id FOLDER --out-prefix /tmp/sigma
# -> /tmp/sigma_dm_spec.json, /tmp/sigma_workbook_spec.json, /tmp/conversion_report.md

# Assess an estate (read-only) from exported RDL:
python3 skills/ssrs-assessment/scripts/assess.py --dir ssrs-export/reports --out /tmp/ssrs-assessment
```

Or install as a Claude Code plugin and just ask: *"migrate my SSRS reports to
Sigma"* / *"assess my SSRS estate."*

## Design contract

Core principle (shared with every sibling converter): **flag, never fake.**
Anything without a clean Sigma analog — stored-proc datasets, custom VB `Code`,
gauges/maps/subreports, T-SQL dialect, pixel-perfect paginated layouts — is
surfaced as a loud flag (`conversion_report.md`) with a readable fallback,
never silently mis-converted. Dataset SQL is preserved **verbatim**: the
converter does not gamble on a cross-dialect SQL rewrite. Parity is a hard gate:
a migration is green only when `verify_parity.py` passes against numbers taken
from SSRS itself.

## Graduating into sigma-migration-skills

Already in the marketplace plugin layout (`.claude-plugin/plugin.json` +
`skills/<name>/SKILL.md|scripts|refs|fixtures`). To graduate: drop the repo
content into `sigma-migration-skills/plugins/ssrs-to-sigma/` — no path changes
needed (script cross-references are relative; `ssrs-assessment` imports the
converter scripts via a relative sibling path).

## License

MIT
