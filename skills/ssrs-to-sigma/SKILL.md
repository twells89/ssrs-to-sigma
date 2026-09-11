---
name: ssrs-to-sigma
description: >-
  Migrate Microsoft SSRS (SQL Server Reporting Services) reports to Sigma. Use
  when the user has SSRS / Power BI Report Server reports — .rdl or .rdlc files,
  an SSDT .rptproj project, or a report server behind a firewall — and wants to
  recreate them in Sigma. Provides a customer-runnable export step, RDL XML
  parsing into a bundle, SSRS-expression translation, target selection, and
  conversion to a Sigma data model plus workbook and/or fixed-layout report
  (Tablix matrix→pivot, table→table, charts, safe parameters→controls), with a
  parity-verification scaffold. Translates what maps cleanly
  and flags what doesn't (stored procs, custom VB, gauges/maps/subreports,
  paginated layouts) instead of emitting wrong logic.
user-invocable: true
---

# SSRS → Sigma migration

Convert SSRS **RDL** report definitions into a Sigma **data model** plus a
responsive **workbook**, a fixed-layout **report**, or both for a mixed bundle.
Parse the RDL XML, translate datasets / expressions / parameters / Tablix /
charts, emit the specs, then **verify parity** against numbers rendered from
SSRS itself. Translate what maps cleanly; **flag what doesn't** (stored procs,
custom VB `Code`, gauges/maps/subreports, and unproven pagination/dynamic
layout behavior) — never emit confidently-wrong logic.

> **Status — read this first.** Built from research and **structurally
> validated** against bundled fixtures (DM, workbook, and report JSON shape,
> bounds, and placement are checked offline). It has **not** yet been
> POSTed to a live Sigma org or parity-checked against rendered SSRS output.
> Treat Phases 3–6 as live gates to run on a real engagement, not as
> pre-proven. Do not claim parity until `verify_parity.py` is GREEN. See
> `refs/design-notes.md`.

> Read `refs/` before relying on shapes: `rdl-format.md` (the RDL element tree
> and what the parser extracts), `expression-mapping.md` (SSRS VB → Sigma
> formula rules + what gets flagged), `ssrs-rest-api.md` (firewall export, the
> no-API-key reality, 503 troubleshooting), `viz-type-mapping.md` (coverage
> table), `design-notes.md` (architecture + hard problems + roadmap). For
> canonical Sigma spec shapes, install the companion `sigma-authoring` plugin
> and defer to `sigma-data-models` / `sigma-workbooks`; install and read
> `sigma-reports` before using the report target.

---

## Prerequisites

- **The RDL.** Either the customer's `ssrs-export-*.zip` (Phase 0), a folder of
  `.rdl`/`.rdlc` files, or an SSDT `.rptproj` project folder.
- **Sigma API token** — `eval "$(scripts/get-token.sh)"` (uses
  `SIGMA_CLIENT_ID` / `SIGMA_CLIENT_SECRET` / `SIGMA_BASE_URL`, or
  `~/.sigma-migration/env`).
- **A Sigma connection to the same warehouse SSRS queried.** Parity only means
  something when Sigma reads the database the SSRS reports read. You'll need the
  connection id, target database, and a destination folder id.
- **Python 3** (stdlib only). The customer-side exporter is PowerShell
  (`export-ssrs.ps1`) — Windows / PowerShell Core.
- **Companion authoring skills:** `sigma-authoring` (`sigma-data-models` and
  `sigma-workbooks`), plus `sigma-reports` for fixed-layout output.
- **Private-beta access:** Workbooks as Code and Reports as Code are
  entitlement-gated. Report creation also requires **Create, edit, and publish
  reports** permission. A valid API token does not imply either entitlement.

## Phase 0 — Export the RDL (customer, inside the firewall)

SSRS report servers almost always sit behind a firewall, and SSRS has **no
API-key concept** — auth is Windows credentials. So the customer runs the
read-only exporter themselves and hands back a zip:

```powershell
# SSRS 2017+ / Power BI Report Server (REST):
.\export-ssrs.ps1 -ReportServerUrl http://localhost/reports
# older SSRS, or HTTP blocked (reads the ReportServer catalog DB directly):
.\export-ssrs.ps1 -SqlServer sql01 -Database ReportServer
```

It emits `ssrs-export-<ts>.zip` with `reports/**.rdl`, `catalog.json`,
`metadata.json` (and optionally rendered `expected/*.csv` for data parity).
No server? An SSDT `.rptproj` folder of `.rdl` files works directly. REST 503s
and named-instance URL gotchas are in `refs/ssrs-rest-api.md`.

## Phase 0a — Gap scan (converter-coverage shortlist)

```bash
python3 scripts/scan_gaps.py --dir ssrs-export/reports -o gap_report.md
```

