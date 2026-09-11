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


def _position(el):
    """Return the RDL box without interpreting its physical unit."""
    return {
        "top": text(el, "Top"), "left": text(el, "Left"),
        "width": text(el, "Width"), "height": text(el, "Height"),
    }


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
        "position": _position(tablix),
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
        "position": _position(chart),
    }


def parse_report_items(container, section_index=0, region="body", inventory=None,
                       ancestors=None):
    """Walk a <ReportItems> container for the element kinds we convert."""
    items = []
    inventory = inventory if inventory is not None else []
    ancestors = list(ancestors or [])
    ri = child(container, "ReportItems")
    if ri is None:
        return items
    for el in ri:
        tag = _local(el.tag)
        position = _position(el)
        entry = {
            "sectionIndex": section_index,
            "region": region,
            "kind": tag.lower(),
            "name": el.get("Name"),
            "position": position,
        }
        if ancestors:
            entry["ancestorPositions"] = ancestors
        inventory.append(entry)
        if tag == "Tablix":
            item = parse_tablix(el)
            item["sectionIndex"] = section_index
            items.append(item)
        elif tag == "Chart":
            item = parse_chart(el)
            item["sectionIndex"] = section_index
            items.append(item)
        elif tag == "Gauge":
            items.append({"kind": "gauge", "name": el.get("Name"),
                          "sectionIndex": section_index, "position": position,
                          "flag": "gauge -> KPI substitution (refs/viz-type-mapping.md)"})
        elif tag == "Map":
            items.append({"kind": "map", "name": el.get("Name"),
                          "sectionIndex": section_index, "position": position,
                          "flag": "map -> region/point map, manual review"})
        elif tag == "Subreport":
            items.append({"kind": "subreport", "name": el.get("Name"),
                          "sectionIndex": section_index, "position": position,
                          "reportName": text(el, "ReportName"),
                          "flag": "subreport -> separate page / drillthrough, multi-pass"})
        elif tag == "Textbox":
            v = None
            for run in descendants(el, "TextRun"):
                v = text(run, "Value")
                if v:
                    break
            items.append({"kind": "textbox", "name": el.get("Name"), "value": v,
                          "sectionIndex": section_index, "position": position})
        elif tag in ("Rectangle", "List"):
            # containers can nest report items
            items.extend(parse_report_items(
                el, section_index, region, inventory, ancestors + [position]
            ))
    return items


def _iter_layouts(root):
    """Yield the (Body, Page, owner) tuples that carry report visuals.

    RDL 2016+ (2016/01 schema, SSDT / Power BI Report Server) wraps the layout
    in <ReportSections><ReportSection><Body>/<Page>, and a report may carry
    MORE THAN ONE <ReportSection>. Older 2008/2010 RDL puts a single <Body>/
    <Page> directly under <Report>. This is a structural nesting axis — NOT the
    namespace axis _local() already strips — so walk into the sections when they
    exist, else fall back to the flat root layout. Missing this walk is how
    every Tablix/Chart/Subreport in a 2016 export is silently lost.
    """
    sections = children(child(root, "ReportSections"), "ReportSection")
    if sections:
        for sec in sections:
            yield child(sec, "Body"), child(sec, "Page"), sec
    else:
        yield child(root, "Body"), child(root, "Page"), root


def _page_breaks(container):
    """Inventory explicit pagination directives without changing bodyItems."""
    return [
        {
            "breakLocation": text(node, "BreakLocation"),
            "disabled": text(node, "Disabled"),
            "resetPageNumber": text(node, "ResetPageNumber"),
        }
        for node in descendants(container, "PageBreak")
    ]


def _page_break_state(page_break):
    disabled = str(page_break.get("disabled") or "").strip()
    if disabled.casefold() == "true":
        return "disabled"
    if disabled.startswith("="):
        return "dynamic"
    return "active"


def _section_layout(root, owner, body, page, section_index, item_positions):
    header = child(page, "PageHeader")
    footer = child(page, "PageFooter")
    lists = descendants(body, "List")
    subreports = descendants(body, "Subreport")
    return {
        "index": section_index,
        "reportWidth": text(owner, "Width") or text(root, "Width"),
        "bodyHeight": text(body, "Height"),
        "pageWidth": text(page, "PageWidth"),
        "pageHeight": text(page, "PageHeight"),
        "margins": {
            "top": text(page, "TopMargin"),
            "right": text(page, "RightMargin"),
            "bottom": text(page, "BottomMargin"),
            "left": text(page, "LeftMargin"),
        },
        "headerHeight": text(header, "Height"),
        "footerHeight": text(footer, "Height"),
        "pageBreaks": _page_breaks(body),
        "listCount": len(lists),
        "subreportCount": len(subreports),
        "itemPositions": [
            item for item in item_positions
            if item["sectionIndex"] == section_index
        ],
    }


