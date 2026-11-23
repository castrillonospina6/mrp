@echo off
rem Doble clic para publicar el MES por serveo (https://plan-extiblu.serveousercontent.com). Ctrl+C para detener.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0serveo.ps1" %*
pause
