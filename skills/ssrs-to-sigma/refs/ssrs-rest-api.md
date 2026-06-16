# Getting RDL out of SSRS (Phase 0)

The defining constraint of an SSRS migration: **the report server almost always
sits inside the customer's firewall**, so the converter can't reach it. The
deliverable is a small, auditable, read-only CLI the customer runs *themselves*
inside the network (`scripts/export-ssrs.ps1`), which emits a zip you convert
offline. This doc is the reference behind that script.

## There is no API key

SSRS has **no token / API-key concept** the way Tableau (PAT) or Sigma (client
credentials) do. Authentication is **Windows credentials** — NTLM / Negotiate /
Kerberos by default, optionally Basic. There is nothing to generate in a portal.

- PowerShell: `-UseDefaultCredentials` uses the current logged-in token;
  `-Credential (Get-Credential)` supplies an explicit `DOMAIN\user`.
- The running account needs **Browser** (or Content Manager) rights on the
  reports. A 401/404 on a report means the account can't see it — same as the
  web portal.

## Mode 1 — REST API (SSRS 2017+ / Power BI Report Server)

Base path: `<ReportServerUrl>/api/v2.0`. Useful endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/v2.0/CatalogItems` | list everything; filter `Type -eq "Report"` |
| `GET /api/v2.0/CatalogItemsByPath(path='…')` | resolve a known path |
| `GET /api/v2.0/CatalogItems({id})/Content/$value` | download the report's RDL bytes |
| `GET /api/v2.0/System` | probe — if this 200s, the modern REST API exists |

`CatalogItems` returns `{ value: [ { Id, Name, Path, Type, ModifiedDate, … } ] }`.
`Type` is a string here (`"Report"`, `"DataSet"`, `"DataSource"`, `"Folder"`).

## Mode 2 — ReportServer catalog DB (older SSRS, or HTTP blocked)

Pre-2017 SSRS has no REST API. The RDL XML lives in the **catalog database** as
a `varbinary(max)` BLOB. Needs only `db_datareader` on `ReportServer` —
bypasses HTTP auth entirely.

```sql
SELECT Path, Name,
       CONVERT(varbinary(max), Content) AS Content,   -- RDL XML bytes
       ModifiedDate
FROM   dbo.Catalog
WHERE  Type = 2          -- 2 = Report (1=Folder, 4=LinkedReport,
  AND  Content IS NOT NULL;   -- 5=DataSource, 6=Model, 8=SharedDataSet)
```

Named instance → the catalog DB is `ReportServer$INSTANCE`.

## Mode 3 — no server at all (SSDT / Visual Studio project)

A report *project* ships as a `.rptproj` folder with `.rdl` files directly on
disk. The customer just zips the project folder — same downstream pipeline,
skip the CLI entirely. (`parse_rdl.py --dir <folder>` walks it.)

## Render output for data parity (optional but valuable)

Structural conversion needs only the RDL. **Data** parity (Phase 6) needs
ground-truth numbers — and SSRS will render any report to CSV:

```
<ReportServer>?<ReportPath>&rs:Format=CSV
# also: &rs:Format=EXCELOPENXML | XML | PDF ; &<ParamName>=<value> to set params
```

Ask the customer to render a handful of key reports to CSV and drop them in the
zip under `expected/<report>.csv`. Without that, you can verify spec compilation
but not data parity — be explicit about which you achieved.

## HTTP 503 troubleshooting (the common first failure)

If the REST call returns 503 / connection refused, in order:

1. **Reporting Services is its own Windows service — NOT IIS.**
   ```powershell
   Get-Service | Where-Object { $_.Name -match 'Report|PowerBI|SSRS' }
   Start-Service 'SQL Server Reporting Services'   # or 'PowerBIReportServer'
   ```
2. **Named instance changes the virtual directory.** Default is `/Reports` and
   `/ReportServer`; a named instance is `/Reports_<INSTANCE>` and
   `/ReportServer_<INSTANCE>`. Find the real URLs:
   ```powershell
   netsh http show urlacl | Select-String "Report"
   # or the WMI config provider:
   Get-WmiObject -Namespace 'root\Microsoft\SqlServer\ReportServer\<v>\Admin' `
     -Class MSReportServer_ConfigurationSetting   # ListReservedUrls()
   # version keys: v13=2016, v14=2017, v15=2019, v16=2022
   ```
3. **Load the web portal in a browser** to localize the failure (service down vs
   auth vs wrong URL).
4. If it's **pre-2017** (no `/api/v2.0`), go straight to **Mode 2** (catalog DB).

## What the zip looks like

```
ssrs-export-<timestamp>/
  catalog.json     # path, name, modifiedDate, file per report
  metadata.json    # mode (Rest|Sql), source, reportCount, exportedAt
  reports/<folder>/<name>.rdl     # raw RDL XML, folder structure preserved
  expected/<report>.csv           # OPTIONAL rendered output for parity
ssrs-export-<timestamp>.zip
```

From the zip onward, everything is offline: `parse_rdl.py --dir reports`.
