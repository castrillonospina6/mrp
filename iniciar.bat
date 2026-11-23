@echo off
rem Doble clic para arrancar el MES y la app de piso (tablets) en la red local. Cierra la ventana o Ctrl+C para detener.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1" %*
pause
