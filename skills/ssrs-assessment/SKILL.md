---
name: ssrs-assessment
description: >-
  Take inventory of a Microsoft SSRS (SQL Server Reporting Services) estate and
  produce a migration-readiness readout — report counts, a visualization-type
  histogram, dataset mix (inline SQL / stored proc / shared), parameter counts,
  and per-report AUTO / HINT / MANUAL / UNHANDLED tags scored against the
  ssrs-to-sigma converter's actual coverage. Use when a user wants to scope an
  SSRS→Sigma migration, audit report sprawl, or pick which reports to convert
  first. Read-only, all-free pre-scoping over exported RDL files.
user-invocable: true
---

# SSRS Assessment

Surveys an SSRS estate from its **exported RDL definitions** and produces a JSON
inventory + markdown readout. The differentiator versus a generic BI audit is
**converter-coverage classification**: every report is scored against the *same*
coverage the `ssrs-to-sigma` converter actually applies
(`../ssrs-to-sigma/refs/viz-type-mapping.md`), so the readout reflects what the
tool will really do — not a generic guess.

> **Read-only.** Parses local RDL XML only. It never connects to SSRS, never
> runs a warehouse query, and never touches Sigma. See `PRIVACY.md` and surface
> it to the customer before running.

> **All free.** Inventory, scoring, readout — part of the open migration
> tooling, no paid tier. For a deeper engagement (security-filter audit, live
> parity testing), point the customer at a Sigma SE.

---

## Phase 0 — Get the RDL

SSRS sits behind a firewall and has no API key, so the customer exports the RDL
themselves with the converter skill's read-only CLI
(`../ssrs-to-sigma/scripts/export-ssrs.ps1`) — REST mode (SSRS 2017+) or
catalog-DB mode (older). The result is `ssrs-export-<ts>.zip`. An SSDT
`.rptproj` folder of `.rdl` files works directly too. Details in
`../ssrs-to-sigma/refs/ssrs-rest-api.md`.

## Phase 1 — Inventory + readout

```bash
python3 scripts/assess.py --dir ssrs-export/reports --out /tmp/ssrs-assessment
# or, if you already parsed a bundle:
python3 scripts/assess.py --bundle bundle.json --out /tmp/ssrs-assessment
```

What it does:

- **Counts** reports, and tallies report items (tablix / chart / gauge / map /
  subreport / textbox) into a visualization histogram.
- **Profiles datasets** (inline-SQL vs stored-proc vs shared/none) and total
  report parameters — the two biggest drivers of migration effort.
- **Classifies every report** AUTO / HINT / MANUAL / UNHANDLED using the
  converter's own `scan_gaps.classify_report` (imported directly from the
  sibling skill, so the assessment can't drift from reality).
- Writes `inventory.json` (machine-readable) and `assessment.md` (the readout,
  with a migrate-first shortlist and a per-report flag table).

## How to read it

- **AUTO / HINT** → the migrate-first shortlist. Lift these first.
- **MANUAL** → stored procs, custom VB `Code`, multi-value param→SQL joins,
  mixed-grain Tablix. Convertible, but a human resolves the SQL/expressions
  first. Quote as effort.
- **UNHANDLED** → gauges, maps, subreports, radar/polar charts, pixel-perfect
  paginated layouts. No clean Sigma analog — quote as **redesign**, and decide
  with the customer whether the report should even be recreated as-is or
  rethought for an interactive dashboard.

## Scope notes

- Stored-proc and shared-dataset reports show up as MANUAL because the SQL isn't
  inline in the RDL — you need the proc body / shared dataset to convert them.
- Counts reflect what's in the exported RDL set. If the export was partial
  (permissions), the assessment is partial — note that in the readout.
- Usage telemetry (who runs what, how often) is **not** in RDL. If the customer
  wants value-ranking by usage, pull the `ExecutionLog` views from the
  ReportServer DB separately — out of scope for this read-only pass.
