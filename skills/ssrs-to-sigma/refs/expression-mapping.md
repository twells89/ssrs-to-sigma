# SSRS expressions → Sigma formulas

SSRS expressions are VB.NET-flavored and begin with `=`. `scripts/ssrs_expr.py`
does a best-effort syntactic translation and — critically — **returns the list
of things it could not translate cleanly** rather than emitting silently-wrong
logic. `translate(expr, ref_fmt) -> (formula, flags)`; an empty `flags` means a
clean 1:1.

`ref_fmt` controls how a field reference renders:
- `"[{0}]"` — a data-model column (default).
- `"[Master/{0}]"` or `"[<ElementName>/{0}]"` — a workbook formula referencing a
  source element by name (what `convert.py` passes for chart/pivot value cells).

## Reference & operator rewrites

| SSRS | Sigma | Notes |
|---|---|---|
| `Fields!X.Value` | `[X]` (via `ref_fmt`) | field reference |
| `Parameters!P.Value` | `[P]` | wire to the matching control |
| `Sum(Fields!X.Value, "Dataset1")` | `Sum([X])` | **aggregate scope arg dropped** — only on known aggregates, so it never eats a real string arg |
| `<>` | `!=` | not-equal |
| `AndAlso` / `And` | `and` | |
| `OrElse` / `Or` | `or` | |
| `Not` | `not` | |
| `Mod` | `%` | |
| `&` | `&` | string concat — same in Sigma |

## Function map (`FUNC_MAP`)

| SSRS | Sigma |
|---|---|
| `IIf` | `If` |
| `Switch` | `Switch` |
| `Sum` `Avg` `Min` `Max` `First` `Last` | same |
| `Count` | `Count` |
| `CountDistinct` | `CountDistinct` |
| `Abs` `Round` `Ceiling` `Floor` | same |
| `Len` | `Length` |
| `Trim` | `Trim` |
| `UCase` / `Upper` | `Upper` |
| `LCase` / `Lower` | `Lower` |
| `Left` `Right` | same |
| `Mid` | `Substring` |
| `IsNothing` | `IsNull` |
| `Today` `Now` | same |
| `Year` `Month` `Day` | same |
| `CDbl` `CInt` | `Number` |
| `CStr` | `Text` |
| `Format` | `Format` (⚠ flagged — Sigma masks differ; usually element format config) |

The rename is whole-word, case-insensitive, and only applied to a name
immediately before `(`, so `Fields!Count.Value` (a field named "Count") is not
rewritten.

## Flagged — no clean 1:1 (passed through, surfaced for review)

These are **kept verbatim in the output and added to `flags`** — the converter
will not guess:

- **`Code.Foo(...)`** — custom embedded VB (`<Code>` block). No Sigma analog;
  hand-translate. (Distinguished from a field named `Code` — `Fields!Code.Value`
  is *not* flagged.)
- **`RunningValue`, `Previous`, `RowNumber`, `RunningTotal`** — running/positional
  aggregates. Sigma has window functions (`SumOver`, `RunningSum`) but they
  **silently error inside data-model element calc columns** (see the
  `sigma-data-models` skill's `calc-columns.md`); do them in a workbook
  grouping/window context or as Custom SQL. Flagged.
- **`Lookup` / `LookupSet` / `MultiLookup`** — cross-dataset lookups. Map to a
  Sigma `Lookup(...)` on the model, or a relationship/join. Flagged for review.
- **`Aggregate`, `Level`, `InScope`** — matrix-scope introspection. Re-express
  against the pivot's grouping. Flagged.
- **`ReportItems!Textbox1.Value`** — references another textbox's rendered
  value; re-express against the underlying data. Flagged.
- **`Globals!…` / `User!…`** — runtime objects (`ExecutionTime`, `UserID`). Map
  to a Sigma system function (`CurrentUserEmail()`, etc.) by hand. Flagged.
- **`Format(...)`** — numeric/date mask string; Sigma uses per-column format
  config, not an inline function. Flagged for review.

## Worked examples

```
=Sum(Fields!SalesAmount.Value)
  -> Sum([SalesAmount])

=Fields!RegionName.Value
  -> [RegionName]

=IIf(Sum(Fields!SalesAmount.Value) = 0, 0,
     Sum(Fields!SalesAmount.Value) * 0.32 / Sum(Fields!SalesAmount.Value))
  -> If(Sum([SalesAmount]) = 0, 0,
        Sum([SalesAmount]) * 0.32 / Sum([SalesAmount]))

=Switch(Fields!Q.Value=1,"A", Fields!Q.Value=2,"B")
  -> Switch([Q]=1,"A", [Q]=2,"B")

=RunningValue(Fields!Sales.Value, Sum, "DS1")
  -> RunningValue([Sales], Sum)          ⚠ flagged: no clean Sigma analog
```

## Aggregate-vs-row-level grain

In SSRS, a Tablix cell expression like `=Sum(Fields!Sales.Value)` is an
aggregate evaluated within the cell's group scope. In Sigma that becomes an
aggregate **measure** in the pivot/chart (`values` / `yAxis`), grouped by the
`rowsBy`/`columnsBy` dimensions — which is exactly how `convert.py` wires it.
A bare `=Fields!X.Value` in a detail row is row-level — it becomes a plain
column reference. Keep the distinction: dimensions (group expressions) →
row/column shelves; aggregates (cell values) → measures.