Classifies every report **AUTO / HINT / MANUAL / UNHANDLED** against the
converter's real coverage (`refs/viz-type-mapping.md`). Migrate AUTO/HINT first;
quote MANUAL/UNHANDLED (stored procs, custom VB, gauges/maps/subreports) as
design work. The sibling `ssrs-assessment` skill produces the estate-wide
version of this.

## Phase 1 — Parse RDL → bundle.json

```bash
python3 scripts/parse_rdl.py --dir ssrs-export/reports -o bundle.json
# or a single report:  python3 scripts/parse_rdl.py Report.rdl -o bundle.json
```

Namespace-agnostic RDL parse → datasources, datasets (SQL / proc + query
params + fields), report parameters, and a Tablix/chart/gauge/map/subreport
inventory per report. Handles **both** the flat 2008/2010 `<Body>`/`<Page>`
layout **and** the RDL 2016+ `<ReportSections><ReportSection>` nesting (items
concatenated across sections). The additive normalized `layout` block records
report/body/page dimensions, margins, header/footer heights, section count,
page breaks, Lists, subreports, and every item's RDL box. Existing consumers
can continue reading the prior fields — see `refs/rdl-format.md`. A report whose layout
parses to zero visuals while it still has datasets/parameters is flagged as a
likely structural miss, never a clean empty success.

`bundle.json` is the converter contract. Regression-check the parser against
both goldens with the offline suite (stdlib, no pytest):

```bash
python3 -m unittest discover -s tests    # or: python3 tests/test_ssrs.py
```

## Phase 1.5 — Reuse an existing data model (recommended)

Before building a new DM, check whether the org already has one over the same
warehouse table (warehouse FQN + column overlap), exactly as `tableau-to-sigma`
does. Reuse beats re-deriving — it inherits joins, filters, and CLS. If none is
relevant, build fresh below.

## Phase 2 — Convert → Sigma specs

```bash
python3 scripts/convert.py --bundle bundle.json \
  --connection-id <SIGMA_CONNECTION_ID> --folder-id <FOLDER_ID> \
  --dm-name "SSRS Migration" --wb-name "SSRS Migration" \
  [--target workbook|report|auto] [--report-name "Printable SSRS"] \
  [--report-schema-version <CURRENT_VERSION>] \
  [--no-uppercase]      # default assumes Snowflake (UPPERCASE Custom SQL cols)
```

Omitting `--target` remains `workbook`. Explicit `workbook` sends every source
report to responsive workbook pages; explicit `report` sends every source
report to fixed pages. `auto` scores objective RDL signals per source report:
page breaks, Lists, subreports, multiple sections, physical page settings,
headers/footers/margins versus charts and interactive parameters. A mixed
bundle emits both `_workbook_spec.json` and `_report_spec.json`, grouped by
resolved target, plus `_target_resolution.json` with scores and reasons.
Literal page breaks with `Disabled=true` do not score; dynamic `Disabled`
expressions remain potential print signals and are flagged for manual review.
The converter builds and validates every selected spec before touching output,
stages all files beside their destinations, atomically replaces the successful
set, and only then removes obsolete workbook/report/resolution artifacts for
the same `--out-prefix`. A failed rerun preserves the previous valid output
set, so changing target mode cannot leave a stale publishable spec behind.
`--report-schema-version` defaults to `1` only for offline compatibility.
Before a live report verify/create, GET a recent report representation with
`?format=json`, read its `document.schemaVersion`, and pass that current value;
do not assume the offline default matches the target organization.

Emits `sigma_dm_spec.json` (one Custom-SQL element per dataset),
the selected `_workbook_spec.json` and/or `_report_spec.json`,
`parity_keys.json`, and **`conversion_report.md`** — the flag list.

`sigma_workbook_spec.json` is the **current workbooks-as-code** shape:
`{name, folderId, document}` where `document` has `kind: workbook`, a **flat**
`elements` array (one base table per converted SSRS dataset, plus
pivot/table/chart elements and safe controls), a
metadata-only `pages` array, and a `layout` XML string that places every
element exactly once on a 24-column grid. (The data-model spec keeps its
`pages[].elements` nesting — only the *workbook* surface changed.) The
converter runs a local structural check (unique ids, every element placed, no
dangling layout reference) before writing.

