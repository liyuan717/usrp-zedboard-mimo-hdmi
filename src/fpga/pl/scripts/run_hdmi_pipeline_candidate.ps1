param(
  [string]$Build = "build/hp1v1",
  [ValidateSet('validate','synth','implement')][string]$Action='validate',
  [string]$AdiHdlDir = "",
  [string]$Vivado = "G:\AMDDesignTools\2025.2\Vivado\bin\vivado.bat",
  [switch]$Detach,
  [switch]$Worker
)
$ErrorActionPreference='Stop'
$root=(Resolve-Path "$PSScriptRoot/../..").Path
$out=Join-Path $root $Build
if($Build -notmatch '^(build[\\/])'){throw "Build must be beneath build/: $Build"}
if($Detach){
  # A foreground Codex/PTY command can disappear when its turn finishes.  The
  # actual build worker must therefore be a hidden, independent PowerShell
  # child.  Its logs live adjacent to (not inside) the still-empty candidate,
  # preserving bootstrap's fresh-directory proof.
  if(Test-Path -LiteralPath $out){throw "fresh build directory required: $out"}
  $adjacent="$out.runner"
  foreach($path in @("$adjacent.pid","$adjacent.stdout.log","$adjacent.stderr.log")){
    if(Test-Path -LiteralPath $path){throw "fresh detached runner sidecar required: $path"}
  }
  $pwsh=Join-Path $PSHOME 'pwsh.exe'
  $childArgs=@('-NoProfile','-File',$PSCommandPath,'-Build',$Build,'-Action',$Action,'-Vivado',$Vivado,'-Worker')
  if(-not [string]::IsNullOrWhiteSpace($AdiHdlDir)){$childArgs+=@('-AdiHdlDir',$AdiHdlDir)}
  $childProcess=Start-Process -FilePath $pwsh -ArgumentList $childArgs -WindowStyle Hidden -RedirectStandardOutput "$adjacent.stdout.log" -RedirectStandardError "$adjacent.stderr.log" -PassThru
  $childProcess.Id | Set-Content -LiteralPath "$adjacent.pid" -NoNewline
  "HDMI_PIPELINE_DETACHED_WORKER_PID=$($childProcess.Id)"
  exit 0
}
if(Test-Path -LiteralPath $out){throw "fresh build directory required: $out"}
function Write-RunnerExit([int]$Code,[string]$State){
  if(Test-Path -LiteralPath $out){
    $Code | Set-Content -LiteralPath (Join-Path $out 'runner.exitcode.txt')
    "$State $(Get-Date -Format o)" | Set-Content -LiteralPath (Join-Path $out 'runner.state.txt')
  }
}
try {
if(-not (Test-Path -LiteralPath $Vivado -PathType Leaf)){throw "Vivado executable not found: $Vivado"}
$python='C:\Users\Flechazo233\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw "Pinned bootstrap Python not found: $python"}
if([string]::IsNullOrWhiteSpace($AdiHdlDir)){$AdiHdlDir=Join-Path $root 'build/video_candidate_vendor_20261024_abi3_clk2safe'}
if(-not (Test-Path -LiteralPath $AdiHdlDir -PathType Container)){throw "ADI vendor candidate tree not found: $AdiHdlDir"}

New-Item -ItemType Directory -Path $out|Out-Null
$spiBudget=Join-Path $out 'zedboard_spi_mode1_budget.xdc'
& $python (Join-Path $root 'pl/scripts/bootstrap_hdmi_pipeline_candidate.py') --build $out --spi-budget $spiBudget
if($LASTEXITCODE -ne 0){throw "bootstrap failed with exit code $LASTEXITCODE"}
if(-not (Test-Path -LiteralPath $spiBudget -PathType Leaf)){throw "bootstrap did not create reviewed SPI timing budget: $spiBudget"}
$env:ADI_HDL_DIR=$AdiHdlDir
$env:MIMO_CONTINUOUS_VIDEO_HEALTH_ALLOW='YES'
$env:ADI_ALLOW_VERSION_MISMATCH='YES'
$env:MIMO_RF_EXTERNAL_BUDGET_XDC=Join-Path $root 'pl/constraints/zedboard_rf_lvds_video_candidate_budget.xdc'
$env:MIMO_RF_SPI_BUDGET_XDC=$spiBudget
$env:MIMO_RF_BUILD_PYTHON=$python
$vivadoLog=Join-Path $out "vivado_$Action.log"
$vivadoStdout=Join-Path $out "vivado_$Action.stdout.log"
& $Vivado -mode batch -notrace -nojournal -log $vivadoLog -source (Join-Path $root 'pl/scripts/create_zedboard_hdmi_pipeline_system.tcl') -tclargs $out $Action *> $vivadoStdout
$vivadoExit=$LASTEXITCODE
$vivadoExit | Set-Content -LiteralPath (Join-Path $out "vivado_$Action.exitcode.txt")
if($vivadoExit -ne 0){throw "Vivado $Action failed with exit code $vivadoExit; see $vivadoLog and $vivadoStdout"}
Write-RunnerExit 0 "PASS"
} catch {
  if(Test-Path -LiteralPath $out){
    $_ | Out-File -LiteralPath (Join-Path $out 'runner.failure.log') -Encoding utf8
    Write-RunnerExit 1 "FAIL"
  }
  Write-Error $_
  exit 1
}
