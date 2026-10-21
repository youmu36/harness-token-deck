$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    & uv run python -m scripts.publish_release @args
    if ($LASTEXITCODE -ne 0) { throw "Release publisher stopped (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
