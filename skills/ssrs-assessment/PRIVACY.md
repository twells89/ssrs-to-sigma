# Privacy & data handling — ssrs-assessment

Surface this to the customer before running the assessment.

## What it reads

- **Local RDL XML only.** The reports the customer already exported (an
  `ssrs-export-*` folder, a `.rptproj` project, or any directory of `.rdl`
  files).

## What it does NOT do

- **No connection to SSRS / Report Server.** It does not call the REST API,
  query the ReportServer catalog DB, or render any report.
- **No warehouse query.** It never executes the SQL inside the reports.
- **No Sigma calls.** Nothing is created, uploaded, or transmitted to Sigma.
- **No network access at all** for the assessment step. It parses files on disk
  and writes two local artifacts.

## What it writes

- `inventory.json` and `assessment.md` in the `--out` directory you choose.
  These contain report names, paths, visualization/dataset counts, and
  coverage tags. They may include **dataset SQL fragments and field/column
  names** quoted from the RDL (e.g. in flag messages). Treat the output with the
  same sensitivity as the reports themselves; store it where the reports' own
  metadata would be allowed.

## The export step (separate, customer-run)

The RDL export itself (`../ssrs-to-sigma/scripts/export-ssrs.ps1`) is run by the
customer inside their own network using their own Windows credentials. It is
read-only against SSRS (lists catalog items and downloads report definitions; in
catalog-DB mode it issues a single read-only `SELECT`). It does not modify,
execute, or delete anything in SSRS. Review that script before running it — it
is short and single-purpose by design.
