#!/usr/bin/env python3
"""
assess.py — SSRS estate assessment (read-only).

Surveys a set of SSRS RDL reports — an `ssrs-export-*` folder, a `.rptproj`
project, or any directory of `.rdl` files — and produces a migration-readiness
readout: counts, a visualization-type histogram, dataset/parameter mix, and
per-report converter-coverage tags (AUTO / HINT / MANUAL / UNHANDLED) scored
against the SAME coverage the ssrs-to-sigma converter applies.

Read-only: parses local XML only. Never connects to SSRS, a warehouse, or Sigma.

Usage:
    python3 assess.py --dir ssrs-export/reports --out /tmp/ssrs-assessment
    python3 assess.py --bundle bundle.json      --out /tmp/ssrs-assessment

It reuses parse_rdl.py + scan_gaps.py from the converter skill (sibling dir),
so the assessment can never drift from what the converter actually does.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

# import the converter's parser + classifier (sibling skill)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ssrs-to-sigma", "scripts"))
import parse_rdl       # noqa: E402
import scan_gaps       # noqa: E402


def load_reports(args):
    if args.bundle:
        with open(args.bundle) as fh:
            return json.load(fh)["reports"]
    reports = []
    for p in sorted(glob.glob(os.path.join(args.dir, "**", "*.rdl"), recursive=True)):
        try:
            reports.append(parse_rdl.parse_rdl(p))
        except Exception as e:  # noqa: BLE001
            print(f"!! skipped {p}: {e}", file=sys.stderr)
    return reports


def summarize(reports):
    charts = Counter()
    item_kinds = Counter()
    dataset_kinds = Counter()
    buckets = Counter()
    total_params = 0
    rows = []

    for rep in reports:
        bucket, reasons = scan_gaps.classify_report(rep)
        buckets[bucket] += 1
        for it in rep.get("bodyItems", []):
            item_kinds[it.get("kind")] += 1
            if it.get("kind") == "chart":
                charts[(it.get("chartType") or "?").lower()] += 1
        for ds in rep.get("dataSets", []):
            if ds.get("isStoredProc"):
                dataset_kinds["stored-proc"] += 1
            elif ds.get("commandText"):
                dataset_kinds["inline-sql"] += 1
            else:
                dataset_kinds["shared/none"] += 1
        total_params += len(rep.get("parameters", []))
        flat = "; ".join(reasons["unhandled"] + reasons["manual"] + reasons["hint"])
        rows.append({
            "report": rep["report"],
            "bucket": bucket,
            "datasets": len(rep.get("dataSets", [])),
            "params": len(rep.get("parameters", [])),
            "tablixes": sum(1 for i in rep.get("bodyItems", []) if i.get("kind") == "tablix"),
            "charts": sum(1 for i in rep.get("bodyItems", []) if i.get("kind") == "chart"),
            "flags": flat,
        })

    return {
        "reportCount": len(reports),
        "buckets": dict(buckets),
        "chartHistogram": dict(charts),
        "itemKinds": dict(item_kinds),
        "datasetKinds": dict(dataset_kinds),
        "totalParameters": total_params,
        "reports": rows,
    }


def readout_md(s):
    order = ["AUTO", "HINT", "MANUAL", "UNHANDLED"]
    b = s["buckets"]
    lines = [
        "# SSRS → Sigma — migration assessment\n",
        f"**{s['reportCount']} report(s)** across the estate. Read-only scan of "
        "RDL definitions; no system was queried.\n",
        "## Migration-readiness\n",
        "| Bucket | Reports | Meaning |", "|---|---|---|",
        f"| AUTO | {b.get('AUTO',0)} | converts cleanly |",
        f"| HINT | {b.get('HINT',0)} | converts; glance before shipping |",
        f"| MANUAL | {b.get('MANUAL',0)} | hand-work first (procs, custom VB) |",
        f"| UNHANDLED | {b.get('UNHANDLED',0)} | no clean analog (gauge/map/subreport) |",
        "",
        f"**Migrate-first shortlist = {b.get('AUTO',0)+b.get('HINT',0)} report(s)** "
        "(AUTO + HINT).\n",
        "## Visualization mix\n",
        "| Item | Count |", "|---|---|",
    ]
    for k, v in sorted(s["itemKinds"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    if s["chartHistogram"]:
        lines += ["", "Chart types: " + ", ".join(
            f"{k} ({v})" for k, v in sorted(s["chartHistogram"].items(), key=lambda x: -x[1]))]
    lines += [
        "", "## Datasets & parameters\n",
        "| Dataset kind | Count |", "|---|---|",
    ]
    for k, v in sorted(s["datasetKinds"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines += [f"\nTotal report parameters: **{s['totalParameters']}**\n",
              "## Per-report detail\n",
              "| Report | Bucket | Datasets | Params | Tablix | Charts | Flags |",
              "|---|---|---|---|---|---|---|"]
    rank = {bkt: i for i, bkt in enumerate(order)}
    for r in sorted(s["reports"], key=lambda r: (rank[r["bucket"]], r["report"])):
        lines.append(f"| {r['report']} | {r['bucket']} | {r['datasets']} | "
                     f"{r['params']} | {r['tablixes']} | {r['charts']} | {r['flags'][:200]} |")
    lines += [
        "", "---", "",
        "> Stored procs, custom VB `Code`, gauges/maps/subreports, and "
        "pixel-perfect paginated layouts need design work — quote them as "
        "redesign, not lift-and-shift. The converter flags each one explicitly "
        "(flag, never fake).",
    ]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="SSRS estate assessment (read-only)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir", help="folder of .rdl files (recursive)")
    g.add_argument("--bundle", help="a pre-parsed bundle.json")
    ap.add_argument("--out", default="/tmp/ssrs-assessment")
    args = ap.parse_args()

    reports = load_reports(args)
    if not reports:
        print("no reports found", file=sys.stderr)
        sys.exit(1)
    summary = summarize(reports)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "inventory.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    md = readout_md(summary)
    with open(os.path.join(args.out, "assessment.md"), "w") as fh:
        fh.write(md)
    print(md)
    print(f"\nwrote {args.out}/inventory.json and {args.out}/assessment.md")


if __name__ == "__main__":
    main()
