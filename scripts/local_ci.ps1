$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    & uv run python -m scripts.local_ci @args
    if ($LASTEXITCODE -ne 0) { throw "Local CI stopped (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