def parse_rdl(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if _local(root.tag) != "Report":
        raise ValueError(f"{path}: root element is <{_local(root.tag)}>, not <Report> — not an RDL file")

    name = os.path.splitext(os.path.basename(path))[0]

    body_items = []
    page_header_items = []
    page_footer_items = []
    item_positions = []
    section_layouts = []
    layout_found = False
    # Concatenate items across every ReportSection (multi-section 2016 reports)
    # and across the flat legacy layout — one flat inventory per report.
    layouts = list(_iter_layouts(root))
    for section_index, (body, page, owner) in enumerate(layouts):
        if body is not None or page is not None:
            layout_found = True
        body_items.extend(parse_report_items(
            body, section_index, "body", item_positions
        ))
        if page is not None:
            page_header_items.extend(parse_report_items(
                child(page, "PageHeader"), section_index, "header", item_positions
            ))
            page_footer_items.extend(parse_report_items(
                child(page, "PageFooter"), section_index, "footer", item_positions
            ))
        section_layouts.append(
            _section_layout(
                root, owner, body, page, section_index, item_positions
            )
        )

    if not layout_found:
        # No <Body>/<Page> under <Report> OR any <ReportSection> — this is a
        # malformed/unsupported RDL, not a genuinely empty report. Fail loudly.
        raise ValueError(
            f"{path}: no <Body>/<Page> found under <Report> or <ReportSections> — "
            "malformed RDL or an unsupported layout wrapper"
        )

    datasets = parse_datasets(root)
    parameters = parse_parameters(root)
    report_width = text(root, "Width")
    if not report_width and section_layouts:
        report_width = section_layouts[0]["reportWidth"]
    first_section = section_layouts[0] if section_layouts else {
        "bodyHeight": None,
        "pageWidth": None,
        "pageHeight": None,
        "margins": {"top": None, "right": None, "bottom": None, "left": None},
        "headerHeight": None,
        "footerHeight": None,
    }
    layout = {
        "reportWidth": report_width,
        # First-section aliases keep the common one-section case simple while
        # `sections` retains every value for multi-section reports.
        "bodyHeight": first_section["bodyHeight"],
        "pageWidth": first_section["pageWidth"],
        "pageHeight": first_section["pageHeight"],
        "margins": first_section["margins"],
        "headerHeight": first_section["headerHeight"],
        "footerHeight": first_section["footerHeight"],
        "sectionCount": len(section_layouts),
        "pageBreaks": [
            page_break
            for section in section_layouts
            for page_break in section["pageBreaks"]
        ],
        "listCount": sum(s["listCount"] for s in section_layouts),
        "subreportCount": sum(s["subreportCount"] for s in section_layouts),
        "itemPositions": item_positions,
        "sections": section_layouts,
        "signals": {
            "pageBreakCount": sum(
                _page_break_state(page_break) != "disabled"
                for s in section_layouts
                for page_break in s["pageBreaks"]
            ),
            "listCount": sum(s["listCount"] for s in section_layouts),
            "subreportCount": sum(s["subreportCount"] for s in section_layouts),
        },
    }

    warnings = []
    if not (body_items or page_header_items or page_footer_items):
        # A layout element existed but produced zero recognized visuals. When a
        # report also has datasets or parameters that is almost always a
        # structural miss (a layout wrapper the parser doesn't walk), not a
        # genuinely empty report — surface it instead of passing as clean.
        if datasets or parameters:
            warnings.append(
                "no report items parsed but datasets/parameters were found — "
                "likely an unrecognized layout wrapper; the visual inventory may be incomplete"
            )
            print(f"!! {path}: {warnings[-1]}", file=sys.stderr)
    dynamic_page_breaks = sum(
        _page_break_state(page_break) == "dynamic"
        for page_break in layout["pageBreaks"]
    )
    if dynamic_page_breaks:
        warnings.append(
            f"{dynamic_page_breaks} page break Disabled value(s) are dynamic "
            "expressions — resolve pagination manually"
        )

    report = {
        "report": name,
        "sourceFile": os.path.basename(path),
        "dataSources": parse_data_sources(root),
        "dataSets": datasets,
        "parameters": parameters,
        "bodyItems": body_items,
        "pageHeaderItems": page_header_items,
        "pageFooterItems": page_footer_items,
        "layout": layout,
    }
    if warnings:
        report["warnings"] = warnings
    return report


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
