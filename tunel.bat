@echo off
rem Doble clic para publicar el MES por un tunel de Cloudflare. Cierra la ventana o Ctrl+C para detener.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunel.ps1" %*
pause
