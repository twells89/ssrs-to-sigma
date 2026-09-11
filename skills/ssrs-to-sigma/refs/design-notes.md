# Design notes — architecture, decisions, roadmap

## Status (be honest about this)

**Built from research; structurally validated against the bundled fixture; NOT
yet live-validated end-to-end against a real SSRS server or a live Sigma POST.**

What that means concretely:
- `parse_rdl.py` + `ssrs_expr.py` + `convert.py` + `scan_gaps.py` run clean on
  the fixtures and produce structurally checked Sigma DM + workbook/report
  specs that follow `sigma-data-models`, `sigma-workbooks`, and
  `sigma-reports`.
- They have **not** been POSTed to a live Sigma org, and parity has not been run
  against rendered SSRS output. The Phase 3/4 post-and-readback gate and Phase 6
  parity gate are scaffolded and documented — run them on a real engagement and
  fix any `type: error` columns the readback surfaces.

Don't claim parity until `verify_parity.py` is GREEN against numbers taken from
SSRS itself. (Same rule as every sibling converter.)

## Pipeline

```
Phase 0   export-ssrs.ps1 (customer, inside firewall) → ssrs-export-*.zip
Phase 0a  scan_gaps.py    → gap_report.md (AUTO/HINT/MANUAL/UNHANDLED shortlist)
Phase 1   parse_rdl.py    → bundle.json
Phase 1.5 (reuse) DM-reuse check — does an existing Sigma DM already cover this
                  warehouse table? (warehouse FQN + column overlap)
Phase 2   convert.py      → sigma_dm_spec.json, selected workbook/report spec(s),
                            target resolution (auto), parity keys, flags
Phase 3   POST DM spec → read back element ids → scan for type:error (hard gate)
Phase 4   re-emit with ids → publish.py verify (default) → explicit --create,
          readback/layout preservation, optional report PDF
Phase 5/6 parity + source/Sigma RLS/CLS tests (hard gates)
```

## Key decisions

### One Custom-SQL element per dataset
Most RDL datasets are raw SQL (`<CommandText>`), so the majority path is a
`source.kind: "sql"` data-model element carrying the statement. Plain DB fields
become DM columns referencing `[Custom SQL/<COL>]` (Snowflake returns unquoted
names UPPERCASE — `--no-uppercase` to disable). Calculated/aggregate dataset
fields are **not** DM columns; they surface in the workbook layer as pivot
values / chart measures, translated by `ssrs_expr`.

### SQL is preserved verbatim — never auto-translated
`convert.py` does **not** rewrite SQL across warehouse dialects and does **not**
strip/substitute T-SQL `@parameters`. Both are surfaced as flags. Rationale:
- T-SQL → Snowflake/BigQuery is a genuine dialect translation (GETDATE, ISNULL,
  TOP, `dbo.`, BETWEEN…AND, multi-value `IN (@p)`), not a regex job.
- Naive WHERE-clause editing breaks on `BETWEEN x AND y` (literal ` AND `).
- A silently-broken SQL rewrite is worse than an explicit "translate this."

So the emitted DM spec is **structurally** complete but the SQL needs a
dialect/param pass before it will compile against the target warehouse. That's
expected and documented — fix it, then run the Phase 3 gate.

### Parameters → controls, filter wiring is manual
Each safely resolvable `<ReportParameter>` becomes a page control (date / list /
number / checkbox). The actual *filtering* (the `WHERE … @param` in the SSRS
SQL) must be re-expressed as a Sigma element filter or a control-bound formula
— flagged, not auto-wired, because it depends on how you resolved the SQL above.

List value sources have a stricter gate. A query-backed list binds only to the
dataset named by `validValuesQuery.dataSet` and the exact
`validValuesQuery.valueField`, and only when that dataset produced a source
context. Static valid values are not converted into unrestricted data-driven
choices: until a current literal-source contract is proven, the control is
flagged and omitted.

### Workbook architecture (per-dataset dependency pattern)
Per report → one page with one **base table per converted SSRS dataset**
(`source.kind: data-model`). Each pivot/table/chart then sources the base table
named by its own `dataSetName`; a missing or ambiguous dependency is flagged and
the item is omitted rather than guessed. Downstream elements reference base
columns with the `[<BaseName>/<Col>]` prefix. All base tables, safe controls,
and visuals are included in the flat element collection and layout exactly
once.

The emitted spec is the **current workbooks-as-code shape**:
`{name, folderId, document}` with `document.kind: workbook`, a single **flat**
`document.elements` array (elements are workbook-global, not nested in pages),
metadata-only `document.pages`, and a `document.layout` XML string that places
every element. `convert.py` validates ids/placement locally before writing.
The layout is a **stacked, full-width 24-column starter grid** — every element
placed once, one row band each — not a reproduction of the RDL's pixel geometry
(the parsed `position` boxes are still ignored; mapping `<Top>/<Left>/<Width>/
<Height>` onto the grid is a future enhancement). The DM spec is unchanged:
data-model code-rep keeps `pages[].elements`; only the workbook surface moved to
the `document` wrapper.

Workbook emission is routed through local `scripts/lib/code_rep.py`, based on
the shared migration adapter. It owns document wrapping, flat-element
normalization, current theme placement, alignment aliases, and layout tag
aliases. The data-model emitter never calls it.

