<#
.SYNOPSIS
    Compila Autonoma.exe (onefile, consola) y deja su SHA-256 junto al binario.
.DESCRIPTION
    Uso personal en Windows. Requiere Python 3.10+ en el PATH y acceso a PyPI.
    El script es idempotente: crea el venv en .venv-build, instala el paquete con
    los extras [build,keyboard] y verifica que el ejecutable resultante se autoensaya
    (--selftest --json) antes de dar la versión por buena.
.NOTES
    Si `pynput` falla al instalar, quita el extra keyboard: el listener global es opcional.
#>
[CmdletBinding()]
param(
    [switch]$SkipSmoke,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
# Scripts/build_windows.ps1 vive en autonoma-agent/scripts: el repositorio está dos niveles arriba.
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$agent = Join-Path $repo "autonoma-agent"
if (-not (Test-Path (Join-Path $agent "pyproject.toml"))) { throw "no se encontró autonoma-agent/pyproject.toml en $repo" }
Set-Location $repo
$root = $repo
Write-Host "== Autonoma: build Windows (onefile) en $root"

$venv = Join-Path $root ".venv-build"
if (-not (Test-Path $venv)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el venv de construcción" }
}
$py = Join-Path $venv "Scripts\python.exe"

Write-Host "-- Instalando dependencias de construcción"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install "./autonoma-agent[build,keyboard]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "   pynput falló; reintentando sin el extra keyboard (el hotkey global es opcional)"
    & $py -m pip install "./autonoma-agent[build]"
    if ($LASTEXITCODE -ne 0) { throw "Fallo instalando el paquete" }
}
# El paquete se instala desde la raíz del repo; se compila dentro de autonoma-agent.
Push-Location $agent
try {
    Write-Host "-- PyInstaller"
    & $py -m PyInstaller --noconfirm --clean autonoma.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

    $exe = Join-Path (Get-Location) "dist\Autonoma.exe"
    if (-not (Test-Path $exe)) { throw "No se generó $exe" }

    if (-not $SkipSmoke) {
        Write-Host "-- Autoensayo del binario (offline, sin credenciales)"
        $env:AUTONOMA_HOME = Join-Path $env:TEMP "autonoma-smoke"
        $version = (& $exe --version) -join ""
        if ($LASTEXITCODE -ne 0) { throw "--version falló" }
        $selftestRaw = & $exe --selftest --json
        if ($LASTEXITCODE -ne 0) { throw "--selftest devolvió $LASTEXITCODE" }
        $selftest = ($selftestRaw -join "`n") | ConvertFrom-Json
        if (-not $selftest.ok) { throw "selftest_ok=false: $($selftest.checks | Out-String)" }
        if ($selftest.frozen -ne $true) { throw "el binario no se marcó como congelado" }
        Write-Host "   OK: $version · python $($selftest.python) · $($selftest.checks.Count) checks"
    }

    Write-Host "-- ZIP portable (copiar y usar)"
    & $py (Join-Path $repo "scripts\make_bundle.py") --platform windows --dist-dir (Join-Path $agent "dist")
    if ($LASTEXITCODE -ne 0) { Write-Warning "no se pudo armar el ZIP portable (el .exe sigue siendo válido)" }

    $hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    "$hash  Autonoma.exe" | Set-Content -NoNewline (Join-Path (Get-Location) "dist\Autonoma.exe.sha256")
    $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "== Listo: dist\Autonoma.exe ($sizeMb MB)"
    Write-Host "   SHA-256: $hash"
    Write-Host "   Copia .env.example a .env junto al .exe y añade NOTRACK_API_KEY."
}
finally {
    Pop-Location
}
