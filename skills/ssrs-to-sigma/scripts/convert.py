#!/usr/bin/env python3
"""
convert.py — Phase 2 of ssrs-to-sigma.

Turn a `bundle.json` (from parse_rdl.py) into Sigma specs:
  - sigma_dm_spec.json        (POST /v2/dataModels/spec)
  - sigma_workbook_spec.json  (POST /v2/workbooks/spec)
  - parity_keys.json          (Phase 5 grouping keys per report)
  - conversion_report.md      (what converted, what was flagged — never silent)

Modeling choices (documented in refs/design-notes.md):
  * Each SSRS dataset becomes one Custom-SQL data-model element
    (`source.kind: "sql"`). Most RDL datasets are raw SQL, so this is the
    majority path. The original CommandText is preserved VERBATIM — it is NOT
    auto-translated across warehouse dialects, and T-SQL `@parameters` are NOT
    rewritten. Both are surfaced as loud flags. This is deliberate: a silent,
    wrong SQL rewrite is worse than an honest "translate this before POST".
  * Each report becomes one workbook page: a base table sourcing the DM
    element, then a pivot-table per matrix Tablix, a table per table Tablix,
    and a chart per Chart. Report parameters become page controls.
  * Aggregate expressions in Tablix value cells / chart series are translated
    by ssrs_expr.translate() and surfaced as flags when not a clean 1:1.

Two-pass id wiring (mirrors the sibling converters): pass 1 emits specs with
`{{DATA_MODEL_ID}}` and element-id placeholders; after you POST the DM and read
back its server-assigned element ids, re-run with --data-model-id /
--dm-element-ids to bake them into the workbook spec.

stdlib only.
"""
import argparse
import json
import re
import sys

import ssrs_expr


# SSRS chart Type -> Sigma chart element kind
CHART_KIND = {
    "column": "bar-chart",
    "bar": "bar-chart",
    "line": "line-chart",
    "smoothline": "line-chart",
    "area": "area-chart",
    "pie": "pie-chart",
    "doughnut": "donut-chart",
    "scatter": "scatter-chart",
}

def slug(*parts):
    s = "-".join(str(p) for p in parts if p is not None)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "x"


def warehouse_colname(field_name, uppercase):
    """The output column name as the warehouse returns it (Snowflake -> upper)."""
    return field_name.upper() if uppercase else field_name


class Flags:
    def __init__(self):
        self.items = []

    def add(self, report, where, msg):
        self.items.append((report, where, msg))

    def report_md(self):
        if not self.items:
            return "# Conversion report\n\nNo flags — everything mapped cleanly. ✅\n"
        out = ["# Conversion report\n",
               "Items below need a human before this migration is GREEN "
               "(flag, never fake).\n",
               "| Report | Where | Issue |", "|---|---|---|"]
        for r, w, m in self.items:
            out.append(f"| {r} | {w} | {m} |")
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

def build_dm(bundle, conn_id, name, folder_id, uppercase, flags):
    pages_elements = []
    dm_element_index = {}    # report -> {dataset -> (element_id, base_name, {field->colref})}
    for rep in bundle["reports"]:
        rname = rep["report"]
        for ds in rep["dataSets"]:
            if not ds.get("commandText"):
                flags.add(rname, f"dataset {ds['name']}",
                          "no CommandText (shared-dataset reference or stored proc w/o body) — supply the SQL")
                continue
            el_id = slug("sql", rname, ds["name"])
            statement = ds["commandText"]

            params_in_sql = sorted(set(re.findall(r"@[A-Za-z0-9_]+", statement)))
            if params_in_sql:
                flags.add(rname, f"dataset {ds['name']}",
                          f"SQL references T-SQL parameters {', '.join(params_in_sql)} — "
                          "wire these to Sigma controls / element filters; the statement will NOT compile as-is")
            if ds.get("isStoredProc"):
                flags.add(rname, f"dataset {ds['name']}",
                          "dataset is a stored-proc call — inline the proc body as Custom SQL or escalate")
            # Cheap T-SQL dialect smell test
            if re.search(r"\b(GETDATE|ISNULL|TOP\s+\d|DATEADD|CONVERT|NVARCHAR|\[dbo\]|dbo\.)\b",
                         statement, re.IGNORECASE):
                flags.add(rname, f"dataset {ds['name']}",
                          "SQL looks like T-SQL — review for target-warehouse dialect (GETDATE/ISNULL/TOP/dbo. etc.)")

            cols = []
            field_ref = {}
            for f in ds["fields"]:
                if f["calculated"]:
                    # aggregate/calc fields live in the workbook layer, not the DM element
                    continue
                whname = warehouse_colname(f["dataField"] or f["name"], uppercase)
                cols.append({
                    "id": slug("col", rname, ds["name"], f["name"]),
                    "name": f["name"],
                    "formula": f"[Custom SQL/{whname}]",
                })
                field_ref[f["name"]] = f["name"]   # DM column display name
            pages_elements.append({
                "id": el_id,
                "kind": "table",
                "name": f"{rname} · {ds['name']}",
                "source": {"kind": "sql", "connectionId": conn_id, "statement": statement},
                "columns": cols,
            })
            dm_element_index.setdefault(rname, {})[ds["name"]] = {
                "elementId": el_id,
                "name": f"{rname} · {ds['name']}",
                "fields": field_ref,
            }

    spec = {
        "name": name,
        "folderId": folder_id,
        "schemaVersion": 1,
        "pages": [{"id": "page-1", "name": "Model", "elements": pages_elements}],
    }
    return spec, dm_element_index


