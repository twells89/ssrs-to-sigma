# SSRS report item → Sigma element mapping

The lookup the converter applies, and the same coverage table the
`ssrs-assessment` skill scores reports against. "Difficulty" is the honest
expectation, not a promise.

## Report items

| SSRS report item | Sigma element | Difficulty | Notes |
|---|---|---|---|
| Tablix — **table** (no column groups) | `table` | clean | flat detail or row-grouped |
| Tablix — **matrix** (row + column groups) | `pivot-table` (`rowsBy`/`columnsBy`/`values`) | clean | the bread-and-butter case |
| Tablix — **list** (banded) | `table` w/ grouping *or* a chart | medium | banded layouts often need redesign |
| Tablix — **mixed grain** (groups + detail band) | hand-split into a pivot OR a grouped table | hard | not both; decompose |
| Chart — Column / Bar | `bar-chart` | clean | orientation via config |
| Chart — Line / SmoothLine | `line-chart` | clean | |
| Chart — Area | `area-chart` | clean | |
| Chart — Pie | `pie-chart` | clean | |
| Chart — Doughnut | `donut-chart` | clean | |
| Chart — Scatter | `scatter-chart` | clean | |
| Chart — Range / Funnel / Pyramid | KPI / bar substitution | hard | flagged |
| Chart — Radar / Polar / Shape | redesign | hard | no analog — flagged |
| **Gauge / GaugePanel** | KPI substitution | hard | flagged; explicit warning/omission |
| **Map** (region / point) | region-map / point-map | hard | manual review — flagged |
| **Subreport** | separate page / drillthrough | medium | multi-pass |
| Textbox (title/free text) | `text` element | clean | header titles only |
| Image / Line / Rectangle (chrome) | — | n/a | layout chrome, usually dropped |

## Datasets & sources

| SSRS | Sigma | Difficulty |
|---|---|---|
| Dataset — raw SQL (`<CommandText>`) | Custom-SQL DM element (`source.kind: "sql"`) | clean (but dialect/param review) |
| Dataset — stored proc (`CommandType=StoredProcedure`) | inline the proc body as Custom SQL | hard — flagged |
| Shared dataset (external ref) | export it too / supply SQL | hard — flagged if missing |
| Data source (`<ConnectString>`) | Sigma connection id + warehouse path | medium — connection lookup is manual |

## Parameters → controls

| SSRS parameter | Sigma control | Difficulty |
|---|---|---|
| Single value, String with a safe valid-values source | `list` (`selectionMode: single`) | clean |
| Single value, DateTime | `date-range` (SSRS date params are typically BETWEEN pairs) | clean |
| Single value, Integer/Float | `number` | clean |
| Boolean | `checkbox` | clean |
| Multi-value (`MultiValue=true`) | `list` (`selectionMode: multiple`) | clean (filter wiring manual) |
| Valid values from a dataset query | `list` with a value-list `source` on a DM column | medium — flagged |
| Static valid values | omitted until a current literal-source shape is proven | medium — flagged |
| Default = expression (`=Today()`) | set control default by hand | medium — flagged |

> **Control field names are the current workbooks-as-code shape.** A `list`
> control carries flat `mode` / `selectionMode` / `values` (NOT the removed
> `multiSelect` / `defaultValue`); its value-list `source` and the `filters`
> binding are independent. The converter wires a value-list source only when a
> parsed query names a converted dataset context and its value field exactly
> matches a base column. This may be the visual dataset or a separately declared
> lookup dataset; it is never rebound to an unrelated primary source. Static
> valid values are flagged and omitted because the current literal-source shape
> has not been proven; mapping them to a same-name data column would expose
> unrestricted values. Missing datasets/fields are likewise flagged and
> omitted. Target `filters`
> remain flagged because they depend on resolving dataset SQL / `@parameter`.
> The converter does not fake a column binding — flag, never fake.

## Why "flagged" matters

Anything in the **hard** rows is emitted as a **loud flag** in
`conversion_report.md` (and, where useful, an explicit “not converted” text
element in a workbook), never as a confidently-wrong data visualization. Report
output omits unsupported items. A gauge silently turned into the wrong KPI, or a
T-SQL proc that won't run in Snowflake, is worse than an explicit "a human must
handle this." This is the shared contract across every sibling converter:
**flag, never fake.**

The report target uses the conservative `sigma-reports` support matrix:
table/pivot, text, bar/line/area/scatter, and standard controls. Pie/donut and
unknown chart kinds are omitted with flags until targeted report verify,
readback, and PDF evidence establishes support.
