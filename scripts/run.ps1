param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $AppArgs)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

if (-not (Test-Path $py)) {
    Write-Host "创建虚拟环境 .venv"
    if ($hasUv) { uv venv .venv } else { python -m venv .venv }
}

& $py -c "import semg_desktop, PySide6, pyqtgraph, numpy, scipy, serial" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装依赖（可编辑安装 + dev）"
    if ($hasUv) { uv pip install --python $py -e ".[dev]" } else { & $py -m pip install -e ".[dev]" }
}

if ($AppArgs.Count -eq 0) { $AppArgs = @("gui", "--simulate") }
& $py -m semg_desktop @AppArgs
exit $LASTEXITCODE