# ---------------------------------------------------------------------------
# workbook
# ---------------------------------------------------------------------------

def _control_type(param):
    """SSRS parameter -> Sigma controlType.

    DateTime params are almost always a Start/End BETWEEN pair, so a single
    `date-range` control is the honest 1:1 (not two bare `date` controls). Any
    multi-value non-date param is a multi-select `list`; a single string is a
    single-select `list`; numerics are `number`; boolean is `checkbox`.
    """
    raw = (param["dataType"] or "string").lower()
    if raw in ("datetime", "date"):
        return "date-range"
    if raw == "boolean":
        return "checkbox"
    if param["multiValue"]:
        return "list"
    if raw in ("integer", "float"):
        return "number"
    return "list"


def _control(param, flags, rname):
    """Build a schema-correct control element.

    The widget/value fields are emitted with their CURRENT (workbooks-as-code)
    names — `mode` / `selectionMode` / `values` on a list, not the removed
    `multiSelect` / `defaultValue`. The filter wiring (which base-table column
    this control filters) and a list's value-list `source` depend on how the
    dataset SQL + `@parameter` were resolved, so they are LOUDLY FLAGGED for a
    human rather than bound to a guessed column — flag, never fake.
    """
    ctype = _control_type(param)
    ctl = {
        "id": slug("ctl", rname, param["name"]),
        "kind": "control",
        "controlId": slug(param["name"]),
        "controlType": ctype,
        "name": param["prompt"] or param["name"],
    }

    if ctype == "list":
        ctl["mode"] = "include"
        ctl["selectionMode"] = "multiple" if param["multiValue"] else "single"
        literals = [d for d in param["defaultValues"] if not str(d).startswith("=")]
        exprs = [d for d in param["defaultValues"] if str(d).startswith("=")]
        # `values` is the default SELECTION ([] = all). It is a list for both
        # single- and multi-select list controls.
        ctl["values"] = literals
        if exprs:
            flags.add(rname, f"parameter {param['name']}",
                      "default is an expression (e.g. =Today()) — set the control default by hand")
        if param["validValuesStatic"] or param["validValuesQuery"]:
            flags.add(rname, f"parameter {param['name']}",
                      "list control needs a value-list `source` — point it at the DM/base-table column "
                      "(or dataset-query equivalent) that supplies its choices")
    elif param["defaultValues"]:
        # non-list control carrying an expression default (e.g. =Today())
        if any(str(d).startswith("=") for d in param["defaultValues"]):
            flags.add(rname, f"parameter {param['name']}",
                      "default is an expression (e.g. =Today()) — set the control default by hand")

    # Filter wiring is the one thing we never fake: it depends on the resolved
    # SQL/@parameter, so surface it instead of binding a guessed column.
    flags.add(rname, f"parameter {param['name']}",
              f"wire this {ctype} control's `filters` to the base-table column it filters "
              "(depends on how the dataset SQL / @parameter was resolved)")
    return ctl


