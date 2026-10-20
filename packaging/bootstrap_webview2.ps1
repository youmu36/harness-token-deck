$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$htuBootstrap = Join-Path $PSScriptRoot 'MicrosoftEdgeWebview2Setup.exe'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $htuBootstrap
    $htuSignature = Get-AuthenticodeSignature -LiteralPath $htuBootstrap
    if ($htuSignature.Status -ne 'Valid' -or $htuSignature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw 'The WebView2 bootstrapper signature is not valid.'
    }
    $htuProcess = Start-Process -FilePath $htuBootstrap -ArgumentList '/silent', '/install' -WindowStyle Hidden -Wait -PassThru
    if ($htuProcess.ExitCode -notin @(0, 3010)) { throw 'WebView2 installation failed.' }
    exit 0
} catch {
    exit 1
} finally {
    Remove-Item -LiteralPath $htuBootstrap -Force -ErrorAction SilentlyContinue
}
