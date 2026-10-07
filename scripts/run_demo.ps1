param(
    [int]$Count = 10
)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python .\scripts\run_demo.py --count $Count