def build_workbook(bundle, dm_id, dm_element_ids, dm_element_index,
                   name, folder_id, flags):
    # Workbooks-as-code: elements are a single FLAT, workbook-global collection;
    # `pages` carries metadata only; `layout` XML is the sole source of truth for
    # which page each element sits on. (The data-model spec keeps its
    # pages[].elements nesting — only the WORKBOOK surface changed.)
    all_elements = []
    pages_meta = []
    page_blocks = []
    for rep in bundle["reports"]:
        rname = rep["report"]
        # Surface any parser warning (e.g. an unrecognized RDL layout wrapper)
        # and refuse to let a visual-less report convert silently — an empty
        # body with live datasets is a structural miss, not a clean report.
        for w in rep.get("warnings", []):
            flags.add(rname, "parse", w)
        if not rep.get("bodyItems") and (rep.get("dataSets") or rep.get("parameters")):
            flags.add(rname, "report body",
                      "no report visuals parsed — only a base table will be emitted; "
                      "check the RDL layout / ReportSections nesting before trusting this workbook")
        idx = dm_element_index.get(rname, {})
        if not idx:
            continue
        # primary dataset = the one most report items reference
        primary_ds = None
        for it in rep["bodyItems"]:
            if it.get("dataSetName"):
                primary_ds = it["dataSetName"]
                break
        primary_ds = primary_ds or next(iter(idx))
        dm_meta = idx.get(primary_ds) or next(iter(idx.values()))

        # resolve the DM element's server id (placeholder until phase 4)
        if dm_element_ids and dm_meta["name"] in dm_element_ids:
            dm_el_server_id = dm_element_ids[dm_meta["name"]]
        else:
            dm_el_server_id = "{{%s_ID}}" % slug(dm_meta["elementId"]).upper().replace("-", "_")

        page_id = slug("page", rname)
        base_id = slug("base", rname)
        base_name = f"{rname} Base"

        # base table: passthrough of the DM element's columns
        base_cols = []
        for fname in dm_meta["fields"]:
            base_cols.append({
                "id": slug("c", rname, fname),
                "name": fname,
                "formula": f"[{fname}]",       # data-model source: reference DM column by name
            })
        base_table = {
            "id": base_id,
            "kind": "table",
            "name": base_name,
            "source": {
                "kind": "data-model",
                "dataModelId": dm_id or "{{DATA_MODEL_ID}}",
                "elementId": dm_el_server_id,
            },
            "columns": base_cols,
        }
        elements = [base_table]

        # controls
        for p in rep["parameters"]:
            elements.append(_control(p, flags, rname))

        # title from page header — a `text` element carries Markdown in `body`
        # (no `name`/`content`, which the current spec rejects/strips).
        for hi in rep.get("pageHeaderItems", []):
            if hi.get("kind") == "textbox" and hi.get("value") and not str(hi["value"]).startswith("="):
                elements.append({
                    "id": slug("txt", rname, hi["name"]),
                    "kind": "text",
                    "body": hi["value"],
                })
                break

        # body items
        for it in rep["bodyItems"]:
            kind = it.get("kind")
            if kind == "tablix":
                elements.append(_tablix_element(it, rname, base_id, base_name, flags))
            elif kind == "chart":
                elements.append(_chart_element(it, rname, base_id, base_name, flags))
            elif kind in ("gauge", "map", "subreport"):
                flags.add(rname, f"{kind} {it.get('name')}", it.get("flag", f"{kind} not auto-converted"))
                elements.append({
                    "id": slug(kind, rname, it.get("name")),
                    "kind": "table", "name": f"[FLAGGED {kind}] {it.get('name')}",
                    "source": {"kind": "table", "elementId": base_id},
                    "columns": base_cols[:3] or [{"id": slug("c", rname, "_"), "name": "_", "formula": "1"}],
                })

        all_elements.extend(elements)
        pages_meta.append({"id": page_id, "name": rname})
        page_blocks.append(_page_layout(page_id, elements))

    document = {
        "schemaVersion": 1,
        "kind": "workbook",
        "elements": all_elements,
        "pages": pages_meta,
    }
    if page_blocks:
        document["layout"] = ('<?xml version="1.0" encoding="utf-8"?>\n'
                              + "\n".join(page_blocks))
    spec = {"name": name, "folderId": folder_id, "document": document}
    _validate_workbook(spec)
    return spec


# element kind -> a sensible vertical row span for the stacked starter layout.
_LAYOUT_SPAN = {
    "control": 3, "text": 2, "kpi-chart": 5,
    "table": 12, "pivot-table": 12, "input-table": 12,
}
_DEFAULT_SPAN = 10


