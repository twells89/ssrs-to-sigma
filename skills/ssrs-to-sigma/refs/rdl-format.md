# RDL format — what the parser reads

SSRS reports are authored as **RDL** (Report Definition Language), a public
namespaced XML format. `scripts/parse_rdl.py` reads it into the normalized
`bundle.json` that `convert.py` consumes. This doc is the map: RDL element →
what the parser does with it.

## Namespaces & versions

The root element is `<Report>`. Its default namespace pins the schema *year*:

| SSRS version | namespace |
|---|---|
| 2008 / 2008 R2 | `http://schemas.microsoft.com/sqlserver/reporting/2008/01/reportdefinition` |
| 2010 (R2+)     | `http://schemas.microsoft.com/sqlserver/reporting/2010/01/reportdefinition` |
| 2016 / 2017 / 2019 / 2022 / PBIRS | `http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition` |

A second designer namespace, usually prefixed `rd:`
(`http://schemas.microsoft.com/SQLServer/reporting/reportdesigner`), carries
authoring hints like `<rd:TypeName>`.

**The namespace URI changes by year, but that is only *one* compatibility
axis.** The parser strips namespaces and walks by **local tag name**
(`_local()` / `child()` / `descendants()`), making it namespace-agnostic. Don't
hard-code a namespace.

**A second, independent axis is *structural nesting*** — where the layout
elements sit in the tree — and it is NOT solved by namespace stripping. RDL
2016+ (2016/01 schema, SSDT / Power BI Report Server) wraps the layout in
`<ReportSections><ReportSection><Body>/<Page>`, and permits **more than one**
`<ReportSection>`. Older 2008/2010 RDL puts a single `<Body>`/`<Page>` directly
under `<Report>`. `parse_rdl()` walks into the sections (concatenating items
across all of them) and falls back to the flat root layout — see
`_iter_layouts()`. Missing this walk is how a real 2016 export silently loses
every Tablix/Chart/Subreport while datasets and parameters (direct children of
`<Report>`) still parse, so the report looks like a clean, empty success.

**RDLC** (the client/local variant used by ReportViewer) is the same XML minus
the `<DataSources>` block — data is bound at runtime. The parser handles it
identically; you just won't get connection strings.

## Top-level structure

```
<Report>                              report (name comes from the file name)
  <DataSources>
    <DataSource Name="…">
      <DataSourceReference>…</…>      → shared data source (a path, not inline)
      <ConnectionProperties>
        <DataProvider>SQL</…>         SQL | OLEDB | ORACLE | …
        <ConnectString>…</…>          parsed but NOT auto-mapped to a Sigma conn
  <DataSets>
    <DataSet Name="…">
      <Query>
        <DataSourceName>…</…>
        <CommandText>SELECT …</…>     the SQL (or proc name)
        <CommandType>StoredProcedure</…>   present → isStoredProc=true
        <QueryParameters>
          <QueryParameter Name="@x"><Value>=Parameters!x.Value</Value>
      <Fields>
        <Field Name="…">
          <DataField>…</…>            real DB column  → calculated=false
          <Value>=…</…>               expression field → calculated=true
          <rd:TypeName>System.Decimal</…>
  <ReportParameters>
    <ReportParameter Name="…">
      <DataType>DateTime</…>          String | DateTime | Integer | Float | Boolean
      <MultiValue>true</…>
      <Nullable>…</…>
      <Prompt>…</…>
      <DefaultValue><Values><Value>…</Value>
      <ValidValues>
        <DataSetReference><DataSetName/><ValueField/><LabelField/>  → dynamic list
        <ParameterValues><ParameterValue><Value/>                   → static list
  <Body>                              2008/2010 flat layout: Body/Page are
    <ReportItems> … </ReportItems>    the visuals (see below)   direct children
    <Height>…</Height>                                          of <Report>
  <Width>…</Width>                    (page width sits on <Report>)
  <Page>
    <PageHeader><ReportItems>…        → pageHeaderItems
    <PageFooter><ReportItems>…        → pageFooterItems
```

**RDL 2016+ nests that same Body/Page inside report sections** (and may have
more than one section). The parser reads every section, so the resulting
`bodyItems` / `pageHeaderItems` / `pageFooterItems` are the concatenation
across all of them:

```
<Report>
  <ReportSections>
    <ReportSection>
      <Body><ReportItems> … </ReportItems></Body>   → bodyItems (this + every other section)
      <Page><PageHeader/><PageFooter/></Page>        → pageHeaderItems / pageFooterItems
    <ReportSection> …                                (multi-section: items concatenated)
```

If a `<Report>` has neither a flat `<Body>`/`<Page>` nor any `<ReportSection>`,
`parse_rdl()` raises (malformed / unsupported layout). If a layout element
exists but yields zero recognized visuals while datasets/parameters are
present, the report carries a `warnings` entry (also printed to stderr) so the
miss surfaces instead of scoring as a clean, empty report.

## Report items (the visuals)

Everything renderable lives inside a `<ReportItems>` container. The parser
recognizes:

