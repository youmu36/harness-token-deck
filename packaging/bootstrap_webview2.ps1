$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$htdBootstrap = Join-Path $PSScriptRoot 'MicrosoftEdgeWebview2Setup.exe'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $htdBootstrap
    $htdSignature = Get-AuthenticodeSignature -LiteralPath $htdBootstrap
    if ($htdSignature.Status -ne 'Valid' -or $htdSignature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw 'The WebView2 bootstrapper signature is not valid.'
    }
    $htdProcess = Start-Process -FilePath $htdBootstrap -ArgumentList '/silent', '/install' -WindowStyle Hidden -Wait -PassThru
    if ($htdProcess.ExitCode -notin @(0, 3010)) { throw 'WebView2 installation failed.' }
    exit 0
} catch {
    exit 1
} finally {
    Remove-Item -LiteralPath $htdBootstrap -Force -ErrorAction SilentlyContinue
}