### Workbook or report target

Omitted `--target` remains `workbook`. `report` forces fixed-layout output.
`auto` resolves each source report and partitions mixed bundles. The resolver
scores parsed evidence: page breaks, Lists, subreports, report-section count,
physical page settings, margins, headers/footers versus charts and interactive
parameters. It writes scores/reasons so the choice is auditable.

A Sigma report has a separate contract and endpoint. Its document has
`kind: report`, document-wide pixel config, flat elements, metadata-only
pages/panels, and absolute leaf placement. Report sections map to pages and
header/footer regions map to report panels. Known RDL boxes are converted at
96 px/in and each emitted element is placed once. Hidden dependency pages hold
all dataset base tables and safely resolved controls. Unsupported or unsafe
content is flagged and omitted; it is never disguised as a working table.

The report schema version defaults to `1` so offline fixtures remain
deterministic. It is not a live-version claim. Before a live build, GET a recent
report spec with `?format=json` and pass its `document.schemaVersion` through
`--report-schema-version`.
The local validator enforces the 1,000-page maximum across visible and hidden
pages and fails clearly rather than partitioning.

### Transactional conversion output

`convert.py` parses, builds, and validates the complete selected output set
before touching an existing result. It writes and fsyncs temporary files beside
their destinations, atomically replaces desired outputs, and removes obsolete
target artifacts only after successful replacement. Existing affected files
are backed up for rollback if the commit phase encounters an I/O failure. A
parse/build/validation failure therefore leaves the previous valid files
unchanged.

### RDL layout nesting (2008/2010 vs 2016+)
`parse_rdl._iter_layouts()` resolves both the flat root-level `<Body>`/`<Page>`
and the RDL 2016 `<ReportSections><ReportSection>` nesting, concatenating items
across every section. This is a structural axis independent of the namespace
one, so namespace stripping alone does not cover it. A layout that parses to
zero visuals while datasets/parameters exist emits a `warnings` entry and is
scored MANUAL — the previous behavior silently lost every visual and scored the
report AUTO. Multi-section reports currently flatten into one workbook page;
the report target maps sections to separate fixed pages.

## Hard problems / known gaps

- **Stored-proc datasets** — `EXEC sp_x @p=1` doesn't run in Sigma's warehouse
  dialects. Inline the proc body as Custom SQL only if it's small and the
  customer hands it over; else escalate. Flagged.
- **Pixel-perfect paginated layouts** — the report target preserves parsed
  boxes, physical page config, and header/footer panels, but cannot guarantee
  pagination, typography, dynamic text, or subreport behavior. It remains a
  flagged draft until verify, readback, PDF inspection, and parity pass.
- **`<Code>` VB blocks** (`=Code.Foo(...)`) — no analog; per-block manual.
- **Multi-value parameter → SQL join** (`=Join(Parameters!X.Value, ",")`) —
  becomes a Sigma `list` control; the chart/filter uses the control reference.
- **Mixed-grain Tablix** — row+column groups AND a detail band = hybrid
  table+pivot; hand-decompose into one or the other.
- **Window/running aggregates** (`RunningValue`, `Previous`) — Sigma window
  functions silently error in DM element calc columns; use workbook
  grouping/window context or Custom SQL.

## Reuse from the sibling converters

This skill deliberately leans on the shared migration toolkit rather than
re-implementing:
- **Canonical spec shapes** → defer to `sigma-data-models` (`reference/sources.md`,
  `calc-columns.md`), `sigma-workbooks` (`reference/specification/*`), and
  `sigma-reports`.
- **DM-reuse check (Phase 1.5)**, **post-and-readback gate**, **layout helpers**,
  **gap-scout subagent** → same patterns as `tableau-to-sigma` /
  `cognos-to-sigma`.

## Roadmap / open questions

- **Customer corpus** — calibrate the gap scanner against ~5 real-world RDLs of
  varying complexity. The bundled fixture is synthetic.
- **Auto SQL-dialect translation** — a guarded T-SQL→ANSI/Snowflake pass for the
  common constructs (GETDATE/ISNULL/TOP/`dbo.`), still flagging anything
  uncertain.
- **Subreports → drillthrough** once a Sigma drill/action primitive is wired.
- **RLS** — SSRS has no native row security at the report level (it relies on
  data-source security / `User!UserID` filters). If reports filter on
  `User!UserID`, that's the RLS-port surface → Sigma user attributes. Ask the
  customer explicitly; never assume an estate has no security just because the
  RDL doesn't carry it.
- **CLS** — RDL only shows fields the author could use. Inventory hidden source
  fields and role grants separately, preserve warehouse/Sigma CLS, and test
  representative allowed and denied identities after DM readback.

`publish.py` defaults to `/spec/verify`; `--create` is the explicit persistent
write boundary. Workbooks as Code and Reports as Code are private beta, reports
require **Create, edit, and publish reports** permission, and reports currently
have no DELETE endpoint. GET readback requests JSON explicitly and compares the
complete normalized submitted/current document; only response-envelope
metadata and benign layout XML whitespace are ignored. Saved verify/create/
readback files and optional PDF are evidence inputs only. This repository has
not established live proof.
