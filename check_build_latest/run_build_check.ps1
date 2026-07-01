#!powershell
#Usage: powershell -ExecutionPolicy Bypass -File .\check_build_latest\run_build_check.ps1 <year>

param(
    [Parameter(Mandatory=$true, HelpMessage="Year to check, e.g., 2026")]
    [string]$year
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

& "$RootDir\.venv\Scripts\Activate.ps1"

pip install -r "$RootDir\requirements.txt" -q

python "$RootDir\check_build_latest\check_build_latest_windows.py" `
    --docker_user "DOCKERUSER" `
    --docker_pass "DOCKERPASS" `
    --input_dir "$RootDir\output\latest_check" `
    --output_dir "$RootDir\output\build_check" `
    --repo_dir "$RootDir\repo" `
    --log_dir "$RootDir\output\log\build_check" `
    --year $year