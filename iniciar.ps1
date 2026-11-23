# Arranca el MES y, con él, la app de control de piso (tablets de los operarios) en la red local.
#
#   .\iniciar.ps1                     # puerto 8508, sin páginas de depuración
#   .\iniciar.ps1 -Port 8080          # otro puerto
#   .\iniciar.ps1 -Depurar            # errores detallados (solo para diagnosticar)
#   .\iniciar.ps1 -AlIniciarWindows   # además, que arranque solo cada vez que se inicia sesión en este PC
#
# Las tablets entran por http://IP-DE-ESTE-PC:8508/p/piso/ (misma red Wi-Fi). La primera vez Windows pregunta si
# permite que Python use la red: acepta en «redes privadas». Ctrl+C detiene el servidor.
param(
    [int]$Port = 8508,
    [switch]$Depurar,
    [switch]$AlIniciarWindows
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1) Python con Django: el entorno virtual si funciona; si no, el Python del sistema.
function Test-Django([string]$exe) {
    try { & $exe -c "import django" 2>$null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
$py = $null
$venv = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ((Test-Path $venv) -and (Test-Django $venv)) { $py = $venv }
if (-not $py) {
    foreach ($name in "python", "py") {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Django $cmd.Source)) { $py = $cmd.Source; break }
    }
}
if (-not $py) { throw "No encuentro un Python con Django. Instala las dependencias: python -m pip install -r requirements.txt" }

if ($AlIniciarWindows) {
    $startup = [Environment]::GetFolderPath("Startup")
    $lnk = Join-Path $startup "MES Planta.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $PSScriptRoot "iniciar.bat"
    $s.WorkingDirectory = $PSScriptRoot
    $s.WindowStyle = 7  # minimizada
    $s.Save()
    Write-Host "Listo: el MES arrancará solo al iniciar sesión en Windows ($lnk). Para quitarlo borra ese acceso directo." -ForegroundColor Green
}

# 2) Clave secreta propia (la de desarrollo es pública).
$envFile = Join-Path $PSScriptRoot ".env"
$hasKey = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*MES_SECRET_KEY=' -Quiet)
if (-not $hasKey) {
    $key = (& $py -c "import secrets; print(secrets.token_urlsafe(50))").Trim()
    Add-Content -Path $envFile -Value "MES_SECRET_KEY=$key"
    Write-Host "Se creó una clave secreta nueva en .env (no la compartas ni la subas a git)." -ForegroundColor DarkGray
}

# 3) Direcciones de este PC en la red local: Django solo responde a los nombres que conoce.
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -ExpandProperty IPAddress)
$hostName = [System.Net.Dns]::GetHostName()
$extra = @($env:MES_ALLOWED_HOSTS -split "," | Where-Object { $_ })
$env:MES_ALLOWED_HOSTS = (@($ips) + @($hostName) + $extra | Select-Object -Unique) -join ","
$env:MES_DEBUG = if ($Depurar) { "1" } else { "0" }

& $py manage.py migrate --noinput | Out-Null

Write-Host ""
Write-Host "  MES Planta en marcha (la app de piso arranca con él)" -ForegroundColor Green
Write-Host "  Oficina:           http://127.0.0.1:$Port/" -ForegroundColor Gray
foreach ($ip in $ips) {
    Write-Host "  Tablets de planta: http://${ip}:$Port/p/piso/" -ForegroundColor Cyan
}
Write-Host "  Códigos de tablet y QR de pausas: menú Módulos › Tablets y QR" -ForegroundColor DarkGray
Write-Host "  Ctrl+C para detener." -ForegroundColor DarkGray
Write-Host ""

# --nostatic: los .css/.js los sirve el MES con caché del navegador y comprimidos (mes_core/static_serve.py)
& $py manage.py runserver "0.0.0.0:$Port" --nostatic --noreload
