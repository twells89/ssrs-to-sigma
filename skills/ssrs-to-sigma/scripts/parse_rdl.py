#!/usr/bin/env python3
"""
parse_rdl.py — Phase 1 of ssrs-to-sigma.

Parse one or more SSRS RDL (Report Definition Language) XML files into a
normalized `bundle.json` — the converter contract that convert.py consumes.

RDL is a namespaced XML format
(http://schemas.microsoft.com/sqlserver/reporting/<year>/.../reportdefinition).
The element tree is stable across the 2008/2010/2016 schema years; the only
thing that changes is the namespace URI, so we strip namespaces and walk by
local tag name. RDLC (the local/client variant) is identical minus the
<DataSources> block — this parser handles both.

Usage:
    python3 parse_rdl.py REPORT.rdl [MORE.rdl ...] -o bundle.json
    python3 parse_rdl.py --dir ssrs-export/reports -o bundle.json

stdlib only.
"""
import argparse
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# namespace-agnostic XML helpers
# ---------------------------------------------------------------------------

def _local(tag):
    """Strip the {namespace} prefix ElementTree prepends to every tag."""
    return tag.rsplit("}", 1)[-1]


def child(el, name):
    """First direct child with the given local tag name, or None."""
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def children(el, name):
    """All direct children with the given local tag name."""
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def descendants(el, name):
    """All descendants (any depth) with the given local tag name."""
    if el is None:
        return []
    return [c for c in el.iter() if _local(c.tag) == name]


def text(el, name=None):
    """Text of `el` itself (name=None) or of its first child `name`."""
    node = el if name is None else child(el, name)
    if node is None or node.text is None:
        return None
    return node.text.strip()


# ---------------------------------------------------------------------------
# section parsers
# ---------------------------------------------------------------------------

def parse_data_sources(root):
    out = []
    for ds in descendants(child(root, "DataSources"), "DataSource"):
        cp = child(ds, "ConnectionProperties")
        out.append({
            "name": ds.get("Name"),
            "reference": text(ds, "DataSourceReference"),
            "provider": text(cp, "DataProvider") if cp is not None else None,
            "connectString": text(cp, "ConnectString") if cp is not None else None,
        })
    return out


def parse_datasets(root):
    out = []
    for dsel in descendants(child(root, "DataSets"), "DataSet"):
        query = child(dsel, "Query")
        command = text(query, "CommandText") if query is not None else None
        command_type = text(query, "CommandType") if query is not None else None
        # CommandType "StoredProcedure" => the CommandText is a proc name
        is_proc = (command_type or "").lower() == "storedprocedure"

        qparams = []
        for qp in descendants(child(query, "QueryParameters") if query is not None else None,
                              "QueryParameter"):
            qparams.append({"name": qp.get("Name"), "value": text(qp, "Value")})

        fields = []
        for f in descendants(child(dsel, "Fields"), "Field"):
            datafield = text(f, "DataField")
            value = text(f, "Value")     # calculated field (expression)
            typename = None
            for c in f:
                if _local(c.tag) == "TypeName":
                    typename = (c.text or "").strip()
            fields.append({
                "name": f.get("Name"),
                "dataField": datafield,
                "expression": value,                 # None for plain DB fields
                "calculated": value is not None,
                "typeName": typename,
            })

        out.append({
            "name": dsel.get("Name"),
            "dataSourceName": text(query, "DataSourceName") if query is not None else None,
            "commandText": command,
            "isStoredProc": is_proc,
            "queryParameters": qparams,
            "fields": fields,
        })
    return out


def parse_parameters(root):
    out = []
    for p in descendants(child(root, "ReportParameters"), "ReportParameter"):
        default_vals = [text(v) for v in descendants(child(p, "DefaultValue"), "Value")]
        valid = child(p, "ValidValues")
        valid_ref = None
        valid_static = None
        if valid is not None:
            dsref = child(valid, "DataSetReference")
            if dsref is not None:
                valid_ref = {
                    "dataSet": text(dsref, "DataSetName"),
                    "valueField": text(dsref, "ValueField"),
                    "labelField": text(dsref, "LabelField"),
                }
            else:
                pvs = child(valid, "ParameterValues")
                if pvs is not None:
                    valid_static = [text(pv, "Value") or text(pv)
                                    for pv in children(pvs, "ParameterValue")]
        out.append({
            "name": p.get("Name"),
            "dataType": text(p, "DataType"),
            "multiValue": (text(p, "MultiValue") or "false").lower() == "true",
            "nullable": (text(p, "Nullable") or "false").lower() == "true",
            "prompt": text(p, "Prompt"),
            "defaultValues": [v for v in default_vals if v is not None],
            "validValuesQuery": valid_ref,
            "validValuesStatic": valid_static,
        })
    return out


def _group_expressions(member):
    grp = child(member, "Group")
    if grp is None:
        return []
    return [text(g) for g in descendants(child(grp, "GroupExpressions"), "GroupExpression")]


def _hierarchy_groups(hierarchy):
    """Collect the (named) group expressions in a Tablix row/column hierarchy."""
    groups = []
    for member in descendants(child(hierarchy, "TablixMembers"), "TablixMember"):
        exprs = _group_expressions(member)
        if exprs:
            grp = child(member, "Group")
            groups.append({"name": grp.get("Name") if grp is not None else None,
                           "expressions": exprs})
    return groups