The report spec follows the separate `sigma-reports` contract:
`{name, folderId, document}` with `kind: report`, document-wide pixel config,
flat elements, metadata-only pages/panels, and absolute
`x`/`y`/`width`/`height` XML. It never uses workbook grid syntax. RDL
header/footer items become report panels; sections become pages; a hidden
dependency page holds every required dataset base table and safe parameter
control. Tablix and chart elements bind only to their declared `dataSetName`;
missing or ambiguous dataset dependencies cause an explicit flag and omission,
never a fallback to an unrelated source. Query-backed list controls use only
their declared dataset and exact value field. Static valid-value lists are
flagged and omitted until a supported literal-source shape is proven. Missing
geometry and unsupported report items are flagged; subreports, gauges, maps,
dynamic text, and chart kinds outside the conservative report baseline are
omitted, not replaced with fake parity. Local validation rejects more than
1,000 total report pages, including hidden dependency pages; oversized output
is not partitioned.

**Read `conversion_report.md` before POSTing.** The converter preserves dataset
SQL **verbatim** — it does not translate T-SQL dialect or rewrite `@parameters`
(see `refs/design-notes.md` for why). Those are flagged; resolve them so the SQL
compiles against your warehouse, and wire parameters to controls/filters,
before the Phase 3 gate. Workbook and report specs carry `{{DATA_MODEL_ID}}` /
element-id placeholders until Phase 4.

## Phase 3 — POST the data model + read back ids (hard gate)

```bash
eval "$(scripts/get-token.sh)"
curl -s -X POST "$SIGMA_BASE_URL/v2/dataModels/spec" \
  -H "Authorization: Bearer $SIGMA_API_TOKEN" -H "Content-Type: application/json" \
  -d @sigma_dm_spec.json            # -> dataModelId
curl -s "$SIGMA_BASE_URL/v2/dataModels/<dataModelId>/spec" \
  -H "Authorization: Bearer $SIGMA_API_TOKEN" > dm_readback.yaml
```

The readback is **YAML with server-reassigned element ids** — capture them as
`dm_element_ids.json` (`{"<element name>": "<server id>"}`). **Gate:** scan the
readback for any column with `type: error` — a spec can POST 200 yet carry SQL
or formulas that don't resolve at query time (a verbatim T-SQL statement that
won't run is the most likely cause here). Do not proceed on errors;
`mcp__sigma-data-model__diagnose_sigma_save_error` and the `sigma-data-models`
skill are the debug path.

## Phase 4 — Re-emit with real ids, verify, then explicitly create

```bash
python3 scripts/convert.py --bundle bundle.json \
  --connection-id <id> --folder-id <FOLDER_ID> \
  --target auto --data-model-id <dataModelId> \
  --dm-element-ids dm_element_ids.json \
  --report-schema-version <CURRENT_REPORT_SCHEMA_VERSION>

# Authenticated server verify only; this is the non-persistent default.
python3 scripts/publish.py --spec sigma_workbook_spec.json --out-dir publish/wb
python3 scripts/publish.py --spec sigma_report_spec.json --out-dir publish/report

# Persistent POST requires the explicit --create switch:
python3 scripts/publish.py --spec sigma_workbook_spec.json \
  --out-dir publish/wb --create
python3 scripts/publish.py --spec sigma_report_spec.json \
  --out-dir publish/report --create --pdf-out publish/report/render.pdf
```

`publish.py` uses only the stdlib, loads credentials from the environment or
`~/.sigma-migration/env`, rejects unsafe API origins, saves verify/create
responses and readback, and checks element/layout coverage where parseable.
Basic and bearer requests reject redirects so credentials cannot cross origins;
report PDF polling treats bounded 404/204/processing responses as not-ready.
Workbook/report GET readback requests JSON explicitly (`?format=json`) and the
gate compares the complete normalized document; material source, column,
formula, filter, panel, or layout changes fail. Only response-envelope metadata
and semantically identical layout XML whitespace are ignored.
Report PDF export is available only after `--create`; inspect it manually.
API success is not visual or data parity.

> **Both code-representation surfaces are private beta.**
> `POST /v2/workbooks/spec`
> (and its `/verify` sibling) is entitlement-gated — confirm the workspace has
> it enabled. Reports require their separate entitlement and the caller's
> **Create, edit, and publish reports** permission. The current report API has
> no DELETE endpoint, which is why `--create` is mandatory for persistence.
> The **data model** endpoints (Phase 3) are GA. The converter already emits the
> current `{name, folderId, document:{…}}` envelope with flat `elements` +
> `layout`; the pre-`document` flat body is rejected with HTTP 400. Before
> POSTing, validate against the live shape with the `sigma-workbooks` skill's
> `validate-spec.sh` / `POST …/spec/verify` — the field surface (control value
> fields, layout tag vocabulary) drifts and the OpenAPI is the source of truth.

Read the workbook spec back the same way; confirm no `type: error` columns and
that your authored `layout` survived (a readback that hoisted every element to
a stacked `1 / 13` span means the layout was dropped — see `sigma-workbooks`
`reference/specification/layout.md`).
(Workbook DELETE for a retry is `DELETE /v2/files/<id>`, not `/v2/workbooks/<id>`.)

