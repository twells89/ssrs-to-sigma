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

**The element *tree* is stable across these years — only the namespace URI
changes.** So the parser strips namespaces and walks by **local tag name**
(`_local()` / `child()` / `descendants()`), making it version-agnostic. Don't
hard-code a namespace.

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
  <Body>
    <ReportItems> … </ReportItems>    the visuals (see below)
    <Height>…</Height>
  <Width>…</Width>                    (page width sits on <Report>)
  <Page>
    <PageHeader><ReportItems>…        → pageHeaderItems
    <PageFooter><ReportItems>…        → pageFooterItems
```

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
pageHeaderItems[]
pageFooterItems[]
```

`fixtures/expected_bundle.json` is a real parse of `fixtures/SalesByRegion.rdl`
— diff against it as a regression check after changing the parser.

## Gotchas

- **Namespace drift** — never match on a full namespaced tag; walk by local
  name. (Already handled — just don't "fix" it by hard-coding 2016.)
- **RDLC has no `<DataSources>`** — `dataSources` will be empty; that's normal.
- **Shared datasets / shared data sources** are external references
  (`<DataSourceReference>`, `<DataSet>` with no inline `<Query>`). The RDL only
  names them — you must export them too, or supply the SQL. The parser flags a
  dataset with no `<CommandText>`.
- **`BETWEEN x AND y` contains the literal ` AND `** — any code that splits a
  WHERE clause on ` AND ` will corrupt it. This is why `convert.py` does **not**
  auto-rewrite SQL; it preserves `CommandText` verbatim and flags.
- **Aggregate scope** — `=Sum(Fields!X.Value, "Dataset1")`: the second arg is a
  dataset/group scope, not a real argument. `ssrs_expr` drops it (see
  `expression-mapping.md`).