### Tablix — table vs matrix vs list

`<Tablix>` is the workhorse: one element that renders three ways depending on
its **group structure**. This classification is the single trickiest part of
reading RDL.

- `<TablixRowHierarchy>` and `<TablixColumnHierarchy>` each hold
  `<TablixMembers>/<TablixMember>`. A member is a **group** when it contains
  `<Group Name="…"><GroupExpressions><GroupExpression>=Fields!X.Value</…>`.
  Members without a `<Group>` are static/detail bands.
- `<TablixBody>` holds the cells: `<TablixCell>/<CellContents>/<Textbox>/
  <Paragraphs>/<Paragraph>/<TextRuns>/<TextRun>/<Value>` — the `<Value>` is the
  cell expression, e.g. `=Sum(Fields!Sales.Value)`. The parser collects the
  `=`-prefixed cell values as the Tablix's **value expressions** (the measures).

Classification (`parse_tablix`):

| RDL shape | parser `shape` | Sigma target |
|---|---|---|
| group(s) on **both** row & column hierarchies | `matrix` | `pivot-table` (rowsBy / columnsBy / values) |
| group(s) on one hierarchy only | `table` (grouped) | `table` with `order` |
| no groups (flat detail band) | `table` | `table` |

Position: `<Top> <Left> <Width> <Height>` (inches, e.g. `"1.2in"`), plus
`<DataSetName>`.

### Chart

`<Chart>`:
- `<ChartCategoryHierarchy>/<ChartMembers>/<ChartMember>/<Group>/
  <GroupExpressions>` → the category axis (x).
- `<ChartSeriesCollection>/<ChartSeries Name="…">` with `<Type>`
  (Column / Bar / Line / Area / Pie / Doughnut / Scatter / Shape / Range / …)
  and `<ChartDataPoints>/<ChartDataPoint>/<ChartDataPointValues>/<Y>` → the
  measure expression(s). The `<Type>` drives `CHART_KIND` in `convert.py`.

### Flagged-not-converted items

- `<Gauge>` / `<GaugePanel>` — radial/linear gauge → Sigma KPI substitution.
- `<Map>` — region/point map → manual review.
- `<Subreport>` (`<ReportName>`) — embeds another report → separate page /
  drillthrough, multi-pass.
- `<Rectangle>` / `<List>` — containers; the parser recurses into their nested
  `<ReportItems>`.
- `<Textbox>` — free text / titles (page-header title becomes a Sigma text
  element).

## What the parser emits (bundle.json)

```
report                              file name (no extension)
sourceFile
dataSources[]   name, reference, provider, connectString
dataSets[]      name, dataSourceName, commandText, isStoredProc,
                queryParameters[{name,value}],
                fields[{name, dataField, expression, calculated, typeName}]
parameters[]    name, dataType, multiValue, nullable, prompt,
                defaultValues[], validValuesQuery{dataSet,valueField,labelField},
                validValuesStatic[]
bodyItems[]     tablix | chart | gauge | map | subreport | textbox
                (concatenated across every <ReportSection> for 2016+ RDL)
pageHeaderItems[]
pageFooterItems[]
warnings[]      present ONLY when a structural miss is suspected (layout found
                but zero visuals parsed while datasets/parameters exist)
```

`fixtures/expected_bundle.json` is a real parse of `fixtures/SalesByRegion.rdl`
(flat 2008/2010 layout) and `fixtures/expected_reportsections_bundle.json` is a
real parse of `fixtures/ReportSections2016.rdl` (2016 `<ReportSections>` with a
subreport + grouped Tablix + header/footer). Diff against both after changing
the parser — the offline suite in `tests/test_ssrs.py` does exactly this.

## Gotchas

- **Namespace drift** — never match on a full namespaced tag; walk by local
  name. (Already handled — just don't "fix" it by hard-coding 2016.)
- **ReportSections is a *nesting* axis, not a namespace one** — stripping the
  namespace does not reach into `<ReportSections><ReportSection>`; the parser
  walks in explicitly. (Already handled — see `_iter_layouts()`. The "never
  hard-code a namespace" rule above does NOT cover this.)
- **RDLC has no `<DataSources>`** — `dataSources` will be empty; that's normal.
- **Shared datasets / shared data sources** are external references
  (`<DataSourceReference>`, `<DataSet>` with no inline `<Query>`). The RDL only
  names them — you must export them too, or supply the SQL. A dataset with no
  `<CommandText>` is flagged **downstream** (`convert.py` / `scan_gaps.py`), not
  by the parser — `parse_rdl` just records `commandText: null`.
- **`BETWEEN x AND y` contains the literal ` AND `** — any code that splits a
  WHERE clause on ` AND ` will corrupt it. This is why `convert.py` does **not**
  auto-rewrite SQL; it preserves `CommandText` verbatim and flags.
- **Aggregate scope** — `=Sum(Fields!X.Value, "Dataset1")`: the second arg is a
  dataset/group scope, not a real argument. `ssrs_expr` drops it (see
  `expression-mapping.md`).
