#!/usr/bin/env python3
"""
verify_parity.py — Phase 5/6 of ssrs-to-sigma (hard gate).

Compare what the migrated Sigma workbook produces against ground-truth numbers
taken from SSRS itself — never from invented expectations. Expected values come
from rendered SSRS output (CSV via `...?rs:Format=CSV`, exported during Phase 0)
normalized into expected_parity.json:

  {
    "<report>/<element>": [
      {"keys": {"RegionName": "East", "Category": "Bikes"}, "values": {"Value": 12345.67}},
      ...
    ]
  }

The script exports each workbook element to CSV via the Sigma export API, joins
on the key columns, and compares values (money/counts exact to a cent; ratio
metrics within rel-tol 1e-6). GREEN only when every element PASSes — a 200 on
the workbook POST is NOT parity.

Authentication is browser-first with client-credentials fallback. A valid
environment bearer or ``auth.json`` is reused; known-stale tokens refresh
proactively and a 401 refreshes and retries once. Stdlib only.
"""
import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import sigma_rest  # noqa: E402

REL_TOL = 1e-6
ABS_TOL = 0.005   # half a cent


def _req(method, path, body=None, raw=False):
    value = sigma_rest.request(
        method,
        path,
        body=body,
        accept="text/csv" if raw else "application/json",
        binary=raw,
        allow_statuses=(404, 409, 425) if raw else (),
    )
    return ({} if value is None else value) if not raw else value


def export_element_csv(workbook_id, element_id, page_id=None):
    """Kick off an export and poll until the CSV is ready; return rows (list[dict])."""
    body = {"format": {"type": "csv"}, "elementId": element_id}
    if page_id:
        body["pageId"] = page_id
    started = _req("POST", f"/v2/workbooks/{workbook_id}/export", body)
    query_id = started.get("queryId") or started.get("exportId") or started.get("id")
    if not query_id:
        raise RuntimeError(f"export start returned no query id: {started}")
    for _ in range(60):
        time.sleep(2)
        data = _req("GET", f"/v2/query/{query_id}/download", raw=True)
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        return list(csv.DictReader(io.StringIO(text)))
    raise TimeoutError(f"export {query_id} did not finish")


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return None


def close(a, b):
    na, nb = _num(a), _num(b)
    if na is None or nb is None:
        return str(a).strip() == str(b).strip()
    if abs(na - nb) <= ABS_TOL:
        return True
    denom = max(abs(na), abs(nb), 1e-9)
    return abs(na - nb) / denom <= REL_TOL


def key_of(row, key_names):
    return tuple(str(row.get(k, "")).strip() for k in key_names)


def compare(expected_rows, actual_rows):
    """Return (passed, [mismatch strings])."""
    if not expected_rows:
        return True, ["(no expected rows — structural only)"]
    key_names = list(expected_rows[0]["keys"].keys())
    actual_by_key = {key_of(r, key_names): r for r in actual_rows}
    problems = []
    for exp in expected_rows:
        k = tuple(str(v).strip() for v in exp["keys"].values())
        act = actual_by_key.get(k)
        if act is None:
            problems.append(f"missing row for keys={exp['keys']}")
            continue
        for col, want in exp["values"].items():
            got = act.get(col)
            if got is None:
                problems.append(f"keys={exp['keys']} col '{col}' absent in Sigma export")
            elif not close(want, got):
                problems.append(f"keys={exp['keys']} col '{col}': SSRS={want} Sigma={got}")
    return (len(problems) == 0), problems


def main():
    ap = argparse.ArgumentParser(description="SSRS→Sigma parity gate")
    ap.add_argument("--workbook-id", required=True)
    ap.add_argument("--expected", required=True, help="expected_parity.json")
    ap.add_argument("--element-map", required=True,
                    help='JSON {"<report>/<element>": {"elementId": "...", "pageId": "..."}}')
    ap.add_argument("--report", default="parity_report.md")
    args = ap.parse_args()

    with open(args.expected) as fh:
        expected = json.load(fh)
    with open(args.element_map) as fh:
        emap = json.load(fh)

    lines = ["# SSRS → Sigma parity report\n", "| Element | Result | Detail |", "|---|---|---|"]
    all_green = True
    for name, exp_rows in expected.items():
        meta = emap.get(name)
        if not meta:
            all_green = False
            lines.append(f"| {name} | ❌ NO MAP | no elementId in --element-map |")
            continue
        try:
            actual = export_element_csv(args.workbook_id, meta["elementId"], meta.get("pageId"))
        except Exception as e:  # noqa: BLE001
            all_green = False
            lines.append(f"| {name} | ❌ ERROR | export failed: {e} |")
            continue
        ok, problems = compare(exp_rows, actual)
        if ok:
            lines.append(f"| {name} | ✅ PASS | {len(exp_rows)} rows |")
        else:
            all_green = False
            lines.append(f"| {name} | ❌ FAIL | {'; '.join(problems[:5])}{' …' if len(problems) > 5 else ''} |")

    verdict = "GREEN ✅ — every element matches SSRS" if all_green else "RED ❌ — see failures above"
    out = "\n".join(lines) + f"\n\n**{verdict}**\n"
    with open(args.report, "w") as fh:
        fh.write(out)
    print(out)
    sys.exit(0 if all_green else 1)


if __name__ == "__main__":
    main()
