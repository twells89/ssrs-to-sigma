#!/usr/bin/env python3
"""
scan_gaps.py — Phase 0a of ssrs-to-sigma.

Read a parsed bundle.json (or parse a directory of RDLs) and classify every
report by how cleanly the converter will handle it. Mirrors the sibling
converters' gap-scan: each report lands in one of four buckets and the markdown
readout becomes a migrate-first shortlist.

  AUTO       fully handled — raw-SQL/warehouse datasets, table/matrix Tablix,
             standard charts, clean expressions, parameters.
  HINT       converts, but a human should glance at it — T-SQL dialect in the
             SQL, aggregate scopes, conditional formatting, list Tablix.
  MANUAL     needs hand-work before it's right — stored procs, custom VB Code,
             multi-value param→SQL joins, mixed-grain Tablix.
  UNHANDLED  no clean Sigma analog — gauges, maps, subreports, radar/polar.

Usage:
    python3 scan_gaps.py --bundle bundle.json -o gap_report.md
    python3 scan_gaps.py --dir ssrs-export/reports -o gap_report.md
"""
import argparse
import json
import os
import re
import sys

import ssrs_expr

UNHANDLED_KINDS = {"gauge", "map", "subreport"}
TSQL_RE = re.compile(r"\b(GETDATE|ISNULL|TOP\s+\d|DATEADD|DATEDIFF|CONVERT|NVARCHAR|CHARINDEX|\[dbo\]|dbo\.)\b",
                     re.IGNORECASE)


def classify_report(rep):
    reasons = {"hint": [], "manual": [], "unhandled": []}

    for ds in rep["dataSets"]:
        cmd = ds.get("commandText") or ""
        if ds.get("isStoredProc"):
            reasons["manual"].append(f"dataset {ds['name']}: stored-proc call")
        if not cmd:
            reasons["manual"].append(f"dataset {ds['name']}: no inline SQL (shared dataset / proc)")
        if TSQL_RE.search(cmd):
            reasons["hint"].append(f"dataset {ds['name']}: T-SQL dialect to review")
        if re.search(r"@[A-Za-z0-9_]+", cmd):
            reasons["hint"].append(f"dataset {ds['name']}: parameterized SQL → controls/filters")
        # expression fields
        for f in ds.get("fields", []):
            if f.get("calculated"):
                _, fl = ssrs_expr.translate(f.get("expression"))
                for w in fl:
                    reasons["manual"].append(f"field {f['name']}: {w}")

    for p in rep.get("parameters", []):
        if p.get("multiValue"):
            reasons["hint"].append(f"param {p['name']}: multi-value → list control")

    for it in rep.get("bodyItems", []):
        k = it.get("kind")
        if k in UNHANDLED_KINDS:
            reasons["unhandled"].append(f"{k} {it.get('name')}")
        elif k == "tablix":
            if it.get("rowGroups") and it.get("columnGroups") and it.get("valueExpressions"):
                pass  # clean matrix
            for vexpr in it.get("valueExpressions", []):
                _, fl = ssrs_expr.translate(vexpr)
                for w in fl:
                    reasons["manual"].append(f"tablix {it['name']}: {w}")
        elif k == "chart":
            ct = (it.get("chartType") or "").lower()
            if ct in ("radar", "polar", "funnel", "range", "shape", "pyramid"):
                reasons["unhandled"].append(f"chart {it.get('name')}: {ct} (redesign)")

    if reasons["unhandled"]:
        bucket = "UNHANDLED"
    elif reasons["manual"]:
        bucket = "MANUAL"
    elif reasons["hint"]:
        bucket = "HINT"
    else:
        bucket = "AUTO"
    return bucket, reasons


def report_md(rows):
    order = {"AUTO": 0, "HINT": 1, "MANUAL": 2, "UNHANDLED": 3}
    rows = sorted(rows, key=lambda r: (order[r[1]], r[0]))
    counts = {b: 0 for b in order}
    for _, b, _ in rows:
        counts[b] += 1
    out = ["# SSRS migration gap scan\n",
           f"**{len(rows)} report(s)** — "
           + " · ".join(f"{b}: {counts[b]}" for b in order) + "\n",
           "Migrate-first = AUTO/HINT. MANUAL/UNHANDLED need design work — quote accordingly.\n",
           "| Report | Bucket | Why |", "|---|---|---|"]
    for name, bucket, reasons in rows:
        why = "clean" if bucket == "AUTO" else "; ".join(
            reasons["unhandled"] + reasons["manual"] + reasons["hint"])[:300]
        out.append(f"| {name} | {bucket} | {why} |")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="SSRS converter-coverage gap scan")
    ap.add_argument("--bundle")
    ap.add_argument("--dir")
    ap.add_argument("-o", "--out", default="gap_report.md")
    args = ap.parse_args()

    if args.bundle:
        with open(args.bundle) as fh:
            bundle = json.load(fh)
    elif args.dir:
        import parse_rdl, glob
        reports = []
        for p in sorted(glob.glob(os.path.join(args.dir, "**", "*.rdl"), recursive=True)):
            try:
                reports.append(parse_rdl.parse_rdl(p))
            except Exception as e:  # noqa: BLE001
                print(f"!! skipped {p}: {e}", file=sys.stderr)
        bundle = {"reports": reports}
    else:
        ap.error("pass --bundle or --dir")

    rows = []
    for rep in bundle["reports"]:
        bucket, reasons = classify_report(rep)
        rows.append((rep["report"], bucket, reasons))

    with open(args.out, "w") as fh:
        fh.write(report_md(rows))
    print(report_md(rows))


if __name__ == "__main__":
    main()
