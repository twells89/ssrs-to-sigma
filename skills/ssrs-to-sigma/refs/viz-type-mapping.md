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
| **Gauge / GaugePanel** | KPI substitution | hard | flagged, table fallback |
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
| Single value, String | `list` | clean |
| Single value, DateTime | `date` (Start/End pair → date-range) | clean |
| Single value, Integer/Float | `number` | clean |
| Boolean | `checkbox` | clean |
| Multi-value (`MultiValue=true`) | `list` + `multiSelect` | clean (filter wiring manual) |
| Valid values from a dataset query | `list` sourced from a DM column | medium — flagged |
| Default = expression (`=Today()`) | set control default by hand | medium — flagged |

## Why "flagged" matters

Anything in the **hard** rows is emitted as a **loud flag** in
`conversion_report.md` (and a table-fallback element in the workbook), never as
a confidently-wrong conversion. A gauge silently turned into the wrong KPI, or a
T-SQL proc that won't run in Snowflake, is worse than an explicit "a human must
handle this." This is the shared contract across every sibling converter:
**flag, never fake.**
