# Publica el MES por un túnel rápido de Cloudflare (https://<algo>.trycloudflare.com).
#
#   .\tunel.ps1              # modo seguro (sin páginas de depuración), puerto 8508
#   .\tunel.ps1 -Port 8001   # otro puerto
#   .\tunel.ps1 -Depurar     # muestra errores detallados (solo para diagnosticar; NO para compartir)
#
# Ctrl+C detiene el túnel y el servidor.
param(
    [int]$Port = 8508,
    [switch]$Depurar
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No encuentro el entorno virtual (.venv\Scripts\python.exe). Créalo primero (ver README)." }

$cf = Join-Path $PSScriptRoot "cloudflared.exe"
if (-not (Test-Path $cf)) {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { $cf = $cmd.Source } else { throw "No encuentro cloudflared.exe (ni en esta carpeta ni en el PATH)." }
}

# 1) Clave secreta propia en .env: la de desarrollo es pública y no debe usarse en internet.
$envFile = Join-Path $PSScriptRoot ".env"
$hasKey = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*MES_SECRET_KEY=' -Quiet)
if (-not $hasKey) {
    $key = (& $py -c "import secrets; print(secrets.token_urlsafe(50))").Trim()
    Add-Content -Path $envFile -Value "MES_SECRET_KEY=$key"
    Write-Host "Se creó una clave secreta nueva en .env (no la compartas ni la subas a git)." -ForegroundColor DarkGray
}

# 2) Modo: sin depuración, salvo que se pida. --insecure hace que Django sirva CSS/JS igual.
$env:MES_DEBUG = if ($Depurar) { "1" } else { "0" }
& $py manage.py migrate --noinput | Out-Null

$logDir = Join-Path $env:TEMP "mes-tunel"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$djLog = Join-Path $logDir "django.log"
$cfLog = Join-Path $logDir "cloudflared.log"
Remove-Item "$djLog*", "$cfLog*" -ErrorAction SilentlyContinue

$dj = $null
$tunnel = $null
try {
    $dj = Start-Process -FilePath $py -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru `
        -ArgumentList "manage.py", "runserver", "127.0.0.1:$Port", "--nostatic", "--noreload" `
        -RedirectStandardError $djLog -RedirectStandardOutput "$djLog.out"

    # Esperar a que Django escuche
    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        if ($dj.HasExited) { break }
        $client = New-Object Net.Sockets.TcpClient
        try { $ready = $client.ConnectAsync("127.0.0.1", $Port).Wait(500) } catch { } finally { $client.Dispose() }
        if (-not $ready) { Start-Sleep -Milliseconds 300 }
    }
    if (-not $ready) {
        Get-Content $djLog -ErrorAction SilentlyContinue | Select-Object -Last 20
        throw "Django no arrancó en el puerto $Port (¿está ocupado por otro runserver?)."
    }

    $tunnel = Start-Process -FilePath $cf -NoNewWindow -PassThru `
        -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$Port" `
        -RedirectStandardError $cfLog -RedirectStandardOutput "$cfLog.out"

    # Esperar la URL pública que imprime cloudflared
    $url = $null
    for ($i = 0; $i -lt 90 -and -not $url; $i++) {
        Start-Sleep -Milliseconds 500
        if ($tunnel.HasExited) { break }
        try {
            $m = Select-String -Path $cfLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction Stop | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value }
        } catch { }
    }
    if (-not $url) {
        Get-Content $cfLog -ErrorAction SilentlyContinue | Select-Object -Last 20
        throw "cloudflared no entregó una URL. Revisa tu conexión a internet."
    }

    Write-Host ""
    Write-Host "  MES publicado en:  $url" -ForegroundColor Green
    Write-Host "  Local:             http://127.0.0.1:$Port" -ForegroundColor DarkGray
    if ($Depurar) { Write-Host "  ATENCIÓN: modo depuración activo; no compartas este enlace." -ForegroundColor Yellow }
    Write-Host "  Registros:         $logDir" -ForegroundColor DarkGray
    Write-Host "  La URL cambia cada vez que inicias el túnel. Ctrl+C para detener." -ForegroundColor DarkGray
    Write-Host ""

    while (-not $dj.HasExited -and -not $tunnel.HasExited) { Start-Sleep -Seconds 1 }
    if ($dj.HasExited) { Write-Host "Django se detuvo; revisa $djLog" -ForegroundColor Red }
    if ($tunnel.HasExited) { Write-Host "El túnel se cerró; revisa $cfLog" -ForegroundColor Red }
}
finally {
    foreach ($p in @($tunnel, $dj)) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    Write-Host "Túnel y servidor detenidos."
}
