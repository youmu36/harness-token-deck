# Optional convenience settings. Explicit user overrides take precedence.
if (-not $env:UV_PROJECT_ENVIRONMENT) {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv'
}
if (-not $env:UV_PYTHON) { $env:UV_PYTHON = '3.12' }
