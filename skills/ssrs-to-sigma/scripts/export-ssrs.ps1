<#
.SYNOPSIS
  ssrs-to-sigma — Phase 0 customer-side export.

  Runs INSIDE the customer firewall and produces a self-contained zip of RDL
  report definitions for offline conversion. Nothing leaves the network except
  the zip the customer chooses to hand over. Read-only against SSRS.

.DESCRIPTION
  Two acquisition modes, auto-selected:
    1. REST  — SSRS 2017+ / Power BI Report Server expose
               /reports/api/v2.0. Walks CatalogItems, downloads each report's
               RDL via Content/$value. Windows-auth (no API key exists in SSRS).
    2. SQL   — older SSRS, or when HTTP auth is blocked. Reads the RDL XML
               straight out of the ReportServer catalog DB (Catalog.Content,
               varbinary(max)). Needs only db_datareader on ReportServer.

  Output layout (matches what parse_rdl.py / scan_gaps.py expect):
    ssrs-export-<timestamp>/
      catalog.json          # path, name, modifiedDate, size per report
      metadata.json         # server kind, mode, version, counts
      reports/<folder>/<name>.rdl
    ssrs-export-<timestamp>.zip

.PARAMETER ReportServerUrl
  REST mode. The Report Server base, e.g. http://localhost/reports or
  https://rs.contoso.com/Reports_PROD (named instances change the vdir).

.PARAMETER SqlServer / Database
  SQL mode. SQL Server host and the ReportServer catalog DB
  (default "ReportServer"; named instance -> "ReportServer$INSTANCE").

.PARAMETER Credential
  Optional explicit DOMAIN\user; omit to use the current Windows token.

.EXAMPLE
  .\export-ssrs.ps1 -ReportServerUrl http://localhost/reports
.EXAMPLE
  .\export-ssrs.ps1 -SqlServer sql01 -Database ReportServer
#>
[CmdletBinding(DefaultParameterSetName = 'Rest')]
param(
  [Parameter(ParameterSetName = 'Rest', Mandatory = $true)]
  [string]$ReportServerUrl,

  [Parameter(ParameterSetName = 'Sql', Mandatory = $true)]
  [string]$SqlServer,
  [Parameter(ParameterSetName = 'Sql')]
  [string]$Database = 'ReportServer',

  [System.Management.Automation.PSCredential]$Credential,
  [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $OutDir) { $OutDir = Join-Path (Get-Location) "ssrs-export-$stamp" }
$reportsDir = Join-Path $OutDir 'reports'
New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null

function Save-Rdl([string]$path, [string]$name, [byte[]]$bytes) {
  $rel = ($path.TrimStart('/'))
  $dir = Join-Path $reportsDir (Split-Path $rel -Parent)
  if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $file = Join-Path $reportsDir ("$rel.rdl" -replace '//', '/')
  [System.IO.File]::WriteAllBytes($file, $bytes)
  return $file
}

$catalog = @()
$mode = $PSCmdlet.ParameterSetName

if ($mode -eq 'Rest') {
  $base = $ReportServerUrl.TrimEnd('/')
  if ($base -notmatch '/api/v2\.0$') { $base = "$base/api/v2.0" }
  $authArgs = @{}
  if ($Credential) { $authArgs['Credential'] = $Credential }
  else { $authArgs['UseDefaultCredentials'] = $true }

  Write-Host "REST mode -> $base"
  try {
    $items = Invoke-RestMethod -Uri "$base/CatalogItems" @authArgs
  } catch {
    Write-Error ("CatalogItems failed ($($_.Exception.Message)). " +
      "If this is a 503, the Reporting Services service may be stopped (it is NOT IIS): " +
      "Get-Service *Report*,*PowerBI* ; Start-Service 'SQL Server Reporting Services'. " +
      "If pre-2017 (no REST API), re-run in -SqlServer mode.")
    throw
  }
  $reports = $items.value | Where-Object { $_.Type -eq 'Report' }
  Write-Host "found $($reports.Count) report(s)"
  foreach ($r in $reports) {
    try {
      $bytes = Invoke-RestMethod -Uri "$base/CatalogItems($($r.Id))/Content/`$value" @authArgs `
                 -OutFile ([System.IO.Path]::GetTempFileName()) -PassThru -ErrorAction Stop
    } catch { Write-Warning "skip $($r.Path): $($_.Exception.Message)"; continue }
    # Invoke-RestMethod with -OutFile returns nothing useful; re-fetch as bytes:
    $bytes = Invoke-WebRequest -Uri "$base/CatalogItems($($r.Id))/Content/`$value" @authArgs
    $file = Save-Rdl $r.Path $r.Name $bytes.Content
    $catalog += [pscustomobject]@{ path = $r.Path; name = $r.Name
                                   modifiedDate = $r.ModifiedDate; file = $file }
  }
}
else {
  # SQL mode — RDL XML lives in Catalog.Content (varbinary), Type 2 = Report.
  $query = @"
SELECT Path, Name, CONVERT(varbinary(max), Content) AS Content, ModifiedDate
FROM   dbo.Catalog
WHERE  Type = 2 AND Content IS NOT NULL
"@
  $connStr = "Server=$SqlServer;Database=$Database;"
  if ($Credential) {
    $connStr += "User Id=$($Credential.UserName);Password=$($Credential.GetNetworkCredential().Password);"
  } else { $connStr += "Integrated Security=SSPI;" }

  Write-Host "SQL mode -> $SqlServer / $Database"
  $conn = New-Object System.Data.SqlClient.SqlConnection $connStr
  $conn.Open()
  try {
    $cmd = $conn.CreateCommand(); $cmd.CommandText = $query
    $rdr = $cmd.ExecuteReader()
    while ($rdr.Read()) {
      $bytes = [byte[]]$rdr['Content']
      $file = Save-Rdl $rdr['Path'] $rdr['Name'] $bytes
      $catalog += [pscustomobject]@{ path = $rdr['Path']; name = $rdr['Name']
                                     modifiedDate = $rdr['ModifiedDate']; file = $file }
    }
  } finally { $conn.Close() }
  Write-Host "exported $($catalog.Count) report(s)"
}

$catalog | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutDir 'catalog.json')
[pscustomobject]@{
  exportedAt = $stamp; mode = $mode; reportCount = $catalog.Count
  source = if ($mode -eq 'Rest') { $ReportServerUrl } else { "$SqlServer/$Database" }
} | ConvertTo-Json | Set-Content (Join-Path $OutDir 'metadata.json')

$zip = "$OutDir.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $OutDir -DestinationPath $zip
Write-Host ""
Write-Host "Done. Hand this single file to the migration team:"
Write-Host "  $zip"
Write-Host ""
Write-Host "OPTIONAL (enables data-parity verification): for a handful of key"
Write-Host "reports, also export rendered CSV and drop it in the zip under"
Write-Host "expected/<report>.csv  —  e.g. browse:"
Write-Host "  <ReportServer>?<ReportPath>&rs:Format=CSV"
