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
  * Each report becomes one workbook page: one base table per converted SSRS
    dataset, then each Tablix/Chart sources the base named by its dataSetName.
    Safely resolvable report parameters become page controls.
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
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import ssrs_expr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import code_rep  # noqa: E402


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
REPORT_SAFE_CHART_KINDS = {
    "bar-chart", "line-chart", "area-chart", "scatter-chart",
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


def _control(param, flags, rname, source_contexts=None):
    """Build a schema-correct control element.

    The widget/value fields are emitted with their CURRENT (workbooks-as-code)
    names — `mode` / `selectionMode` / `values` on a list, not the removed
    `multiSelect` / `defaultValue`. A query-backed list source is wired only
    when its declared dataset has a converted source context and its value
    field exactly matches a base column. Static valid values are omitted until
    a current literal-source shape is proven. Otherwise the control is flagged
    and omitted. Target filter
    wiring still depends on the resolved dataset SQL / `@parameter` and remains
    a loud manual flag — flag, never fake.
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
        valid_query = param.get("validValuesQuery")
        valid_static = param.get("validValuesStatic")
        if valid_query:
            declared_dataset = valid_query.get("dataSet")
            source_context = (
                (source_contexts or {}).get(declared_dataset)
                if declared_dataset else None
            )
            if source_context is None:
                flags.add(
                    rname,
                    f"parameter {param['name']}",
                    "list valid-values dataset "
                    f"{declared_dataset!r} has no converted source context; "
                    "control was omitted rather than binding an unrelated table",
                )
                return None
            source_field = valid_query.get("valueField")
        elif valid_static is not None:
            flags.add(
                rname,
                f"parameter {param['name']}",
                "static valid values require a proven current literal value-list "
                "source shape; control was omitted rather than exposing "
                "unrestricted data-driven choices",
            )
            return None
        else:
            flags.add(
                rname,
                f"parameter {param['name']}",
                "list parameter has no declared valid-values source; control was "
                "omitted rather than guessing a value-list column",
            )
            return None
        source_column = next((
            column for column in source_context.get("columns") or []
            if column.get("name") == source_field
        ), None)
        if source_column:
            ctl["source"] = {
                "kind": "source",
                "source": {
                    "kind": "table",
                    "elementId": source_context["id"],
                },
                "columnId": source_column["id"],
            }
        else:
            flags.add(rname, f"parameter {param['name']}",
                      "list control has no safely resolved value-list source and "
                      "was omitted; map it to a DM/base-table column or a manual source")
            return None
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


def _source_contexts(rep, dm_id, dm_element_ids, dm_element_index):
    """Build one base-table dependency per converted SSRS dataset."""
    rname = rep["report"]
    idx = dm_element_index.get(rname, {})
    if not idx:
        return {}

    contexts = {}
    single_dataset = len(idx) == 1
    for dataset_name, dm_meta in idx.items():
        if dm_element_ids and dm_meta["name"] in dm_element_ids:
            dm_el_server_id = dm_element_ids[dm_meta["name"]]
        else:
            dm_el_server_id = "{{%s_ID}}" % slug(
                dm_meta["elementId"]
            ).upper().replace("-", "_")
        base_id = (
            slug("base", rname)
            if single_dataset else slug("base", rname, dataset_name)
        )
        base_name = (
            f"{rname} Base"
            if single_dataset else f"{rname} · {dataset_name} Base"
        )
        base_cols = [
            {
                "id": slug("c", rname, dataset_name, fname),
                "name": fname,
                "formula": f"[{dm_meta['name']}/{fname}]",
            }
            for fname in dm_meta["fields"]
        ]
        contexts[dataset_name] = {
            "element": {
                "id": base_id,
                "kind": "table",
                "name": base_name,
                "source": {
                    "kind": "data-model",
                    "dataModelId": dm_id or "{{DATA_MODEL_ID}}",
                    "elementId": dm_el_server_id,
                },
                "columns": base_cols,
            },
            "id": base_id,
            "name": base_name,
            "columns": base_cols,
            "dataSetName": dataset_name,
        }
    return contexts


def _source_for_item(item, source_contexts, flags, rname):
    dataset_name = item.get("dataSetName")
    if dataset_name:
        source = source_contexts.get(dataset_name)
        if source is None:
            flags.add(
                rname,
                f"{item.get('kind')} {item.get('name')}",
                f"dataset {dataset_name!r} has no converted source context; "
                "item was omitted",
            )
        return source
    if len(source_contexts) == 1:
        return next(iter(source_contexts.values()))
    if not source_contexts:
        flags.add(
            rname,
            f"{item.get('kind')} {item.get('name')}",
            "item has no dataSetName and no converted dataset source is "
            "available; item was omitted",
        )
        return None
    flags.add(
        rname,
        f"{item.get('kind')} {item.get('name')}",
        "item has no dataSetName and the report has multiple converted "
        "datasets; item was omitted rather than guessing a source",
    )
    return None


def _flag_layout_degradations(report, target, flags):
    layout = report.get("layout") or {}
    signals = layout.get("signals") or {}
    break_inventory = layout.get("pageBreaks")
    if break_inventory is None:
        break_inventory = [
            page_break
            for section in layout.get("sections") or []
            for page_break in section.get("pageBreaks") or []
        ]
    if break_inventory:
        page_breaks = sum(
            str(item.get("disabled") or "").strip().casefold() != "true"
            for item in break_inventory
        )
        dynamic_breaks = sum(
            str(item.get("disabled") or "").strip().startswith("=")
            for item in break_inventory
        )
    else:
        page_breaks = int(signals.get("pageBreakCount") or 0)
        dynamic_breaks = 0
    lists = int(signals.get("listCount") or 0)
    if page_breaks:
        flags.add(
            report["report"], "page breaks",
            f"{page_breaks} explicit RDL page break(s) were captured but are not "
            f"automatically expanded into Sigma {target} pages; split and inspect manually",
        )
    if dynamic_breaks:
        flags.add(
            report["report"], "dynamic page breaks",
            f"{dynamic_breaks} RDL page break Disabled expression(s) require "
            "manual evaluation before target/layout approval",
        )
    if lists:
        flags.add(
            report["report"], "List data regions",
            f"{lists} RDL List region(s) were flattened to their recognized child "
            "items; repeating/banded semantics require manual parity review",
        )


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
        _flag_layout_degradations(rep, "workbook", flags)
        # Surface any parser warning (e.g. an unrecognized RDL layout wrapper)
        # and refuse to let a visual-less report convert silently — an empty
        # body with live datasets is a structural miss, not a clean report.
        for w in rep.get("warnings", []):
            flags.add(rname, "parse", w)
        if not rep.get("bodyItems") and (rep.get("dataSets") or rep.get("parameters")):
            flags.add(rname, "report body",
                      "no report visuals parsed — only dataset dependencies and "
                      "safe controls will be emitted; "
                      "check the RDL layout / ReportSections nesting before trusting this workbook")
        source_contexts = _source_contexts(
            rep, dm_id, dm_element_ids, dm_element_index
        )

        page_id = slug("page", rname)
        elements = [
            source["element"] for source in source_contexts.values()
        ]

        # controls
        for p in rep["parameters"]:
            control = _control(p, flags, rname, source_contexts)
            if control:
                elements.append(control)

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
                source = _source_for_item(
                    it, source_contexts, flags, rname
                )
                if source:
                    elements.append(_tablix_element(
                        it, rname, source["id"], source["name"], flags
                    ))
            elif kind == "chart":
                source = _source_for_item(
                    it, source_contexts, flags, rname
                )
                if source:
                    elements.append(_chart_element(
                        it, rname, source["id"], source["name"], flags
                    ))
            elif kind in ("gauge", "map", "subreport"):
                flags.add(rname, f"{kind} {it.get('name')}", it.get("flag", f"{kind} not auto-converted"))
                elements.append({
                    "id": slug(kind, rname, it.get("name")),
                    "kind": "text",
                    "body": f"**Not converted:** SSRS {kind} `{it.get('name')}`",
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
    spec = code_rep.wrap(document, {"name": name, "folderId": folder_id})
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
        shelf.append({"columnId": cid})
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
        dim_ids = (
            [d["columnId"] for d in rows_by]
            + [d["columnId"] for d in cols_by]
        )
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
# target selection and fixed-layout reports
# ---------------------------------------------------------------------------

_UNIT_TO_PX = {
    "px": 1.0,
    "in": 96.0,
    "cm": 96.0 / 2.54,
    "mm": 96.0 / 25.4,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
}


def _size_px(value, default=0.0):
    """Convert an RDL physical size to CSS pixels (96 px/in)."""
    if value in (None, ""):
        return float(default)
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]*)\s*",
        str(value),
    )
    if not match:
        raise ValueError(f"unsupported RDL size {value!r}")
    unit = (match.group(2) or "px").lower()
    if unit not in _UNIT_TO_PX:
        raise ValueError(f"unsupported RDL size unit {unit!r} in {value!r}")
    return float(match.group(1)) * _UNIT_TO_PX[unit]


def _xml_number(value):
    value = round(float(value), 3)
    return str(int(value)) if value.is_integer() else str(value)


def resolve_target(report):
    """Resolve one parsed report from objective print/dashboard signals."""
    layout = report.get("layout") or {}
    sections = layout.get("sections") or []
    signals = layout.get("signals") or {}
    print_score = 0
    dashboard_score = 0
    print_reasons = []
    dashboard_reasons = []

    break_inventory = layout.get("pageBreaks")
    if break_inventory is None:
        break_inventory = [
            page_break
            for section in sections
            for page_break in section.get("pageBreaks") or []
        ]
    if break_inventory:
        page_breaks = sum(
            str(item.get("disabled") or "").strip().casefold() != "true"
            for item in break_inventory
        )
        dynamic_breaks = sum(
            str(item.get("disabled") or "").strip().startswith("=")
            for item in break_inventory
        )
    else:
        page_breaks = int(signals.get("pageBreakCount") or 0)
        dynamic_breaks = 0
    lists = int(signals.get("listCount") or 0)
    subreports = int(signals.get("subreportCount") or 0)
    section_count = int(layout.get("sectionCount") or len(sections) or 1)
    if page_breaks:
        print_score += 6
        reason = f"{page_breaks} enabled/potential page break(s)"
        if dynamic_breaks:
            reason += f" ({dynamic_breaks} dynamic Disabled expression(s); manual)"
        print_reasons.append(reason)
    if lists:
        print_score += 4
        print_reasons.append(f"{lists} List data region(s)")
    if subreports:
        print_score += 4
        print_reasons.append(f"{subreports} subreport(s)")
    if section_count > 1:
        print_score += 4
        print_reasons.append(f"{section_count} report sections")
    if any(s.get("pageWidth") or s.get("pageHeight") for s in sections):
        print_score += 4
        print_reasons.append("explicit physical page dimensions")
    if any(s.get("footerHeight") for s in sections):
        print_score += 2
        print_reasons.append("page footer")
    if any(s.get("headerHeight") for s in sections):
        print_score += 1
        print_reasons.append("page header")
    if any(any((s.get("margins") or {}).values()) for s in sections):
        print_score += 2
        print_reasons.append("explicit page margins")

    charts = sum(i.get("kind") == "chart" for i in report.get("bodyItems", []))
    if charts:
        dashboard_score += min(4, charts * 2)
        dashboard_reasons.append(f"{charts} chart(s)")
    parameters = len(report.get("parameters") or [])
    if parameters:
        dashboard_score += min(2, parameters)
        dashboard_reasons.append(f"{parameters} interactive parameter(s)")
    if not print_reasons:
        dashboard_score += 1
        dashboard_reasons.append("no print-specific pagination signals")

    target = (
        "report"
        if print_score >= 4 and print_score > dashboard_score
        else "workbook"
    )
    return {
        "target": target,
        "printScore": print_score,
        "dashboardScore": dashboard_score,
        "printSignals": print_reasons,
        "dashboardSignals": dashboard_reasons,
    }


def _bundle_with_reports(bundle, reports):
    return {**bundle, "reports": list(reports)}


def resolve_bundle_targets(bundle, requested):
    decisions = []
    grouped = {"workbook": [], "report": []}
    for report in bundle["reports"]:
        evidence = resolve_target(report)
        target = evidence["target"] if requested == "auto" else requested
        decisions.append({
            "report": report["report"],
            "resolvedTarget": target,
            **evidence,
        })
        grouped[target].append(report)
    return grouped, decisions


def _section_margins(section):
    margins = section.get("margins") or {}
    return {
        side: _size_px(margins.get(side), 0)
        for side in ("top", "right", "bottom", "left")
    }


def _report_config(reports, flags):
    dimensions = []
    margins = []
    for report in reports:
        sections = (report.get("layout") or {}).get("sections") or [{}]
        for section in sections:
            section_margins = _section_margins(section)
            margins.extend(section_margins.values())
            report_width = _size_px(
                section.get("reportWidth")
                or (report.get("layout") or {}).get("reportWidth"),
                0,
            )
            body_height = _size_px(section.get("bodyHeight"), 0)
            header_height = _size_px(section.get("headerHeight"), 0)
            footer_height = _size_px(section.get("footerHeight"), 0)
            required_height = (
                body_height + header_height + footer_height
                + section_margins["top"] + section_margins["bottom"]
            )
            page_width = _size_px(
                section.get("pageWidth"),
                report_width + section_margins["left"] + section_margins["right"]
                if report_width else 816,
            )
            page_height = _size_px(
                section.get("pageHeight"),
                required_height
                if body_height else 1056,
            )
            dimensions.append((page_width, page_height, report["report"]))
            if section.get("pageHeight") and required_height > page_height + 0.001:
                flags.add(
                    report["report"],
                    f"section {int(section.get('index') or 0) + 1} pagination",
                    "RDL body plus panels/margins exceeds one physical page; "
                    "the draft preserves item coordinates but does not infer "
                    "dynamic overflow pages",
                )

            explicit = [value for value in section_margins.values() if value]
            if explicit and len({round(value, 3) for value in explicit}) > 1:
                flags.add(
                    report["report"],
                    f"section {int(section.get('index') or 0) + 1} margins",
                    "Sigma report config has one uniform margin; asymmetric RDL "
                    "margins are preserved in element coordinates but not in config",
                )

    widths = {round(item[0], 3) for item in dimensions}
    heights = {round(item[1], 3) for item in dimensions}
    if len(widths) > 1 or len(heights) > 1:
        for _, _, report_name in dimensions:
            flags.add(
                report_name,
                "report page size",
                "this output bundle contains mixed page sizes; Sigma report config "
                "is document-wide, so the largest canvas is used",
            )
    return {
        "pageWidth": math.ceil(max(widths or {816})),
        "pageHeight": math.ceil(max(heights or {1056})),
        "margin": math.ceil(max(margins or [0])),
    }


def _ancestor_offsets(report, item, region):
    section_index = int(item.get("sectionIndex") or 0)
    sections = (report.get("layout") or {}).get("sections") or []
    if section_index >= len(sections):
        return 0.0, 0.0
    entries = sections[section_index].get("itemPositions") or []
    for entry in entries:
        if (
            entry.get("region") == region
            and entry.get("name") == item.get("name")
            and entry.get("kind") == item.get("kind")
        ):
            ancestors = entry.get("ancestorPositions") or []
            return (
                sum(_size_px(p.get("left"), 0) for p in ancestors),
                sum(_size_px(p.get("top"), 0) for p in ancestors),
            )
    return 0.0, 0.0


def _report_box(report, item, section, region, config, flags):
    position = item.get("position") or {}
    ancestor_left, ancestor_top = _ancestor_offsets(report, item, region)
    margins = _section_margins(section)
    panel_height = _size_px(
        section.get("headerHeight") if region == "header"
        else section.get("footerHeight"),
        0,
    )
    default_heights = {
        "textbox": 32, "chart": 240, "tablix": 192,
        "gauge": 64, "map": 240, "subreport": 96,
    }
    left = _size_px(position.get("left"), 0) + ancestor_left
    top = _size_px(position.get("top"), 0) + ancestor_top
    left_margin = max(margins["left"], float(config.get("margin") or 0))
    right_margin = max(margins["right"], float(config.get("margin") or 0))
    x = left_margin + left
    if region == "body":
        top_margin = max(margins["top"], float(config.get("margin") or 0))
        bottom_margin = max(
            margins["bottom"], float(config.get("margin") or 0)
        )
        footer_height = _size_px(section.get("footerHeight"), 0)
        y = top_margin + _size_px(section.get("headerHeight"), 0) + top
        available_height = (
            config["pageHeight"] - y - bottom_margin - footer_height
        )
    else:
        y = top
        available_height = panel_height - y
    width = _size_px(
        position.get("width"),
        max(1, config["pageWidth"] - x - right_margin),
    )
    height = _size_px(
        position.get("height"),
        min(default_heights.get(item.get("kind"), 48), max(1, available_height)),
    )
    missing = [
        key for key in ("top", "left", "width", "height")
        if not position.get(key)
    ]
    if missing:
        flags.add(
            report["report"],
            f"{region} {item.get('kind')} {item.get('name')}",
            "RDL omitted " + ", ".join(missing)
            + "; deterministic fallback geometry was used",
        )
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(
            f"{report['report']}/{item.get('name')}: invalid report geometry"
        )
    return {"x": x, "y": y, "width": width, "height": height}


def _layout_element(element_id, box):
    attrs = " ".join(
        f'{key}="{_xml_number(box[key])}"'
        for key in ("x", "y", "width", "height")
    )
    return f'  <Element elementId="{element_id}" {attrs}/>'


def _static_text_element(item, rname, region, flags):
    value = item.get("value")
    if not value:
        flags.add(rname, f"{region} textbox {item.get('name')}",
                  "empty textbox omitted")
        return None
    if str(value).startswith("="):
        flags.add(
            rname,
            f"{region} textbox {item.get('name')}",
            "dynamic SSRS textbox expression is not safely authorable as report "
            "text and was omitted",
        )
        return None
    return {
        "id": slug("txt", rname, region, item.get("sectionIndex"), item.get("name")),
        "kind": "text",
        "body": value,
    }


def build_report(bundle, dm_id, dm_element_ids, dm_element_index,
                 name, folder_id, flags, schema_version=1):
    """Build a Sigma fixed-layout report spec from parsed RDL geometry."""
    config = _report_config(bundle["reports"], flags)
    elements = []
    pages = []
    panels = []
    roots = []

    for report in bundle["reports"]:
        rname = report["report"]
        _flag_layout_degradations(report, "report", flags)
        source_contexts = _source_contexts(
            report, dm_id, dm_element_ids, dm_element_index
        )
        sections = (report.get("layout") or {}).get("sections") or [
            {
                "index": 0, "margins": {}, "headerHeight": None,
                "footerHeight": None,
            }
        ]
        page_ids = {}
        for section in sections:
            section_index = int(section.get("index") or 0)
            page_id = slug("page", rname, section_index + 1)
            page_ids[section_index] = page_id
            pages.append({"id": page_id, "name": (
                rname if len(sections) == 1
                else f"{rname} · Section {section_index + 1}"
            )})
            page_lines = [f'<Page id="{page_id}">']
            for item in report.get("bodyItems") or []:
                if int(item.get("sectionIndex") or 0) != section_index:
                    continue
                kind = item.get("kind")
                element = None
                if kind == "textbox":
                    element = _static_text_element(item, rname, "body", flags)
                elif kind == "tablix":
                    source = _source_for_item(
                        item, source_contexts, flags, rname
                    )
                    if source:
                        element = _tablix_element(
                            item, rname, source["id"], source["name"], flags
                        )
                elif kind == "chart":
                    source = _source_for_item(
                        item, source_contexts, flags, rname
                    )
                    if source:
                        report_chart_kind = CHART_KIND.get(
                            (item.get("chartType") or "column").lower()
                        )
                        if report_chart_kind not in REPORT_SAFE_CHART_KINDS:
                            flags.add(
                                rname, f"chart {item.get('name')}",
                                f"{report_chart_kind or item.get('chartType')} is not "
                                "in the conservative Sigma report authoring baseline; "
                                "chart was omitted",
                            )
                        else:
                            element = _chart_element(
                                item, rname, source["id"], source["name"], flags
                            )
                else:
                    flags.add(
                        rname, f"{kind} {item.get('name')}",
                        item.get("flag", f"{kind} is unsupported and was omitted"),
                    )
                if element:
                    elements.append(element)
                    box = _report_box(
                        report, item, section, "body", config, flags
                    )
                    page_lines.append(_layout_element(element["id"], box))
            page_lines.append("</Page>")
            roots.append("\n".join(page_lines))

            for panel_type, source_items, height_key in (
                ("header", report.get("pageHeaderItems") or [], "headerHeight"),
                ("footer", report.get("pageFooterItems") or [], "footerHeight"),
            ):
                panel_height = _size_px(section.get(height_key), 0)
                panel_items = [
                    item for item in source_items
                    if int(item.get("sectionIndex") or 0) == section_index
                ]
                if not panel_height and not panel_items:
                    continue
                if not panel_height:
                    panel_height = 48
                    flags.add(
                        rname, f"section {section_index + 1} {panel_type}",
                        "panel items exist without an RDL height; 48px was used",
                    )
                panel_id = slug("panel", rname, section_index + 1, panel_type)
                panels.append({
                    "id": panel_id,
                    "type": panel_type,
                    "title": f"{rname} {panel_type}",
                    "pages": [page_id],
                    "config": {"height": round(panel_height, 3)},
                })
                panel_lines = [
                    f'<Panel id="{panel_id}" type="{panel_type}">'
                ]
                for item in panel_items:
                    element = _static_text_element(
                        item, rname, panel_type, flags
                    )
                    if not element:
                        continue
                    elements.append(element)
                    box = _report_box(
                        report, item, section, panel_type, config, flags
                    )
                    panel_lines.append(_layout_element(element["id"], box))
                panel_lines.append("</Panel>")
                roots.append("\n".join(panel_lines))

        dependency_elements = [
            source["element"] for source in source_contexts.values()
        ]
        for parameter in report.get("parameters") or []:
            control = _control(
                parameter, flags, rname, source_contexts
            )
            if control:
                dependency_elements.append(control)
        if dependency_elements:
            dependency_page_id = slug("page", rname, "dependencies")
            pages.append({
                "id": dependency_page_id,
                "name": f"{rname} · Dependencies",
                "visibility": "hidden",
            })
            dependency_lines = [f'<Page id="{dependency_page_id}">']
            inset = float(config.get("margin") or 0)
            usable_width = max(1, config["pageWidth"] - 2 * inset)
            usable_height = max(1, config["pageHeight"] - 2 * inset)
            max_rows = max(1, int(usable_height // 64))
            dependency_columns = max(
                1, math.ceil(len(dependency_elements) / max_rows)
            )
            dependency_rows = max(
                1, math.ceil(len(dependency_elements) / dependency_columns)
            )
            cell_width = usable_width / dependency_columns
            cell_height = usable_height / dependency_rows
            for index, element in enumerate(dependency_elements):
                elements.append(element)
                column = index % dependency_columns
                row = index // dependency_columns
                box = {
                    "x": inset + column * cell_width,
                    "y": inset + row * cell_height,
                    "width": max(1, cell_width - 16),
                    "height": max(1, cell_height - 8),
                }
                dependency_lines.append(_layout_element(element["id"], box))
            dependency_lines.append("</Page>")
            roots.append("\n".join(dependency_lines))

    document = {
        "schemaVersion": schema_version,
        "kind": "report",
        "config": config,
        "elements": elements,
        "pages": pages,
        "panels": panels,
        "layout": (
            '<?xml version="1.0" encoding="utf-8"?>\n' + "\n".join(roots)
        ),
    }
    spec = {"name": name, "folderId": folder_id, "document": document}
    _validate_report(spec)
    return spec


def _validate_report(spec):
    """Local report guard: flat elements, absolute bounds, exact placement."""
    doc = spec["document"]
    if doc.get("kind") != "report":
        raise ValueError("report spec: document.kind must be report")
    page_count = len(doc.get("pages") or [])
    if page_count > 1000:
        raise ValueError(
            f"report spec: {page_count} pages exceeds the 1,000-page limit"
        )
    if re.search(
        r"gridColumn|gridRow|gridTemplate|<(?:Container|TabbedContainer|Overlay)\b",
        doc.get("layout", ""),
    ):
        raise ValueError("report spec: workbook grid/container syntax is forbidden")

    element_ids = [item.get("id") for item in doc.get("elements") or []]
    if len(element_ids) != len(set(element_ids)):
        raise ValueError("report spec: duplicate element ids")
    page_ids = {page["id"] for page in doc.get("pages") or []}
    panel_by_id = {
        panel["id"]: panel for panel in doc.get("panels") or []
    }
    panels_by_page = {page_id: {"header": [], "footer": []}
                      for page_id in page_ids}
    for panel in panel_by_id.values():
        panel_type = panel.get("type")
        if panel_type not in ("header", "footer"):
            raise ValueError(
                f"report spec: unsupported panel type {panel_type!r}"
            )
        for page_id in panel.get("pages") or []:
            if page_id not in panels_by_page:
                raise ValueError(
                    f"report spec: panel references unknown page {page_id!r}"
                )
            panels_by_page[page_id][panel_type].append(panel)
    for page_id, assignments in panels_by_page.items():
        if any(len(values) > 1 for values in assignments.values()):
            raise ValueError(
                f"report spec: page {page_id!r} has duplicate header/footer panels"
            )
    fragment = re.sub(
        r"^\s*<\?xml[^>]*\?>", "", doc.get("layout", ""), count=1
    )
    try:
        root = ET.fromstring(f"<ReportLayout>{fragment}</ReportLayout>")
    except ET.ParseError as exc:
        raise ValueError(f"report spec: invalid layout XML: {exc}") from exc

    placed = []
    seen_pages = set()
    seen_panels = set()
    for layout_root in root:
        if layout_root.tag == "Page":
            root_id = layout_root.get("id")
            if root_id not in page_ids:
                raise ValueError(
                    f"report spec: layout references unknown page {root_id!r}"
                )
            seen_pages.add(root_id)
            bound_width = float(doc["config"]["pageWidth"])
            bound_height = float(doc["config"]["pageHeight"])
            margin = float(doc["config"].get("margin") or 0)
            header_height = sum(
                float((panel.get("config") or {}).get("height") or 0)
                for panel in panels_by_page[root_id]["header"]
            )
            footer_height = sum(
                float((panel.get("config") or {}).get("height") or 0)
                for panel in panels_by_page[root_id]["footer"]
            )
            content_bounds = (
                margin,
                margin + header_height,
                bound_width - margin,
                bound_height - margin - footer_height,
            )
        elif layout_root.tag == "Panel":
            root_id = layout_root.get("id")
            panel = panel_by_id.get(root_id)
            if not panel:
                raise ValueError(
                    f"report spec: layout references unknown panel {root_id!r}"
                )
            if layout_root.get("type") != panel.get("type"):
                raise ValueError(f"report spec: panel type mismatch for {root_id}")
            seen_panels.add(root_id)
            bound_width = float(doc["config"]["pageWidth"])
            bound_height = float((panel.get("config") or {}).get("height") or 0)
            content_bounds = None
        else:
            raise ValueError(
                f"report spec: unsupported layout root <{layout_root.tag}>"
            )
        for leaf in layout_root:
            if leaf.tag != "Element" or list(leaf):
                raise ValueError("report spec: layout leaves must be flat <Element>")
            element_id = leaf.get("elementId")
            placed.append(element_id)
            try:
                x, y, width, height = (
                    float(leaf.get(key)) for key in ("x", "y", "width", "height")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"report spec: non-numeric geometry for {element_id}"
                ) from exc
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                raise ValueError(
                    f"report spec: invalid geometry for {element_id}"
                )
            if x + width > bound_width + 0.001 or y + height > bound_height + 0.001:
                raise ValueError(
                    f"report spec: {element_id} exceeds {layout_root.tag.lower()} bounds"
                )
            if content_bounds:
                left, top, right, bottom = content_bounds
                if (
                    x < left - 0.001
                    or y < top - 0.001
                    or x + width > right + 0.001
                    or y + height > bottom + 0.001
                ):
                    raise ValueError(
                        f"report spec: {element_id} overlaps a page margin "
                        "or assigned header/footer"
                    )

    if seen_pages != page_ids:
        raise ValueError(
            f"report spec: pages missing from layout {sorted(page_ids - seen_pages)}"
        )
    if seen_panels != set(panel_by_id):
        raise ValueError(
            "report spec: panels missing from layout "
            f"{sorted(set(panel_by_id) - seen_panels)}"
        )
    if sorted(placed) != sorted(element_ids):
        missing = set(element_ids) - set(placed)
        dangling = set(placed) - set(element_ids)
        duplicates = sorted({
            item for item in placed if placed.count(item) > 1
        })
        raise ValueError(
            "report spec: placement mismatch "
            f"(missing={sorted(missing)}, dangling={sorted(dangling)}, "
            f"duplicates={duplicates})"
        )


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


_TARGET_OUTPUT_SUFFIXES = (
    "_workbook_spec.json",
    "_report_spec.json",
    "_target_resolution.json",
)


def _stage_text_output(path, content):
    """Write and fsync one temporary file beside its final destination."""
    destination = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(destination)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    return destination, temp_path


def _replace_outputs_transactionally(outputs, obsolete_paths=()):
    """Replace a complete output set, then remove obsolete target artifacts.

    Every payload is built and staged before any destination is touched.
    Existing affected files are backed up so an I/O failure during the commit
    can restore the preceding valid output set.
    """
    normalized_outputs = {
        os.path.abspath(os.fspath(path)): content
        for path, content in outputs.items()
    }
    obsolete = {
        os.path.abspath(os.fspath(path))
        for path in obsolete_paths
    } - set(normalized_outputs)
    affected = sorted(set(normalized_outputs) | obsolete)
    staged = {}
    backups = {}
    existed = {}
    mutations_started = False
    try:
        for path, content in normalized_outputs.items():
            destination, temp_path = _stage_text_output(path, content)
            staged[destination] = temp_path

        for path in affected:
            existed[path] = os.path.isfile(path)
            if existed[path]:
                fd, backup_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(path)}.",
                    suffix=".bak",
                    dir=os.path.dirname(path),
                )
                os.close(fd)
                shutil.copy2(path, backup_path)
                backups[path] = backup_path

        mutations_started = True
        for path, temp_path in staged.items():
            os.replace(temp_path, path)
        for path in obsolete:
            if os.path.isfile(path):
                os.remove(path)
    except Exception:
        if mutations_started:
            for path in affected:
                try:
                    if existed.get(path):
                        backup_path = backups.pop(path, None)
                        if backup_path:
                            os.replace(backup_path, path)
                    elif os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    # Preserve the original exception; any rollback failure is
                    # still visible through the surviving backup/temp file.
                    pass
        raise
    finally:
        for temp_path in list(staged.values()) + list(backups.values()):
            if os.path.exists(temp_path):
                os.remove(temp_path)


def _json_output(value):
    return json.dumps(value, indent=2) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Convert SSRS bundle.json to Sigma specs")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--connection-id", required=True, help="Sigma warehouse connection id")
    ap.add_argument("--folder-id", required=True, help="destination Sigma folder id")
    ap.add_argument("--dm-name", default="SSRS Migration")
    ap.add_argument("--wb-name", default="SSRS Migration")
    ap.add_argument("--report-name",
                    help="Sigma report name (defaults to --wb-name)")
    ap.add_argument(
        "--report-schema-version",
        type=int,
        default=1,
        help="report document schemaVersion (offline default: 1)",
    )
    ap.add_argument(
        "--target",
        choices=("auto", "workbook", "report"),
        default="workbook",
        help="output resource; omitted remains workbook for compatibility",
    )
    ap.add_argument("--data-model-id", help="server dataModelId (phase 4)")
    ap.add_argument("--dm-element-ids", help="JSON {elementName: serverId} read back after DM POST")
    ap.add_argument("--no-uppercase", action="store_true",
                    help="do NOT uppercase Custom SQL column refs (default assumes Snowflake)")
    ap.add_argument("--out-prefix", default="sigma")
    args = ap.parse_args()
    if args.report_schema_version < 1:
        ap.error("--report-schema-version must be a positive integer")

    with open(args.bundle) as fh:
        bundle = json.load(fh)
    dm_element_ids = None
    if args.dm_element_ids:
        with open(args.dm_element_ids) as fh:
            dm_element_ids = json.load(fh)

    flags = Flags()
    dm_spec, dm_index = build_dm(bundle, args.connection_id, args.dm_name,
                                 args.folder_id, not args.no_uppercase, flags)
    grouped, decisions = resolve_bundle_targets(bundle, args.target)
    wb_spec = None
    report_spec = None
    if grouped["workbook"]:
        wb_spec = build_workbook(
            _bundle_with_reports(bundle, grouped["workbook"]),
            args.data_model_id, dm_element_ids, dm_index,
            args.wb_name, args.folder_id, flags,
        )
    if grouped["report"]:
        report_spec = build_report(
            _bundle_with_reports(bundle, grouped["report"]),
            args.data_model_id, dm_element_ids, dm_index,
            args.report_name or args.wb_name, args.folder_id, flags,
            schema_version=args.report_schema_version,
        )
    parity = build_parity_keys(bundle)

    target_paths = {
        suffix: f"{args.out_prefix}{suffix}"
        for suffix in _TARGET_OUTPUT_SUFFIXES
    }
    dm_path = f"{args.out_prefix}_dm_spec.json"
    outputs = {dm_path: _json_output(dm_spec)}
    written = [dm_path]
    if wb_spec is not None:
        path = target_paths["_workbook_spec.json"]
        outputs[path] = _json_output(wb_spec)
        written.append(path)
    if report_spec is not None:
        path = target_paths["_report_spec.json"]
        outputs[path] = _json_output(report_spec)
        written.append(path)
    if args.target == "auto":
        path = target_paths["_target_resolution.json"]
        outputs[path] = _json_output({
            "requestedTarget": args.target,
            "reports": decisions,
        })
        written.append(path)
    outputs["parity_keys.json"] = _json_output(parity)
    outputs["conversion_report.md"] = flags.report_md()
    obsolete_paths = set(target_paths.values()) - set(outputs)
    _replace_outputs_transactionally(outputs, obsolete_paths)

    summary = [f"DM elements: {len(dm_spec['pages'][0]['elements'])}"]
    if wb_spec:
        wb_doc = wb_spec["document"]
        summary.extend([
            f"workbook pages: {len(wb_doc['pages'])}",
            f"workbook elements: {len(wb_doc['elements'])}",
        ])
    if report_spec:
        report_doc = report_spec["document"]
        summary.extend([
            f"report pages: {len(report_doc['pages'])}",
            f"report elements: {len(report_doc['elements'])}",
        ])
    summary.append(f"flags: {len(flags.items)}")
    print("  ".join(summary))
    print("wrote " + ", ".join(
        written + ["parity_keys.json", "conversion_report.md"]
    ))
    if flags.items:
        print("\n--- flags (also in conversion_report.md) ---")
        for r, w, m in flags.items:
            print(f"  [{r}] {w}: {m}")


if __name__ == "__main__":
    main()
