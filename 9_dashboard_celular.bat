@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Abrindo o dashboard com acesso pelo celular (mesmo Wi-Fi)...
echo Feche o 8_dashboard.bat antes, se estiver aberto.
%PY% dashboard.py --rede
pause