def _aggregate_cells(tablix):
    """The value (aggregate) expressions in the Tablix body cells."""
    body = child(tablix, "TablixBody")
    vals = []
    for tb in descendants(body, "Textbox"):
        for run in descendants(tb, "TextRun"):
            v = text(run, "Value")
            if v and v.startswith("="):
                vals.append(v)
    return vals


def parse_tablix(tablix):
    row_groups = _hierarchy_groups(child(tablix, "TablixRowHierarchy"))
    col_groups = _hierarchy_groups(child(tablix, "TablixColumnHierarchy"))
    # Shape: matrix when there are BOTH row and column groups; otherwise table.
    if row_groups and col_groups:
        shape = "matrix"
    elif row_groups or col_groups:
        shape = "table"      # grouped table
    else:
        shape = "table"      # flat detail table
    return {
        "kind": "tablix",
        "name": tablix.get("Name"),
        "shape": shape,
        "dataSetName": text(tablix, "DataSetName"),
        "rowGroups": row_groups,
        "columnGroups": col_groups,
        "valueExpressions": _aggregate_cells(tablix),
        "position": {
            "top": text(tablix, "Top"), "left": text(tablix, "Left"),
            "width": text(tablix, "Width"), "height": text(tablix, "Height"),
        },
    }


def parse_chart(chart):
    cat = []
    for member in descendants(child(chart, "ChartCategoryHierarchy"), "ChartMember"):
        cat += _group_expressions(member)
    series = []
    chart_type = None
    for s in descendants(child(chart, "ChartSeriesCollection"), "ChartSeries"):
        st = None
        for dp in descendants(s, "ChartDataPoint"):
            for c in dp:
                pass
        # Type sits on the series in 2008R2+ schema
        st = text(s, "Type")
        if st:
            chart_type = st
        yvals = [text(y) for y in descendants(s, "Y")]
        series.append({"name": s.get("Name"), "values": [y for y in yvals if y]})
    return {
        "kind": "chart",
        "name": chart.get("Name"),
        "chartType": chart_type or "Column",
        "dataSetName": text(chart, "DataSetName"),
        "categoryExpressions": cat,
        "series": series,
        "position": {
            "top": text(chart, "Top"), "left": text(chart, "Left"),
            "width": text(chart, "Width"), "height": text(chart, "Height"),
        },
    }


def parse_report_items(container):
    """Walk a <ReportItems> container for the element kinds we convert."""
    items = []
    ri = child(container, "ReportItems")
    if ri is None:
        return items
    for el in ri:
        tag = _local(el.tag)
        if tag == "Tablix":
            items.append(parse_tablix(el))
        elif tag == "Chart":
            items.append(parse_chart(el))
        elif tag == "Gauge":
            items.append({"kind": "gauge", "name": el.get("Name"),
                          "flag": "gauge -> KPI substitution (refs/viz-type-mapping.md)"})
        elif tag == "Map":
            items.append({"kind": "map", "name": el.get("Name"),
                          "flag": "map -> region/point map, manual review"})
        elif tag == "Subreport":
            items.append({"kind": "subreport", "name": el.get("Name"),
                          "reportName": text(el, "ReportName"),
                          "flag": "subreport -> separate page / drillthrough, multi-pass"})
        elif tag == "Textbox":
            v = None
            for run in descendants(el, "TextRun"):
                v = text(run, "Value")
                if v:
                    break
            items.append({"kind": "textbox", "name": el.get("Name"), "value": v})
        elif tag in ("Rectangle", "List"):
            # containers can nest report items
            items.extend(parse_report_items(el))
    return items


def parse_rdl(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if _local(root.tag) != "Report":
        raise ValueError(f"{path}: root element is <{_local(root.tag)}>, not <Report> — not an RDL file")

    body = child(root, "Body")
    page = child(root, "Page")
    name = os.path.splitext(os.path.basename(path))[0]

    return {
        "report": name,
        "sourceFile": os.path.basename(path),
        "dataSources": parse_data_sources(root),
        "dataSets": parse_datasets(root),
        "parameters": parse_parameters(root),
        "bodyItems": parse_report_items(body),
        "pageHeaderItems": parse_report_items(child(page, "PageHeader")) if page is not None else [],
        "pageFooterItems": parse_report_items(child(page, "PageFooter")) if page is not None else [],
    }


def main():
    ap = argparse.ArgumentParser(description="Parse SSRS RDL files into bundle.json")
    ap.add_argument("files", nargs="*", help="RDL files")
    ap.add_argument("--dir", help="directory of .rdl files (recursive)")
    ap.add_argument("-o", "--out", default="bundle.json")
    args = ap.parse_args()

    paths = list(args.files)
    if args.dir:
        paths += glob.glob(os.path.join(args.dir, "**", "*.rdl"), recursive=True)
    if not paths:
        ap.error("no RDL files given (pass files or --dir)")

    reports = []
    for p in sorted(set(paths)):
        try:
            reports.append(parse_rdl(p))
        except Exception as e:  # noqa: BLE001 — keep going across a batch
            print(f"!! skipped {p}: {e}", file=sys.stderr)

    bundle = {"version": 1, "reports": reports}
    with open(args.out, "w") as fh:
        json.dump(bundle, fh, indent=2)
    print(f"parsed {len(reports)} report(s) -> {args.out}")


if __name__ == "__main__":
    main()
