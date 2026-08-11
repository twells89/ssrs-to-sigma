# Design notes — architecture, decisions, roadmap

## Status (be honest about this)

**Built from research; structurally validated against the bundled fixture; NOT
yet live-validated end-to-end against a real SSRS server or a live Sigma POST.**

What that means concretely:
- `parse_rdl.py` + `ssrs_expr.py` + `convert.py` + `scan_gaps.py` run clean on
  `fixtures/SalesByRegion.rdl` and produce well-formed Sigma DM + workbook specs
  that follow the canonical shapes in the `sigma-data-models` /
  `sigma-workbooks` skills.
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
Phase 2   convert.py      → sigma_dm_spec.json, sigma_workbook_spec.json,
                            parity_keys.json, conversion_report.md
Phase 3   POST DM spec → read back element ids → scan for type:error (hard gate)
Phase 4   convert.py --data-model-id --dm-element-ids → POST workbook
Phase 5/6 verify_parity.py → parity_report.md (hard gate)
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
Each `<ReportParameter>` becomes a page control (date / list / number /
checkbox). The actual *filtering* (the `WHERE … @param` in the SSRS SQL) must be
re-expressed as a Sigma element filter or a control-bound formula — flagged, not
auto-wired, because it depends on how you resolved the SQL above.

### Workbook architecture (the 5-element pattern)
Per report → one page: a **base table** sourcing the DM element
(`source.kind: data-model`), then pivot/table/chart elements sourcing the base
table by element id, plus controls. Downstream elements reference base columns
with the `[<BaseName>/<Col>]` prefix. This mirrors the sibling converters.

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

### RDL layout nesting (2008/2010 vs 2016+)
`parse_rdl._iter_layouts()` resolves both the flat root-level `<Body>`/`<Page>`
and the RDL 2016 `<ReportSections><ReportSection>` nesting, concatenating items
across every section. This is a structural axis independent of the namespace
one, so namespace stripping alone does not cover it. A layout that parses to
zero visuals while datasets/parameters exist emits a `warnings` entry and is
scored MANUAL — the previous behavior silently lost every visual and scored the
report AUTO. Multi-section reports currently flatten into one workbook page;
mapping sections → separate pages is a possible future refinement.

## Hard problems / known gaps

- **Stored-proc datasets** — `EXEC sp_x @p=1` doesn't run in Sigma's warehouse
  dialects. Inline the proc body as Custom SQL only if it's small and the
  customer hands it over; else escalate. Flagged.
- **Pixel-perfect paginated layouts** — page headers/footers, page breaks,
  banded subreport detail. These don't fit Sigma's grid dashboard model. Assess
  as "redesign," not "convert."
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
  `calc-columns.md`) and `sigma-workbooks` (`reference/specification/*`).
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
