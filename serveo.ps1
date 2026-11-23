# Publica el MES (y la app de planta) en internet por serveo: https://plan-extiblu.serveousercontent.com
#
#   .\serveo.ps1                          # subdominio plan-extiblu, puerto 8508
#   .\serveo.ps1 -Subdominio otro-nombre  # otro subdominio (debe estar registrado con tu llave en serveo)
#   .\serveo.ps1 -Llave C:\ruta\llave     # usar una llave SSH concreta
#
# Arranca Django en 127.0.0.1:8508 (sin modo depuración) y abre el túnel. Si el túnel se cae, se reconecta solo.
# Ctrl+C detiene el túnel y el servidor.
param(
    [string]$Subdominio = "plan-extiblu",
    [int]$Port = 8508,
    [string]$Llave = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$publicHost = "$Subdominio.serveousercontent.com"

# 1) Python con Django: el entorno virtual si funciona; si no, el del sistema.
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
$ssh = (Get-Command ssh -ErrorAction SilentlyContinue).Source
if (-not $ssh) { throw "No encuentro ssh. Instala el «Cliente OpenSSH» en Configuración > Aplicaciones > Características opcionales." }

# 2) .env: clave secreta propia y la dirección de serveo como permitida (Django rechaza nombres que no conoce).
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile) -or -not (Select-String -Path $envFile -Pattern '^\s*MES_SECRET_KEY=' -Quiet)) {
    $key = (& $py -c "import secrets; print(secrets.token_urlsafe(50))").Trim()
    Add-Content -Path $envFile -Value "MES_SECRET_KEY=$key"
}
$hostsLine = Select-String -Path $envFile -Pattern '^\s*MES_ALLOWED_HOSTS=(.*)$' | Select-Object -First 1
if (-not $hostsLine) {
    Add-Content -Path $envFile -Value "MES_ALLOWED_HOSTS=$publicHost"
} elseif (($hostsLine.Matches[0].Groups[1].Value -split ",") -notcontains $publicHost) {
    $all = (Get-Content $envFile) -replace '^\s*MES_ALLOWED_HOSTS=(.*)$', "MES_ALLOWED_HOSTS=`$1,$publicHost"
    Set-Content -Path $envFile -Value $all -Encoding utf8
}
$env:MES_DEBUG = "0"
$env:MES_BASE_URL = "https://$publicHost"

# 3) ¿La red deja salir hacia serveo por SSH (puerto 22)?
$probe = New-Object Net.Sockets.TcpClient
try { $reach = $probe.ConnectAsync("serveo.net", 22).Wait(8000) } catch { $reach = $false } finally { $probe.Dispose() }
if (-not $reach) {
    Write-Host ""
    Write-Host "  No hay conexión con serveo.net por el puerto 22 (SSH)." -ForegroundColor Red
    Write-Host "  Esta red lo está bloqueando o serveo no responde. Pide a sistemas que permitan la salida a serveo.net:22," -ForegroundColor Yellow
    Write-Host "  o ejecuta esto desde otra red. El MES sigue funcionando en la red local con iniciar.bat." -ForegroundColor Yellow
    exit 1
}

# 4) Llaves SSH: la indicada, o las de serveo que haya en ~\.ssh, o la por defecto de ssh.
$keys = @()
if ($Llave) { $keys = @($Llave) } else {
    $keys = @("id_serveo_3", "id_serveo_2", "id_serveo", "id_ed25519", "id_rsa") |
            ForEach-Object { Join-Path "$env:USERPROFILE\.ssh" $_ } | Where-Object { Test-Path $_ }
}
$keyArgs = @(); foreach ($k in $keys) { $keyArgs += @("-i", $k) }

& $py manage.py migrate --noinput | Out-Null
$logDir = Join-Path $env:TEMP "mes-serveo"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$djLog = Join-Path $logDir "django.log"

$dj = $null
try {
    $dj = Start-Process -FilePath $py -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru `
        -ArgumentList "manage.py", "runserver", "127.0.0.1:$Port", "--nostatic", "--noreload" `
        -RedirectStandardError $djLog -RedirectStandardOutput "$djLog.out"
    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        if ($dj.HasExited) { break }
        $c = New-Object Net.Sockets.TcpClient
        try { $ready = $c.ConnectAsync("127.0.0.1", $Port).Wait(500) } catch { } finally { $c.Dispose() }
        if (-not $ready) { Start-Sleep -Milliseconds 300 }
    }
    if (-not $ready) {
        Get-Content $djLog -ErrorAction SilentlyContinue | Select-Object -Last 20
        throw "Django no arrancó en el puerto $Port (¿hay otro servidor usando ese puerto? Ciérralo primero)."
    }

    Write-Host ""
    Write-Host "  MES publicado en:   https://$publicHost/" -ForegroundColor Green
    Write-Host "  App de planta:      https://$publicHost/p/piso/" -ForegroundColor Cyan
    Write-Host "  Local:              http://127.0.0.1:$Port/" -ForegroundColor DarkGray
    Write-Host "  Si serveo pide registrar la llave, abre el enlace que muestra y vuelve a ejecutar esto." -ForegroundColor DarkGray
    Write-Host "  Ctrl+C para detener." -ForegroundColor DarkGray
    Write-Host ""

    while (-not $dj.HasExited) {
        & $ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes `
            -o StrictHostKeyChecking=accept-new @keyArgs -R "${Subdominio}:80:127.0.0.1:$Port" serveo.net
        if ($dj.HasExited) { break }
        Write-Host "El túnel se cerró; reconectando en 5 s… (Ctrl+C para salir)" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
    if ($dj.HasExited) { Write-Host "Django se detuvo; revisa $djLog" -ForegroundColor Red }
}
finally {
    if ($dj -and -not $dj.HasExited) { Stop-Process -Id $dj.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "Túnel y servidor detenidos."
}
