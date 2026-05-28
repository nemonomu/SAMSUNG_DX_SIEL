param(
  [string[]]$Products = @('hhp', 'tv', 'ref', 'ldy'),
  [switch]$SkipPull,
  [switch]$NoRun
)

$ErrorActionPreference = 'Stop'

$Repo = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $Repo

$Started = Get-Date
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$OutDir = Join-Path $Repo "test_output\amzn_full_$Stamp"
$LogDir = Join-Path $OutDir 'amzn_logs'
New-Item -ItemType Directory -Force -Path $OutDir, $LogDir | Out-Null

function Invoke-Logged {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Exe,
    [string[]]$Args = @()
  )

  $logPath = Join-Path $OutDir $Name
  $commandLine = ($Exe, $Args) -join ' '
  Write-Host "[amzn_full_test] $commandLine"
  & $Exe @Args 2>&1 | Tee-Object -FilePath $logPath
  $code = $LASTEXITCODE
  if ($null -eq $code) {
    $code = 0
  }
  if ($code -ne 0) {
    throw "[amzn_full_test] failed: $commandLine (exit=$code, log=$logPath)"
  }
}

$commandArgs = @(
  'amzn\run.py',
  '--product'
) + $Products + @(
  '--stages', 'main', 'bsr', 'detail',
  '--headless',
  '--no-auto-insert'
)

Set-Content -Encoding UTF8 -Path (Join-Path $OutDir 'command.txt') `
  -Value ('python ' + ($commandArgs -join ' '))

@(
  "started=$($Started.ToString('o'))"
  "repo=$Repo"
  "products=$($Products -join ' ')"
  "no_auto_insert=true"
  "skip_pull=$SkipPull"
) | Set-Content -Encoding UTF8 -Path (Join-Path $OutDir 'summary.txt')

try {
  Invoke-Logged 'git_status_before.txt' 'git' @('status')
  if (-not $SkipPull) {
    Invoke-Logged 'git_pull.txt' 'git' @('pull')
  }
  Invoke-Logged 'apply_sql.txt' 'python' @('apply_sql.py', 'sql\dx_siel_xpath_selectors.sql')
  Invoke-Logged 'py_compile.txt' 'python' @('-m', 'py_compile', 'insert_test_retail_com.py', 'amzn\run.py', 'amzn\detail.py', 'amzn\listing.py')

  if ($NoRun) {
    Write-Host "[amzn_full_test] NoRun enabled. Command prepared only."
  } else {
    Invoke-Logged 'run_console.log' 'python' $commandArgs
  }

  $amznLogs = Join-Path $Repo 'amzn\logs'
  if (Test-Path $amznLogs) {
    Get-ChildItem -Path $amznLogs -File |
      Where-Object { $_.LastWriteTime -ge $Started.AddSeconds(-10) } |
      Copy-Item -Destination $LogDir -Force
  }

  Get-ChildItem -Path $LogDir -File |
    Select-Object Name, Length, LastWriteTime |
    Format-Table -AutoSize |
    Out-String |
    Set-Content -Encoding UTF8 -Path (Join-Path $OutDir 'copied_logs.txt')

  Invoke-Logged 'git_status_after.txt' 'git' @('status')
  Add-Content -Encoding UTF8 -Path (Join-Path $OutDir 'summary.txt') `
    -Value "finished=$((Get-Date).ToString('o'))"

  Write-Host "[amzn_full_test] RESULT_FOLDER=$OutDir"
  exit 0
} catch {
  Add-Content -Encoding UTF8 -Path (Join-Path $OutDir 'summary.txt') `
    -Value "error=$($_.Exception.Message)"
  Write-Error $_
  Write-Host "[amzn_full_test] RESULT_FOLDER=$OutDir"
  exit 1
}