def _page_layout(page_id, elements):
    """One page's layout XML: a stacked, full-width 24-column grid.

    Every element is placed exactly once (the API rejects an unplaced element),
    each on its own row band. This is a valid starter layout, not a faithful
    reproduction of the RDL's pixel geometry — mapping <Top>/<Left>/<Width>/
    <Height> onto the grid is a later enhancement. `document.elements[].id`
    values are slugs (`[a-z0-9-]`), so they need no XML-attribute escaping.
    """
    lines = [
        f'<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" '
        f'gridTemplateRows="auto" id="{page_id}">'
    ]
    row = 1
    for el in elements:
        span = _LAYOUT_SPAN.get(el.get("kind"), _DEFAULT_SPAN)
        lines.append(
            f'  <Element elementId="{el["id"]}" gridColumn="1 / 25" '
            f'gridRow="{row} / {row + span}"/>'
        )
        row += span
    lines.append("</Page>")
    return "\n".join(lines)


def _validate_workbook(spec):
    """Structural guard before write: unique ids, every element placed once,
    no dangling layout reference. Cheap local checks that catch the mistakes a
    200-POST would otherwise mask (or reject opaquely)."""
    doc = spec["document"]
    ids = [e.get("id") for e in doc["elements"]]
    seen, dupes = set(), set()
    for i in ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    if dupes:
        raise ValueError(f"workbook spec: duplicate element ids {sorted(dupes)}")
    layout = doc.get("layout", "")
    placed = set(re.findall(r'elementId="([^"]+)"', layout))
    idset = set(ids)
    unplaced = idset - placed
    dangling = placed - idset
    if unplaced:
        raise ValueError(f"workbook spec: elements missing from layout {sorted(unplaced)}")
    if dangling:
        raise ValueError(f"workbook spec: layout references unknown elements {sorted(dangling)}")


def _grp_field(expr):
    """=Fields!X.Value -> X (group/category expression -> field name)."""
    m = ssrs_expr.FIELDS_RE.search(expr or "")
    return m.group(1) if m else None


def _tablix_element(it, rname, base_id, base_name, flags):
    el = {
        "id": slug("tbx", rname, it["name"]),
        "name": it["name"],
        "source": {"kind": "table", "elementId": base_id},
        "columns": [],
    }
    cols = el["columns"]
    rows_by, cols_by, values = [], [], []

    def add_dim(field, shelf):
        if not field:
            return None
        cid = slug("d", rname, it["name"], field)
        cols.append({"id": cid, "name": field, "formula": f"[{base_name}/{field}]"})
        shelf.append({"id": cid})
        return cid

    for g in it.get("rowGroups", []):
        for e in g["expressions"]:
            add_dim(_grp_field(e), rows_by)
    for g in it.get("columnGroups", []):
        for e in g["expressions"]:
            add_dim(_grp_field(e), cols_by)

    for i, vexpr in enumerate(it.get("valueExpressions", [])):
        formula, fl = ssrs_expr.translate(vexpr, ref_fmt=f"[{base_name}/{{0}}]")
        for w in fl:
            flags.add(rname, f"tablix {it['name']} value", w)
        cid = slug("v", rname, it["name"], i)
        cols.append({"id": cid, "name": f"Value {i+1}" if i else "Value", "formula": formula})
        values.append(cid)

    if it["shape"] == "matrix" and cols_by:
        el["kind"] = "pivot-table"
        el["values"] = values
        el["rowsBy"] = rows_by
        el["columnsBy"] = cols_by
    else:
        el["kind"] = "table"
        el["order"] = [c["id"] for c in cols]
        dim_ids = [d["id"] for d in rows_by] + [d["id"] for d in cols_by]
        if dim_ids and values:
            # A grouped Tablix is an AGGREGATED query. A Sigma `table` without a
            # `groupings` entry renders raw detail rows (the #1 migration bug —
            # dimensions repeat, aggregate cells read per-row), so a grouped
            # table MUST carry groupBy dims + aggregate calculations.
            el["groupings"] = [{
                "id": slug("grp", rname, it["name"]),
                "groupBy": dim_ids,
                "calculations": values,
            }]
    return el


