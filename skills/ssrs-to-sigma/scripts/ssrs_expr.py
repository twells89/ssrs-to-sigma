#!/usr/bin/env python3
"""
ssrs_expr.py — translate SSRS (VB.NET-style) expressions to Sigma formulas.

SSRS expressions begin with `=` and use a VB dialect:
  =Fields!Sales.Value
  =Sum(Fields!Sales.Value, "Dataset1")
  =IIf(Fields!Qty.Value > 0, "Y", "N")

This module does a best-effort syntactic translation and, crucially, REPORTS
what it could not translate cleanly rather than silently emitting wrong logic
("flag, never fake"). `translate()` returns (formula, flags) where flags is a
list of human-readable warnings; an empty list means a clean 1:1.

It is deliberately conservative: anything involving custom VB code
(`Code.*`), running aggregates (`RunningValue`, `Previous`, `RowNumber`), or
report-runtime objects (`ReportItems!`, `Globals!`, `User!`) is passed through
unchanged AND flagged for manual review.
"""
import re

# SSRS function name -> Sigma function name (case-insensitive match on the SSRS side)
FUNC_MAP = {
    "iif": "If",
    "switch": "Switch",
    "sum": "Sum",
    "avg": "Avg",
    "count": "Count",
    "countdistinct": "CountDistinct",
    "min": "Min",
    "max": "Max",
    "first": "First",
    "last": "Last",
    "abs": "Abs",
    "round": "Round",
    "ceiling": "Ceiling",
    "floor": "Floor",
    "len": "Length",
    "trim": "Trim",
    "ucase": "Upper",
    "lcase": "Lower",
    "upper": "Upper",
    "lower": "Lower",
    "left": "Left",
    "right": "Right",
    "mid": "Substring",
    "isnothing": "IsNull",
    "today": "Today",
    "now": "Now",
    "year": "Year",
    "month": "Month",
    "day": "Day",
    "format": "Format",      # arg semantics differ — flagged below
    "cdbl": "Number",
    "cint": "Number",
    "cstr": "Text",
}

# Functions with no clean Sigma analog — pass through, flag for manual review.
UNSUPPORTED = {
    "runningvalue", "previous", "rownumber", "runningtotal",
    "lookupset", "multilookup", "level", "inscope", "aggregate",
}

# VB logical / comparison operators -> Sigma
OPERATOR_SUBS = [
    (r"\bAndAlso\b", "and"),
    (r"\bOrElse\b", "or"),
    (r"\bAnd\b", "and"),
    (r"\bOr\b", "or"),
    (r"\bNot\b", "not"),
    (r"\bMod\b", "%"),
    (r"<>", "!="),
]

FIELDS_RE = re.compile(r"Fields!([A-Za-z0-9_]+)\.Value")
PARAMS_RE = re.compile(r"Parameters!([A-Za-z0-9_]+)\.Value")
# Aggregate-with-scope:  Sum(Fields!X.Value, "Dataset1")  ->  drop the scope arg.
# Scoped to known aggregates and a single field arg so it never eats a real
# string argument (e.g. the last value of a Switch).
_AGGS = "Sum|Avg|Count|CountDistinct|CountRows|Min|Max|First|Last|StDev|StDevP|Var|VarP"
SCOPE_RE = re.compile(
    rf'\b({_AGGS})\s*\(\s*(Fields![A-Za-z0-9_]+\.Value)\s*,\s*"[^"]*"\s*\)',
    re.IGNORECASE)


def translate(expr, ref_fmt="[{0}]"):
    """
    Translate one SSRS expression string to a Sigma formula.

    ref_fmt formats a field reference: "[{0}]" for a data-model column,
    "[Master/{0}]" for a workbook reference into a base element.

    Returns (formula:str, flags:list[str]).
    """
    flags = []
    if expr is None:
        return None, flags
    s = expr.strip()
    if not s:
        return s, flags
    # Constant string/number with no leading '=' is a literal in RDL.
    if not s.startswith("="):
        return s, flags
    s = s[1:].strip()

    low = s.lower()
    for bad in UNSUPPORTED:
        if re.search(rf"\b{bad}\s*\(", low):
            flags.append(f"'{bad}(...)' has no clean Sigma analog — passed through, MANUAL review")
    # Code.Foo(...) custom VB — but NOT Fields!Code.Value (a field named "Code").
    if re.search(r"(?<!!)\bcode\.\w", low):
        flags.append("custom VB Code.* reference — no Sigma analog, MANUAL translation")
    if "reportitems!" in low:
        flags.append("ReportItems! cross-textbox reference — re-express against the data, MANUAL")
    if "globals!" in low or "user!" in low:
        flags.append("Globals!/User! runtime object — map to Sigma system function, MANUAL")
    if re.search(r"\bformat\s*\(", low):
        flags.append("Format(...) numeric/date mask — Sigma uses element format config, REVIEW")

    # Drop aggregate scope args FIRST (matches the raw Fields! form):
    #   Sum(Fields!X.Value, "Dataset1")  ->  Sum(Fields!X.Value)
    prev = None
    while prev != s:
        prev = s
        s = SCOPE_RE.sub(r"\1(\2)", s)

    # Field / parameter references
    s = FIELDS_RE.sub(lambda m: ref_fmt.format(m.group(1)), s)
    s = PARAMS_RE.sub(lambda m: f"[{m.group(1)}]", s)

    # Operators
    for pat, rep in OPERATOR_SUBS:
        s = re.sub(pat, rep, s)

    # Function renames (whole-word, case-insensitive, only before a '(')
    def _fn(m):
        name = m.group(1)
        mapped = FUNC_MAP.get(name.lower())
        return (mapped if mapped else name) + "("
    s = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", _fn, s)

    return s, flags


if __name__ == "__main__":
    import sys
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        f, fl = translate(line)
        print(f"{line!r:60} -> {f!r}")
        for w in fl:
            print(f"      ⚠ {w}")