### Layout-last / preservation gate

Resolve data sources, formulas, controls, compilation errors, and target choice
before final layout work. Then preserve every authored element exactly once:
workbook layout remains responsive grid XML; report layout remains absolute
pixel XML with header/footer panels. After a persistent create, compare the
saved submission and readback, then inspect workbook rendering or report PDF.
If layout or an element is dropped, stop; do not accept a server 200 as proof.

## Phase 5/6 — Verify parity (hard gate — the real proof)

Expected values come **from SSRS, never invented** — the rendered CSV
(`...?rs:Format=CSV`) the customer dropped in `expected/` during Phase 0,
normalized into `expected_parity.json` (`{"<report>/<element>":
[{"keys": {...}, "values": {...}}]}`). Map each report element to its server
element id in `element_map.json`, then:

```bash
python3 scripts/verify_parity.py --workbook-id <workbookId> \
  --expected expected_parity.json --element-map element_map.json \
  --report parity_report.md
```

Exports each Sigma element to CSV and compares row-by-row (money/counts exact to
a cent; ratios rel 1e-6). **GREEN only when every element PASSes** — never on a
200 POST alone. If no rendered CSV exists, you can verify spec compilation only
— say so explicitly; don't call structural success "parity." Mind freshness:
Sigma reads the live warehouse; re-capture SSRS numbers if rows landed since.

`verify_parity.py` currently automates workbook element CSV comparison. For a
report target, use the report element/query APIs for numeric checks and inspect
the exported PDF for pagination, clipping, repeated panels, and page count.
That manual report path is a documented degradation, not proof of parity.

### Security gate — RLS and CLS

Before declaring migration-ready, detect source security rather than assuming
the RDL is complete. Search dataset SQL and expressions for `User!UserID`,
custom user functions, security predicates, and parameterized user filters;
inspect shared data sources/datasets and the source database for RLS; inventory
which source fields were hidden by role (CLS). Map user filters to Sigma user
attributes/RLS and preserve or strengthen warehouse/Sigma CLS. Re-read the
posted DM to confirm inherited security. Test representative allowed and denied
users. Missing RLS/CLS evidence is a blocker, not a clean result.

---

## What converts, what's flagged (never faked)

**Converts:** raw-SQL datasets → Custom-SQL DM elements · Tablix matrix →
`pivot-table` (`rowsBy`/`columnsBy` entries use `columnId`) · Tablix table →
`table` · column/bar/
line/area/pie/doughnut/scatter charts → matching Sigma chart · report
parameters with safe sources → date-range/list/number/checkbox controls · clean VB expressions
(`IIf`→`If`, `Switch`, aggregates, scope-arg drop) · page-header titles → text.
The report target uses a narrower conservative chart baseline and flags/omits
unproven report element kinds.

**Flagged (loud, with an explicit warning/omission — never silently wrong):** stored-proc &
shared datasets · T-SQL dialect & `@parameter` SQL (preserved verbatim, you
translate) · custom VB `Code.*` · `RunningValue`/`Previous`/window aggregates ·
`Lookup`/`Globals!`/`ReportItems!` · gauges → KPI · maps → region/point map ·
subreports → page/drillthrough · radar/polar/funnel charts · mixed-grain Tablix ·
pagination, typography, or dynamic layout behavior not proven by RDL geometry ·
static or unresolved list valid-value sources.

## Gotchas baked into the scripts (don't re-learn these)

- **No API key in SSRS** — Windows auth only; the export CLI uses
  `-UseDefaultCredentials` / `-Credential`. A 503 usually = the Reporting
  Services *service* (not IIS) is stopped, or a named-instance URL — see
  `refs/ssrs-rest-api.md`.
- **RDL namespace drifts by version** (2008/2010/2016) — the parser walks by
  local tag name; never hard-code a namespace.
- **RDL 2016+ nests the layout in `<ReportSections><ReportSection>`** (and may
  have several sections) — a *structural nesting* axis, separate from the
  namespace one. The parser walks in and concatenates items across sections;
  never assume `<Body>`/`<Page>` sit directly under `<Report>`.
- **Workbook spec is `document`-wrapped** (workbooks-as-code) — flat
  `document.elements` + a `document.layout` that places every element; the old
  `pages[].elements` body 400s. The DM spec is unchanged.
- **`BETWEEN x AND y` contains literal ` AND `** — why SQL is never auto-split.
- **Snowflake uppercases unquoted SQL output columns** — Custom SQL refs default
  to `[Custom SQL/UPPERCASE]`; `--no-uppercase` for case-sensitive warehouses.
- **A 200 POST is not parity** — the readback `type: error` scan and
  `verify_parity.py` are the real gates.