def _chart_element(it, rname, base_id, base_name, flags):
    kind = CHART_KIND.get((it.get("chartType") or "column").lower())
    if not kind:
        flags.add(rname, f"chart {it['name']}",
                  f"chart type '{it.get('chartType')}' has no clean Sigma analog — "
                  "review (radar/polar/funnel/range = redesign)")
        kind = "bar-chart"
    el = {
        "id": slug("chart", rname, it["name"]),
        "kind": kind,
        "name": it["name"],
        "source": {"kind": "table", "elementId": base_id},
        "columns": [],
    }
    cols = el["columns"]
    x_id = None
    for e in it.get("categoryExpressions", []):
        f = _grp_field(e)
        if f:
            x_id = slug("cx", rname, it["name"], f)
            cols.append({"id": x_id, "name": f, "formula": f"[{base_name}/{f}]"})
            break
    y_ids = []
    for s in it.get("series", []):
        for i, vexpr in enumerate(s.get("values", [])):
            formula, fl = ssrs_expr.translate(vexpr, ref_fmt=f"[{base_name}/{{0}}]")
            for w in fl:
                flags.add(rname, f"chart {it['name']} series", w)
            yid = slug("cy", rname, it["name"], s.get("name"), i)
            cols.append({"id": yid, "name": s.get("name") or f"Series {i+1}", "formula": formula})
            y_ids.append(yid)
    if x_id:
        el["xAxis"] = {"columnId": x_id}
    if y_ids:
        el["yAxis"] = {"columnIds": y_ids}
    return el


# ---------------------------------------------------------------------------
# parity keys
# ---------------------------------------------------------------------------

def build_parity_keys(bundle):
    out = {}
    for rep in bundle["reports"]:
        rname = rep["report"]
        for it in rep["bodyItems"]:
            if it.get("kind") != "tablix":
                continue
            keys = []
            for g in it.get("rowGroups", []) + it.get("columnGroups", []):
                for e in g["expressions"]:
                    f = _grp_field(e)
                    if f:
                        keys.append(f)
            out[f"{rname}/{it['name']}"] = {"keys": keys,
                                            "valueCount": len(it.get("valueExpressions", []))}
    return out


def main():
    ap = argparse.ArgumentParser(description="Convert SSRS bundle.json to Sigma specs")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--connection-id", required=True, help="Sigma warehouse connection id")
    ap.add_argument("--folder-id", required=True, help="destination Sigma folder id")
    ap.add_argument("--dm-name", default="SSRS Migration")
    ap.add_argument("--wb-name", default="SSRS Migration")
    ap.add_argument("--data-model-id", help="server dataModelId (phase 4)")
    ap.add_argument("--dm-element-ids", help="JSON {elementName: serverId} read back after DM POST")
    ap.add_argument("--no-uppercase", action="store_true",
                    help="do NOT uppercase Custom SQL column refs (default assumes Snowflake)")
    ap.add_argument("--out-prefix", default="sigma")
    args = ap.parse_args()

    with open(args.bundle) as fh:
        bundle = json.load(fh)
    dm_element_ids = None
    if args.dm_element_ids:
        with open(args.dm_element_ids) as fh:
            dm_element_ids = json.load(fh)

    flags = Flags()
    dm_spec, dm_index = build_dm(bundle, args.connection_id, args.dm_name,
                                 args.folder_id, not args.no_uppercase, flags)
    wb_spec = build_workbook(bundle, args.data_model_id, dm_element_ids, dm_index,
                             args.wb_name, args.folder_id, flags)
    parity = build_parity_keys(bundle)

    with open(f"{args.out_prefix}_dm_spec.json", "w") as fh:
        json.dump(dm_spec, fh, indent=2)
    with open(f"{args.out_prefix}_workbook_spec.json", "w") as fh:
        json.dump(wb_spec, fh, indent=2)
    with open("parity_keys.json", "w") as fh:
        json.dump(parity, fh, indent=2)
    with open("conversion_report.md", "w") as fh:
        fh.write(flags.report_md())

    wb_doc = wb_spec["document"]
    print(f"DM elements: {len(dm_spec['pages'][0]['elements'])}  "
          f"workbook pages: {len(wb_doc['pages'])}  "
          f"workbook elements: {len(wb_doc['elements'])}  flags: {len(flags.items)}")
    print(f"wrote {args.out_prefix}_dm_spec.json, {args.out_prefix}_workbook_spec.json, "
          "parity_keys.json, conversion_report.md")
    if flags.items:
        print("\n--- flags (also in conversion_report.md) ---")
        for r, w, m in flags.items:
            print(f"  [{r}] {w}: {m}")


if __name__ == "__main__":
    main()
