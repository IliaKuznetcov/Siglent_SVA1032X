$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Create the project virtual environment first: py -m venv .venv"
}
function Invoke-ProjectPython {
    & $Python @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)." }
}
Push-Location $ProjectRoot
try {
    Invoke-ProjectPython -m pip install -r requirements.txt pyinstaller
    Push-Location (Join-Path $ProjectRoot "src")
    try {
        Invoke-ProjectPython -m PyInstaller --clean --noconfirm --distpath ..\dist --workpath ..\build siglent_app.spec
    } finally { Pop-Location }
    $Executable = Join-Path $ProjectRoot "dist\Siglent_SVA1032X.exe"
    if (-not (Test-Path $Executable)) { throw "Executable was not produced." }
    $PackageDirectory = Join-Path $ProjectRoot "dist\Siglent_SVA1032X_v1.0.0_Windows"
    New-Item -ItemType Directory -Force -Path $PackageDirectory | Out-Null
    Copy-Item $Executable $PackageDirectory -Force
    Copy-Item README.md,CALIBRATION.md,THIRD_PARTY_NOTICES.md $PackageDirectory -Force
    $ReleaseZip = Join-Path $ProjectRoot "dist\Siglent_SVA1032X_v1.0.0_Windows.zip"
    Compress-Archive -Path "$PackageDirectory\*" -DestinationPath $ReleaseZip -Force
    $Hash = (Get-FileHash $ReleaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  Siglent_SVA1032X_v1.0.0_Windows.zip" | Set-Content -Encoding ascii (Join-Path $ProjectRoot "dist\SHA256SUMS.txt")
    Write-Host "Created $ReleaseZip"
    Write-Host "Test the executable before publishing the release."
} finally { Pop-Location }
